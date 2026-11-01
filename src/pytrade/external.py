import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path
from urllib.parse import urlsplit

from .domain import Instrument, MarketCapability, Snapshot
from .records import Journal, load_journal
from .risk import RiskBook, RiskLimits
from .stops import StopManager
from .protocol import validate_event
from .metadata import query_instrument
from .reconciliation import validate_snapshot


@dataclass
class ConnectionConfig:
    md_front: str
    td_front: str = ""
    broker: str = ""
    user: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    app_id: str = ""
    auth_code: str = field(default="", repr=False)
    environment: str = "simulation"
    allow_orders: bool = False
    max_order: int = 1
    max_position: int = 10
    stale_seconds: int = 10
    md_anonymous: bool = True
    md_separate_credentials: bool = False
    md_broker: str = ""
    md_user: str = field(default="", repr=False)
    md_password: str = field(default="", repr=False)
    max_total: int = 0
    max_group: int = 0
    max_cancels: int = 0
    max_daily_loss: str = "0"
    probe_mode: bool = False
    paper_trading: bool = False

    def risk_limits(self):
        return RiskLimits(self.max_total, self.max_group, self.max_cancels, str(self.max_daily_loss))

    def market_credentials(self):
        if self.md_separate_credentials:
            return self.md_broker, self.md_user, self.md_password
        return self.broker, self.user, self.password

    def validate(self):
        if self.paper_trading and (self.td_front or self.allow_orders or self.probe_mode or self.environment != "simulation"):
            raise ValueError("在线模拟只允许行情连接与本地撮合，不能配置交易前置或外部报撤单")
        if self.probe_mode and (self.td_front or self.allow_orders or not self.md_anonymous or self.user or self.password or self.auth_code or self.md_user or self.md_password):
            raise ValueError("故障注入仅适用于空凭据、无交易接口的只读行情验证")
        if self.allow_orders:
            self.risk_limits().validate()
        if self.environment not in {"simulation", "readonly"}:
            raise ValueError("环境须为外部模拟或实盘只读")
        for address in (self.md_front, self.td_front):
            if not address:
                continue
            try:
                parsed = urlsplit(address)
                valid = parsed.scheme == "tcp" and parsed.hostname and parsed.port and not parsed.username and not parsed.password and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("前置地址须为 tcp://主机:端口")
        if not self.md_front:
            raise ValueError("请填写行情前置")
        if not self.md_anonymous and not all(self.market_credentials()):
            raise ValueError("行情账户登录需要服务方提供的 BrokerID、账号和密码")
        if self.td_front and not all((self.broker, self.user, self.password)):
            raise ValueError("交易连接需要 BrokerID、账号和密码")
        if bool(self.app_id) != bool(self.auth_code):
            raise ValueError("AppID 与授权码须同时填写或同时留空")
        if self.allow_orders and (self.environment != "simulation" or not self.td_front):
            raise ValueError("当前仅允许在明确的外部模拟环境启用报撤单")
        if not 1 <= self.max_order <= 1000 or not 1 <= self.max_position <= 100000 or not 2 <= self.stale_seconds <= 300:
            raise ValueError("风险或行情超时参数无效")


def redact(text, config):
    result = str(text)
    for secret in sorted((config.password, config.auth_code, config.user, config.md_user, config.md_password), key=len, reverse=True):
        if secret:
            result = result.replace(secret, "***")
    return result


def finite_decimal(value):
    number = Decimal(str(value))
    if not number.is_finite() or abs(number) > Decimal("1e50"):
        raise ValueError("无效数值")
    return number


def quote_ticks(value, tick):
    number = finite_decimal(value) / tick
    nearest = number.to_integral_value(rounding=ROUND_HALF_UP)
    if nearest <= 0 or abs(number - nearest) > Decimal("0.000001"):
        raise ValueError("报价未对齐 tick")
    return int(nearest)


def normalize_quote(data, instrument, source, received=None):
    last = quote_ticks(data["LastPrice"], instrument.tick)
    levels = []
    for side in ("Bid", "Ask"):
        rows = []
        for level in range(1, 6):
            try:
                ticks = quote_ticks(data.get(f"{side}Price{level}"), instrument.tick)
                quantity = int(data.get(f"{side}Volume{level}", 0))
                if quantity <= 0:
                    continue
                if rows and ((side == "Bid" and ticks >= rows[-1][0]) or (side == "Ask" and ticks <= rows[-1][0])):
                    raise ValueError("盘口档位顺序无效")
                rows.append((ticks, quantity))
            except (ValueError, TypeError, ArithmeticError):
                if level == 1:
                    rows = []
                break
        levels.append(tuple(rows))
    if levels[0] and levels[1] and levels[0][0][0] > levels[1][0][0]:
        raise ValueError("盘口买卖价格交叉")
    def optional_number(name, positive=False):
        try:
            value = finite_decimal(data.get(name))
            return value if (value > 0 if positive else value >= 0) else None
        except (ValueError, TypeError, ArithmeticError):
            return None

    def optional_ticks(name):
        try:
            return quote_ticks(data.get(name), instrument.tick)
        except (ValueError, TypeError, ArithmeticError):
            return None

    return Snapshot(instrument.key, last, levels[0], levels[1], int(data.get("Volume", 0)), str(data.get("UpdateTime", "")), source, str(data.get("TradingDay", "")), str(data.get("ActionDay", "")), received if received is not None else time.monotonic(), optional_number("PreSettlementPrice", True), optional_number("OpenInterest"), optional_number("PreOpenInterest"), optional_ticks("UpperLimitPrice"), optional_ticks("LowerLimitPrice"))


def load_instruments(path):
    result = {}
    if not path.exists():
        return result
    for _, element in ET.iterparse(path, events=("end",)):
        if element.tag != "Instrument":
            continue
        row = element.attrib
        try:
            tick = finite_decimal(row["PriceTick"])
            multiplier = int(row["VolumeMultiple"])
            exchange, symbol = row["ExchangeID"], row["InstrumentID"]
            if tick <= 0 or multiplier <= 0 or not re.fullmatch(r"[A-Za-z0-9& +_-]+", symbol):
                continue
            key = f"{exchange}.{symbol}"
            name = element.text or symbol
            product_class = row.get("ProductClass", "")
            if product_class.isdigit() and 49 <= int(product_class) <= 57:
                product_class = chr(int(product_class))
            result[key] = Instrument(key, name if "?" not in name else symbol, tick, multiplier, 1, 0, row.get("ExpireDate", ""), "historical", datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(), "", product_class)
        except (KeyError, ValueError, ArithmeticError):
            pass
        element.clear()
    return result


ORDER_STATES = {"0": "全部成交", "1": "部分成交", "2": "部分成交不在队列", "3": "未成交", "4": "未成交不在队列", "5": "已撤单", "a": "未知待核对", "b": "尚未触发", "c": "已触发"}
TERMINAL = {"0", "2", "4", "5", "rejected"}
OFFSETS = {"开仓": "0", "平仓": "1", "平今": "3", "平昨": "4"}
OFFSET_NAMES = {v: k for k, v in OFFSETS.items()}


def order_key(row):
    return f"{row.get('TradingDay', '')}:{row.get('FrontID', 0)}:{row.get('SessionID', 0)}:{str(row.get('OrderRef', '')).strip()}"


def trade_key(row):
    return ":".join(str(row.get(name, "")) for name in ("TradingDay", "ExchangeID", "InstrumentID", "TradeID"))


class ExternalSession:
    def __init__(self, config, gateway, catalog=None, journal_path=None, legacy_journal_path=None):
        config.validate()
        self.config = config
        self.gateway = gateway
        self.catalog = catalog or {}
        self.verified = set()
        self.specifications = {}
        self.metadata_stage = {}
        self.snapshots = {}
        self.raw_quotes = {}
        self.capabilities = {}
        self.references = Counter()
        self.orders = {}
        self.trades = {}
        self.positions = []
        self.accounts = []
        self.md_status = "连接中"
        self.td_status = "连接中" if config.td_front else "未配置"
        self.md_epoch = 0
        self.md_state_at = time.monotonic()
        self.td_state_at = time.monotonic()
        self.subscribed = set()
        self.quote_wait_started = {}
        self.ready = False
        self.logged_in = False
        self.settled = False
        self.settlement = ""
        self.front_id = 0
        self.session_id = 0
        self.order_ref = 0
        self.trading_day = ""
        self.sync_cycle = 0
        self.syncing = False
        self.refreshing_instruments = False
        self.needs_metadata = bool(config.td_front)
        self.revision = 0
        self.sync_revision = 0
        self.query_rows = {}
        self.error = ""
        self.faulted = False
        self.canceling = set()
        self.last_refresh = 0.0
        self.journal_path = Path(journal_path) if journal_path else None
        self.writer = None
        self.dispatches = []
        self.stop_subscriptions = {}
        self.td_generation = 0
        self.risk = RiskBook()
        self.stops = StopManager(self)
        self.metrics = Counter()
        if self.journal_path:
            try:
                self.writer = Journal(self.journal_path)
            except (OSError, ValueError) as error:
                self.faulted = True
                self.error = str(error) if isinstance(error, ValueError) else "交易日志无法创建，禁止交易"
                return
        load_path = self.journal_path
        if load_path and not load_path.exists() and legacy_journal_path:
            load_path = Path(legacy_journal_path)
        if load_path and load_path.exists():
            try:
                state = load_journal(load_path)
                if state["version"] == 2 and (not isinstance(state.get("risk"), dict) or not isinstance(state.get("observations"), dict)):
                    raise ValueError("交易日志缺少风险或回报状态")
                self.orders = state["orders"]
                self.trades = state["trades"]
                self.risk = RiskBook(state.get("risk"))
                self.stops = StopManager(self, state.get("stops", []))
                self.metrics = Counter(state.get("observations", {}))
            except (OSError, ValueError, AttributeError, TypeError, KeyError, ArithmeticError):
                self.faulted = True
                self.error = "交易日志损坏，禁止交易；须保留文件并人工核对"

    def persist(self):
        if not self.journal_path:
            return 0
        if self.faulted:
            raise ValueError(self.error or "交易故障，禁止覆盖日志")
        try:
            if self.writer is None:
                self.writer = Journal(self.journal_path)
            state = self.journal_state()
            return self.writer.submit(state, owned=True)
        except (OSError, ValueError) as error:
            self.ready = False
            self.faulted = True
            self.error = str(error) if isinstance(error, ValueError) else "交易日志无法创建或写入，禁止交易"
            raise ValueError(self.error) from None

    def journal_state(self):
        return dict(version=2, orders=dict(self.orders), trades=dict(self.trades), risk=self.risk.state(), stops=self.stops.records(), observations=dict(self.metrics))

    def archive_history(self):
        old_orders = {key for key, row in self.orders.items() if row.get("OrderStatus") in TERMINAL and len(str(row.get("TradingDay", ""))) == 8 and str(row["TradingDay"]) < self.trading_day}
        old_trades = {key for key, row in self.trades.items() if len(str(row.get("TradingDay", ""))) == 8 and str(row["TradingDay"]) < self.trading_day}
        if not old_orders and not old_trades:
            return
        if not self.journal_path:
            return
        if self.writer is None:
            self.writer = Journal(self.journal_path)
        self.writer.archive(self.journal_state(), self.trading_day)
        for key in old_orders:
            del self.orders[key]
        for key in old_trades:
            del self.trades[key]
            self.risk.processed.discard(key)

    def flush(self):
        if self.writer:
            self.writer.flush()

    def close(self):
        for stop in self.stops.stops.values():
            if stop.mode == "local" and stop.state not in {"filled", "canceled", "failed"}:
                stop.state, stop.message = "paused", "连接已关闭；重连对账后须手动启用"
        if not self.faulted:
            self.persist()
        if self.writer:
            self.writer.close()
            self.writer = None

    def durable_send(self, command, row, token=""):
        sequence = self.persist()
        self.dispatches.append((sequence, self.td_generation, command, dict(row), token))
        self.dispatch_pending()

    def dispatch_pending(self):
        if self.writer and self.writer.error:
            self.faulted = True
            self.ready = False
            self.error = self.writer.error
        waiting = []
        changed = False
        for sequence, generation, command, row, token in self.dispatches:
            if self.faulted:
                continue
            key = order_key(row)
            contract = f"{row.get('ExchangeID')}.{row.get('InstrumentID')}"
            reason = ""
            if generation != self.td_generation or not self.logged_in or not self.settled:
                reason = "交易连接或结算状态已改变，本地排队请求未发送"
            elif self.config.environment != "simulation" or not self.config.allow_orders:
                reason = "报撤单权限已改变，本地排队请求未发送"
            elif command == "insert":
                if row.get("LocalMDEpoch") != self.md_epoch or not self.quote_fresh(contract):
                    reason = "行情连接已改变或报价过期，本地排队委托未发送"
                elif time.monotonic() - row.get("LocalQuoteReceived", 0) > self.config.stale_seconds:
                    reason = "报单所用行情已过期，本地排队委托未发送"
                elif contract not in self.verified or str(self.catalog[contract].tick) != row.get("LocalPriceTick"):
                    reason = "合约参数已改变，本地排队委托未发送"
                elif row.get("LocalRevision") != self.revision:
                    reason = "账户回报已改变，本地排队委托未发送，须重新对账"
                else:
                    try:
                        self.check_queued_insert(row, contract, key)
                    except (ValueError, KeyError, ArithmeticError):
                        reason = "风险限额、可平持仓或价格范围已改变，本地排队委托未发送"
            if reason:
                if command == "insert" and self.orders.get(key, {}).get("OrderStatus") == "pending":
                    self.orders[key] = {**self.orders[key], "OrderStatus": "rejected", "StatusMsg": reason, "DispatchState": "not_sent"}
                elif command == "cancel":
                    self.canceling.discard(key)
                    self.risk.unsent_cancel(token)
                self.error = reason
                changed = True
                continue
            if self.writer and self.writer.written < sequence:
                waiting.append((sequence, generation, command, row, token))
                continue
            try:
                self.send(command, row=row, cancel_token=token, quote_epoch=row.get("LocalMDEpoch"), quote_received=row.get("LocalQuoteReceived"))
            except ValueError:
                self.ready = False
                self.faulted = True
                self.error = "交易命令发送结果无法确认，禁止重发；须重新连接对账"
        self.dispatches = waiting
        if changed and not self.faulted:
            self.persist()

    def check_queued_insert(self, row, contract, identifier):
        quantity = row["VolumeTotalOriginal"]
        instrument = self.catalog[contract]
        if not instrument.trading_enabled or not instrument.min_order_volume <= quantity <= min(instrument.max_order_volume, self.config.max_order):
            raise ValueError("合约或单笔限额已改变")
        price = finite_decimal(row["LimitPrice"])
        raw = self.raw_quotes[contract]["row"]
        lower, upper = (finite_decimal(raw[name]) for name in ("LowerLimitPrice", "UpperLimitPrice"))
        if not 0 < lower <= price <= upper:
            raise ValueError("价格超出有效涨跌停范围")
        if row["CombOffsetFlag"] == "0":
            pending = [value for key, value in self.orders.items() if key != identifier and value.get("OrderStatus") not in TERMINAL and value.get("CombOffsetFlag") == "0"]
            self.risk.check_open(self.config.risk_limits(), self.positions, pending, quantity, row["Group"])
            direction = "2" if row["Direction"] == "0" else "3"
            held = sum(value.get("Position", 0) for value in self.positions if f"{value.get('ExchangeID')}.{value.get('InstrumentID')}" == contract and value.get("PosiDirection") == direction)
            if held + self.pending_quantity(contract, row["Direction"], "0") > self.config.max_position:
                raise ValueError("单合约持仓限额已改变")
        else:
            offset = row["CombOffsetFlag"]
            if quantity > self.close_available(contract, row["Direction"], offset, exclude=identifier):
                raise ValueError("可平持仓不足")

    def send(self, command, **values):
        self.gateway.send({"command": command, **values})

    def retain(self, key):
        if any(existing.split(".", 1)[1] == key.split(".", 1)[1] and existing != key for existing in self.references):
            raise ValueError("行情订阅存在跨交易所重名合约，需先核对来源映射")
        self.references[key] += 1
        self.quote_wait_started.setdefault(key, time.monotonic())
        self.send("subscriptions", keys=list(self.references))

    def release(self, key):
        self.references[key] -= 1
        if self.references[key] <= 0:
            del self.references[key]
            self.snapshots.pop(key, None)
            self.raw_quotes.pop(key, None)
            self.subscribed.discard(key)
            self.quote_wait_started.pop(key, None)
        self.send("subscriptions", keys=list(self.references))

    def synchronize(self, instruments=False):
        if not self.logged_in or self.syncing or self.faulted or self.dispatches:
            return
        instruments = instruments or self.needs_metadata
        self.needs_metadata = instruments
        self.ready = False
        self.syncing = True
        self.refreshing_instruments = instruments
        if instruments:
            self.metadata_stage = {}
        self.sync_cycle += 1
        self.sync_revision = self.revision
        self.query_rows = {}
        self.last_refresh = time.monotonic()
        self.send("sync", cycle=self.sync_cycle, instruments=instruments)

    def handle(self, event):
        try:
            validate_event(event)
        except ValueError as error:
            self.event_fault(str(error))
            return
        try:
            self.handle_valid_event(event)
        except ValueError as error:
            self.event_fault(redact(str(error), self.config))
        except (KeyError, TypeError, ArithmeticError, AttributeError):
            self.event_fault(self.error if self.faulted and self.error else "接口数据结构或状态异常，须重新连接并查询对账")

    def event_fault(self, message):
        self.ready = False
        self.logged_in = False
        self.faulted = True
        self.error = message
        self.td_status = "接口故障，须重新连接对账"
        self.md_status = "接口故障"
        self.snapshots.clear()
        self.raw_quotes.clear()

    def handle_valid_event(self, event):
        kind = event["kind"]
        self.metrics[kind] += 1
        if kind == "md_state":
            if event["epoch"] < self.md_epoch:
                return
            self.md_state_at = time.monotonic()
            self.md_epoch = event["epoch"]
            self.md_status = event["status"]
            self.snapshots.clear()
            self.raw_quotes.clear()
            self.subscribed.clear()
            self.quote_wait_started = {key: time.monotonic() for key in self.references}
        elif kind == "subscription":
            if event.get("epoch") != self.md_epoch or event["key"] not in self.references:
                return
            key = event["key"]
            if event.get("error"):
                self.subscribed.discard(key)
                self.snapshots.pop(key, None)
                self.raw_quotes.pop(key, None)
                self.error = f"{key} 订阅失败，代码 {event['error']}"
            elif event.get("epoch") == self.md_epoch:
                self.subscribed.add(key)
                if not self.snapshots:
                    self.md_status = f"已订阅 {len(self.subscribed)} 个合约，等待有效行情"
        elif kind == "quote":
            if event["epoch"] != self.md_epoch or event["key"] not in self.references or event["key"] not in self.subscribed:
                return
            previous = self.raw_quotes.get(event["key"])
            if previous and event["received"] <= previous["received"]:
                return
            self.raw_quotes[event["key"]] = event
            self.accept_quote(event)
        elif kind == "td_state":
            self.td_generation += 1
            for stop in self.stops.stops.values():
                if stop.mode == "local" and stop.state not in {"filled", "canceled", "failed"}:
                    stop.state, stop.message = "paused", "交易连接改变；须重新对账并手动启用"
            self.td_state_at = time.monotonic()
            self.td_status = event["status"]
            self.ready = False
            self.logged_in = False
            self.settled = False
            self.syncing = False
        elif kind == "td_login":
            self.td_generation += 1
            self.logged_in = True
            self.ready = False
            self.settled = False
            self.front_id, self.session_id = event["front"], event["session"]
            self.order_ref = int(str(event.get("max_ref") or "").strip() or "0")
            self.trading_day = event["day"]
            self.risk.set_day(self.trading_day)
            self.td_status = "登录成功，查询对账中"
            self.verified.clear()
            self.specifications.clear()
            self.synchronize(instruments=True)
        elif kind == "query_row" and self.logged_in and self.syncing and event["cycle"] == self.sync_cycle:
            self.accept_query_rows(event["query"], [event["row"]])
        elif kind == "query_rows" and self.logged_in and self.syncing and event["cycle"] == self.sync_cycle:
            self.accept_query_rows(event["query"], event["rows"])
        elif kind == "sync_done" and self.logged_in and self.syncing and event["cycle"] == self.sync_cycle:
            self.complete_sync()
        elif kind == "order":
            if 0 < event["row"].get("VolumeTraded", 0) < event["row"].get("VolumeTotalOriginal", 0):
                self.metrics["partial_order_observed"] += 1
            if self.merge_order(event["row"]):
                self.revision += 1
                self.ready = False
                self.persist()
        elif kind == "trade":
            row = event["row"]
            if self.merge_trade(row):
                self.revision += 1
                self.ready = False
                self.persist()
        elif kind == "insert_error":
            key = event["order_key"]
            row = self.orders.get(key)
            if row and row.get("OrderStatus") == "pending":
                self.orders[key] = {**row, "OrderStatus": "rejected", "StatusMsg": event["message"]}
            self.error = event["message"]
            self.ready = False
            if not self.faulted:
                self.persist()
        elif kind == "cancel_error":
            self.canceling.discard(event["order_key"])
            if event.get("not_sent"):
                self.risk.unsent_cancel(event.get("cancel_token", ""))
            self.error = event["message"]
            if not self.faulted:
                self.persist()
        elif kind == "settled":
            self.settled = True
            self.synchronize()
        elif kind in {"error", "fatal"}:
            self.error = event["message"]
            if kind == "fatal" or event.get("channel") == "td":
                self.ready = False
                self.syncing = False
            if kind == "fatal":
                self.faulted = True
                self.logged_in = False
                self.td_status = "故障，必须断开后重连对账"
                self.md_status = "故障"
                self.snapshots.clear()

    def accept_quote(self, event):
        key = event["key"]
        if key not in self.catalog:
            return
        try:
            self.snapshots[key] = normalize_quote(event["row"], self.catalog[key], self.config.md_front, event["received"])
            self.md_status = "收到有效真实行情"
            capability = self.capabilities.setdefault(key, MarketCapability(self.config.md_front, key))
            capability.observe(self.snapshots[key], datetime.now(timezone.utc).isoformat())
            if self.quote_fresh(key):
                extrema = [self.snapshots[key].last]
                for field in ("range_low", "range_high"):
                    try:
                        extrema.append(quote_ticks(event[field], self.catalog[key].tick))
                    except (KeyError, ValueError, ArithmeticError):
                        pass
                self.stops.observe(key, min(extrema), max(extrema))
        except (ValueError, TypeError, ArithmeticError):
            if self.faulted:
                raise
            self.snapshots.pop(key, None)
            self.error = f"{key} 无效报价已隔离"

    def merge_order(self, row):
        row = dict(row)
        if row.get("OrderSubmitStatus") == "4":
            row["OrderStatus"] = "rejected"
        key = order_key(row)
        old = self.orders.get(key)
        if old:
            for name in ("ExchangeID", "InstrumentID", "Direction", "CombOffsetFlag", "CombHedgeFlag", "VolumeTotalOriginal"):
                if name in old and name in row and old[name] != row[name]:
                    raise ValueError("相同委托标识的合约、方向或数量冲突，须人工核对")
            for name in ("Group", "Strategy", "StopID"):
                if name in old:
                    row[name] = old[name]
            if row.get("VolumeTraded", 0) < old.get("VolumeTraded", 0):
                return False
            if old.get("OrderStatus") in TERMINAL and row.get("OrderStatus") not in TERMINAL:
                row["OrderStatus"] = old["OrderStatus"]
            if old.get("OrderStatus") == "0":
                row["OrderStatus"] = "0"
            if row.get("VolumeTraded", 0) >= row.get("VolumeTotalOriginal", 1) > 0:
                row["OrderStatus"] = "0"
            if row == old:
                return False
        self.orders[key] = row
        if row.get("OrderStatus") in TERMINAL:
            self.canceling.discard(key)
        return True

    def merge_trade(self, row):
        key = trade_key(row)
        old = self.trades.get(key)
        if old is not None:
            for name in ("Volume", "Price", "Direction", "OffsetFlag", "HedgeFlag", "OrderSysID"):
                if name not in old or name not in row:
                    continue
                previous, incoming = old[name], row[name]
                if name == "Price":
                    previous, incoming = finite_decimal(previous), finite_decimal(incoming)
                if previous != incoming:
                    raise ValueError("相同成交标识的回报内容冲突，须重新连接并人工核对")
            return False
        if "Volume" in row and (type(row["Volume"]) is not int or row["Volume"] <= 0):
            raise ValueError("成交数量无效，须人工核对")
        self.trades[key] = dict(row)
        return True

    def accept_query_rows(self, query, rows):
        if query != "instruments":
            self.query_rows.setdefault(query, []).extend(rows)
            return
        timestamp = datetime.now(timezone.utc).isoformat()
        for row in rows:
            try:
                instrument = query_instrument(row, self.trading_day, timestamp)
                self.metadata_stage[instrument.key] = (instrument, dict(row))
            except (ValueError, KeyError, TypeError, ArithmeticError):
                self.metrics["invalid_instrument"] += 1

    def complete_sync(self):
        rows = self.query_rows
        if self.refreshing_instruments:
            self.verified.clear()
            self.specifications.clear()
            self.needs_metadata = False
            for key, (instrument, specification) in self.metadata_stage.items():
                self.catalog[key] = instrument
                self.specifications[key] = specification
                if instrument.trading_enabled:
                    self.verified.add(key)
            self.metadata_stage = {}
        for event in self.raw_quotes.values():
            self.accept_quote(event)
        maximum_ref = self.order_ref
        for row in rows.get("orders", []):
            self.merge_order(row)
            if row.get("FrontID") == self.front_id and row.get("SessionID") == self.session_id:
                try:
                    maximum_ref = max(maximum_ref, int(row["OrderRef"]))
                except (ValueError, KeyError):
                    pass
        self.order_ref = maximum_ref
        for row in rows.get("trades", []):
            self.merge_trade(row)
        positions = rows.get("positions", [])
        accounts = rows.get("account", [])
        if "settlement" in rows:
            self.settlement = "".join(row.get("Content", "") for row in rows["settlement"])
        self.syncing = False
        known = TERMINAL | {"1", "3", "b", "c"}
        unknown = any(row.get("OrderStatus") not in known or (row.get("TradingDay") != self.trading_day and row.get("OrderStatus") not in TERMINAL) for row in self.orders.values())
        stable = self.sync_revision == self.revision
        complete = {"orders", "trades", "positions", "account"}.issubset(rows)
        if complete and stable:
            validate_snapshot(positions, accounts, self.trading_day)
            self.positions, self.accounts = positions, accounts
        self.ready = complete and stable and not unknown and bool(self.accounts) and bool(self.verified) and not self.faulted
        if self.ready:
            self.risk.equity(self.accounts, self.trading_day, self.config.risk_limits())
            self.risk.reconcile(self.positions, self.trades, self.orders)
            self.archive_history()
        self.td_status = "对账完成" if self.ready else "对账未稳定或存在未知委托，禁止发单"
        if not self.faulted:
            self.persist()
        if not stable or not complete:
            self.synchronize()

    def quote_fresh(self, key):
        snapshot = self.snapshots.get(key)
        return snapshot is not None and 0 <= time.monotonic() - snapshot.received_monotonic <= self.config.stale_seconds and snapshot.trading_day == self.trading_day

    def check_trading(self, require_reconciled=True):
        if self.config.environment != "simulation" or not self.config.allow_orders:
            raise ValueError("当前连接只读，未启用外部模拟报撤单")
        if not self.logged_in or not self.settled or self.faulted or (require_reconciled and not self.ready):
            raise ValueError("交易尚未完成登录、结算确认与对账")

    def pending_quantity(self, key, direction, offset=None, exclude=None):
        return sum(max(0, row.get("VolumeTotalOriginal", 0) - row.get("VolumeTraded", 0)) for identifier, row in self.orders.items() if identifier != exclude and f"{row.get('ExchangeID')}.{row.get('InstrumentID')}" == key and row.get("Direction") == direction and row.get("OrderStatus") not in TERMINAL and (offset is None or row.get("CombOffsetFlag", "")[0:1] == offset))

    def close_available(self, key, direction, offset, exclude=None):
        exchange = key.split(".")[0]
        position_direction = "3" if direction == "0" else "2"
        rows = [r for r in self.positions if f"{r.get('ExchangeID')}.{r.get('InstrumentID')}" == key and r.get("PosiDirection") == position_direction and r.get("HedgeFlag") == "1"]
        if exchange in {"SHFE", "INE"}:
            date = "1" if offset == "3" else "2"
            rows = [r for r in rows if r.get("PositionDate") == date]
        frozen_field = "LongFrozen" if direction == "0" else "ShortFrozen"
        held = sum(r.get("Position", 0) for r in rows)
        frozen = sum(r.get(frozen_field, 0) for r in rows)
        pending = self.pending_quantity(key, direction, offset, exclude) if exchange in {"SHFE", "INE"} else sum(self.pending_quantity(key, direction, flag, exclude) for flag in ("1", "3", "4"))
        return max(0, held - max(frozen, pending))

    def submit(self, key, side, offset_name, ticks, quantity, group=1, strategy="limit", stop_id=""):
        self.check_trading()
        if type(group) is not int or not 1 <= group <= 20 or strategy != "limit":
            raise ValueError("组别无效或订单策略尚未核验")
        if offset_name != "开仓":
            direction = "0" if side == "买" else "1"
            pending = sum(max(0, row.get("VolumeTotalOriginal", 0) - row.get("VolumeTraded", 0)) for row in self.orders.values() if row.get("Group") == group and row.get("OrderStatus") not in TERMINAL and f"{row.get('ExchangeID')}.{row.get('InstrumentID')}" == key and row.get("Direction") == direction and row.get("CombOffsetFlag") in ({OFFSETS.get(offset_name)} if key.split(".")[0] in {"SHFE", "INE"} else {"1", "3", "4"}))
            reserved = self.stop_reserved(key, side, offset_name, group)
            if stop_id:
                stop = self.stops.stops.get(stop_id)
                if not stop or (stop.key, stop.group, stop.quantity, stop.offset) != (key, group, quantity, offset_name):
                    raise ValueError("止损关联标识与委托不匹配")
                reserved -= stop.quantity
            if reserved + pending + quantity > self.stop_held(key, "卖" if side == "买" else "买", offset_name, group):
                raise ValueError("分组可平持仓不足或已被止损占用")
        if key not in self.verified or side not in {"买", "卖"} or offset_name not in OFFSETS:
            raise ValueError("仅支持已查询核验的期货合约和明确开平方向")
        if not self.quote_fresh(key):
            raise ValueError("行情缺失、过期或交易日不一致，禁止发单")
        if type(ticks) is not int or ticks <= 0 or type(quantity) is not int or not 1 <= quantity <= self.config.max_order:
            raise ValueError("价格或单笔数量超过本地限制")
        instrument = self.catalog[key]
        if not instrument.trading_enabled or not instrument.min_order_volume <= quantity <= instrument.max_order_volume:
            raise ValueError("数量不符合接口查询的合约限价单手数范围")
        exchange, symbol = key.split(".", 1)
        direction, offset = ("0" if side == "买" else "1"), OFFSETS[offset_name]
        if offset == "1" and exchange in {"SHFE", "INE"}:
            raise ValueError("上期所/能源中心请明确选择平今或平昨")
        if offset in {"3", "4"} and exchange not in {"SHFE", "INE"}:
            raise ValueError("该交易所请使用平仓，由接口按其规则处理")
        if offset == "0":
            pending = [row for row in self.orders.values() if row.get("OrderStatus") not in TERMINAL and row.get("CombOffsetFlag") == "0"]
            self.risk.check_open(self.config.risk_limits(), self.positions, pending, quantity, group)
            if not any(finite_decimal(row.get("Available", 0)) > 0 for row in self.accounts if row.get("CurrencyID", "CNY") == "CNY"):
                raise ValueError("接口账户可用资金不足或缺失，禁止开仓")
            position_direction = "2" if direction == "0" else "3"
            held = sum(r.get("Position", 0) for r in self.positions if f"{r.get('ExchangeID')}.{r.get('InstrumentID')}" == key and r.get("PosiDirection") == position_direction)
            if held + self.pending_quantity(key, direction, "0") + quantity > self.config.max_position:
                raise ValueError("超过本地单合约单边持仓限额，含未成交占用")
        elif quantity > self.close_available(key, direction, offset):
            raise ValueError("可平持仓不足，已扣除冻结与未成交占用")
        raw = self.raw_quotes[key]["row"]
        price = self.catalog[key].tick * ticks
        for field_name, sign in (("UpperLimitPrice", 1), ("LowerLimitPrice", -1)):
            try:
                limit = finite_decimal(raw[field_name])
            except (KeyError, ValueError, ArithmeticError):
                raise ValueError("涨跌停价无效，禁止发单")
            if limit <= 0 or (price - limit) * sign > 0:
                raise ValueError("委托价格超出涨跌停")
        self.order_ref += 1
        reference = str(self.order_ref)
        if len(reference) > 12:
            raise ValueError("委托引用超出 SDK 长度")
        row = {"FrontID": self.front_id, "SessionID": self.session_id, "OrderRef": reference, "ExchangeID": exchange, "InstrumentID": symbol, "Direction": direction, "CombOffsetFlag": offset, "CombHedgeFlag": "1", "LimitPrice": str(price), "VolumeTotalOriginal": quantity, "VolumeTraded": 0, "OrderStatus": "pending", "StatusMsg": "已提交，等待接口回报", "TradingDay": self.trading_day}
        key_id = order_key(row)
        row.update(Group=group, Strategy=strategy, StopID=stop_id, LocalMDEpoch=self.md_epoch, LocalQuoteReceived=self.snapshots[key].received_monotonic, LocalPriceTick=str(self.catalog[key].tick), LocalRevision=self.revision)
        self.orders[key_id] = row
        self.ready = False
        self.durable_send("insert", row)
        return key_id

    def cancel(self, key):
        self.check_trading(require_reconciled=False)
        row = self.orders[key]
        if row.get("OrderStatus") in TERMINAL or row.get("OrderStatus") in {"pending", "a"} or key in self.canceling:
            raise ValueError("委托已终结或撤单正在处理中")
        if not row.get("OrderSysID") and (row.get("FrontID"), row.get("SessionID")) != (self.front_id, self.session_id):
            raise ValueError("缺少可用的原始委托标识，须先对账")
        token = uuid4().hex
        self.risk.reserve_cancel(self.config.risk_limits(), token)
        self.canceling.add(key)
        self.ready = False
        self.durable_send("cancel", row, token)

    def periodic(self):
        self.dispatch_pending()
        if not self.faulted:
            self.stops.poll()
        if time.monotonic() - self.md_state_at > 30 and self.md_status.startswith(("连接中", "已连接")):
            self.md_status = "连接/登录超时，等待重连或手动断开"
        if self.config.td_front and not self.logged_in and time.monotonic() - self.td_state_at > 30 and self.td_status.startswith(("连接中", "已连接")):
            self.td_status = "连接/认证/登录超时，禁止交易"
        if self.snapshots and all(time.monotonic() - snapshot.received_monotonic > self.config.stale_seconds for snapshot in self.snapshots.values()):
            self.md_status = "行情过期，等待有效更新"
        missing = [key for key in self.references if key not in self.snapshots and time.monotonic() - self.quote_wait_started.get(key, time.monotonic()) > self.config.stale_seconds]
        if missing:
            self.md_status = f"{len(missing)} 个合约无有效行情超时，请检查订阅、交易时段与权限"
        interval = 15 if self.ready else 2
        if self.logged_in and not self.syncing and time.monotonic() - self.last_refresh >= interval:
            self.synchronize()


    def assign_position(self, row, group, quantity):
        self.check_trading()
        key = f"{row['ExchangeID']}.{row['InstrumentID']}"
        if any(stop.key == key and stop.state not in {"filled", "canceled"} for stop in self.stops.stops.values()):
            raise ValueError("该合约存在止损占用，须先处理止损后再调整归属")
        if row not in self.positions or row.get("HedgeFlag") != "1":
            raise ValueError("仅支持已对账的投机持仓分配")
        self.risk.assign(row, group, quantity)
        self.persist()

    def stop_reserved(self, key, closing_side, offset, group):
        side = "卖" if closing_side == "买" else "买"
        return sum(stop.quantity for stop in self.stops.stops.values() if stop.key == key and stop.position_side == side and stop.offset == offset and stop.group == group and stop.state not in {"filled", "canceled"})

    def ensure_stop_subscription(self, stop):
        if stop.id not in self.stop_subscriptions:
            self.retain(stop.key)
            self.stop_subscriptions[stop.id] = stop.key

    def stop_changed(self):
        for stop in self.stops.stops.values():
            if stop.mode == "local" and stop.state in {"armed", "triggered", "canceling", "working"}:
                self.ensure_stop_subscription(stop)
        for identifier, key in list(self.stop_subscriptions.items()):
            stop = self.stops.stops.get(identifier)
            if stop is None or stop.state in {"paused", "filled", "canceled", "failed"}:
                self.release(key)
                del self.stop_subscriptions[identifier]
        self.persist()

    def stop_ready(self, key):
        self.check_trading()
        if not self.quote_fresh(key):
            raise ValueError("行情缺失、过期或交易日不一致，止损等待有效行情")

    def stop_quote(self, key):
        return self.snapshots[key]

    def stop_held(self, key, side, offset, group):
        date = ("1" if offset == "平今" else "2") if key.split(".")[0] in {"SHFE", "INE"} else None
        return self.risk.held(group, key, "2" if side == "买" else "3", date)

    def stop_conflicts(self, stop):
        direction = "1" if stop.position_side == "买" else "0"
        return [identifier for identifier, row in self.orders.items() if f"{row.get('ExchangeID')}.{row.get('InstrumentID')}" == stop.key and row.get("Direction") == direction and row.get("CombOffsetFlag") in {"1", "3", "4"} and row.get("OrderStatus") not in TERMINAL and row.get("Group") == stop.group]

    def stop_cancel_order(self, identifier):
        if identifier not in self.canceling:
            self.cancel(identifier)

    def stop_submit(self, stop, ticks):
        return self.submit(stop.key, "卖" if stop.position_side == "买" else "买", stop.offset, ticks, stop.quantity, stop.group, stop_id=stop.id)

    def stop_order_state(self, identifier):
        status = self.orders.get(identifier, {}).get("OrderStatus")
        if status == "0":
            return "filled"
        if status in TERMINAL:
            return "failed"
        return "working"

    def acceptance_report(self):
        order_fields = ("TradingDay", "ExchangeID", "InstrumentID", "OrderRef", "OrderSysID", "Direction", "CombOffsetFlag", "LimitPrice", "VolumeTotalOriginal", "VolumeTraded", "OrderStatus", "Group", "StopID")
        trade_fields = ("TradingDay", "ExchangeID", "InstrumentID", "TradeID", "OrderRef", "OrderSysID", "Direction", "OffsetFlag", "Price", "Volume")
        return dict(
            version=1, generated_at_utc=datetime.now(timezone.utc).isoformat(), environment=self.config.environment,
            trading_day=self.trading_day, md_front=self.config.md_front, td_front=self.config.td_front,
            checks=dict(logged_in=self.logged_in, settlement_confirmed=self.settled, reconciled=self.ready, faulted=self.faulted,
                        verified_futures=len(self.verified), partial_order_observed=self.metrics["partial_order_observed"] > 0,
                        open_trade_observed=any(row.get("OffsetFlag") == "0" for row in self.trades.values()),
                        close_trade_observed=any(row.get("OffsetFlag") in {"1", "3", "4"} for row in self.trades.values()),
                        canceled_app_order_observed=any(row.get("OrderStatus") == "5" and row.get("Group") for row in self.orders.values())),
            orders=[{name: row[name] for name in order_fields if name in row} for row in self.orders.values()],
            trades=[{name: row[name] for name in trade_fields if name in row} for row in self.trades.values()],
            positions=self.positions, accounts=self.accounts, risk=self.risk.state(), stops=self.stops.records(),
            market_capabilities={key: asdict(value) for key, value in self.capabilities.items()},
            limitations=["观察标志不能代替逐笔核对资金、费用、保证金与盈亏", "账户凭据不包含在报告中", "未观察到的交易场景不能标记验收通过", "服务端止损未配置时不代表已托管"])
