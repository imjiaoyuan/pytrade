import os
import math
import queue
import sys
import threading
import time
from pathlib import Path

from .external import order_key, redact
from .protocol import VERSION, command_message


FIELDS = {
    "quote": ("InstrumentID", "ExchangeID", "TradingDay", "ActionDay", "UpdateTime", "UpdateMillisec", "LastPrice", "Volume", "UpperLimitPrice", "LowerLimitPrice") + tuple(f"{side}{field}{level}" for level in range(1, 6) for side in ("Bid", "Ask") for field in ("Price", "Volume")),
    "instruments": ("InstrumentID", "ExchangeID", "InstrumentName", "PriceTick", "VolumeMultiple", "ExpireDate", "IsTrading", "ProductClass", "MaxLimitOrderVolume", "MinLimitOrderVolume"),
    "orders": ("InstrumentID", "ExchangeID", "FrontID", "SessionID", "OrderRef", "OrderSysID", "Direction", "CombOffsetFlag", "CombHedgeFlag", "LimitPrice", "VolumeTotalOriginal", "VolumeTraded", "OrderStatus", "OrderSubmitStatus", "StatusMsg", "TradingDay", "SequenceNo", "InsertTime"),
    "trades": ("InstrumentID", "ExchangeID", "OrderRef", "OrderSysID", "TradeID", "TradingDay", "Direction", "OffsetFlag", "HedgeFlag", "Price", "Volume", "TradeTime"),
    "positions": ("InstrumentID", "ExchangeID", "PosiDirection", "HedgeFlag", "PositionDate", "Position", "TodayPosition", "YdPosition", "LongFrozen", "ShortFrozen", "PositionCost", "OpenCost", "PositionProfit", "CloseProfit", "Commission", "UseMargin", "TradingDay"),
    "account": ("CurrencyID", "Balance", "Available", "CurrMargin", "FrozenMargin", "Commission", "CloseProfit", "PositionProfit", "TradingDay", "Deposit", "Withdraw", "PreBalance"),
    "settlement": ("Content",),
}
QUERY_TYPES = {
    "instruments": ("CThostFtdcQryInstrumentField", "ReqQryInstrument"),
    "orders": ("CThostFtdcQryOrderField", "ReqQryOrder"),
    "trades": ("CThostFtdcQryTradeField", "ReqQryTrade"),
    "positions": ("CThostFtdcQryInvestorPositionField", "ReqQryInvestorPosition"),
    "account": ("CThostFtdcQryTradingAccountField", "ReqQryTradingAccount"),
    "settlement": ("CThostFtdcQrySettlementInfoField", "ReqQrySettlementInfo"),
}


def copied(data, kind):
    if data is None:
        return None
    return {field: getattr(data, field) for field in FIELDS[kind]}


def protected(method):
    def invoke(self, *args):
        try:
            with self.engine.lock:
                return method(self, *args)
        except Exception as error:
            self.engine.fatal(f"SDK 回调异常：{type(error).__name__}")
    return invoke


def make_market_spi(module, engine):
    class MarketSpi(module.CThostFtdcMdSpi):
        def __init__(self):
            super().__init__()
            self.engine = engine

        @protected
        def OnFrontConnected(self):
            engine.md_logged_in = False
            engine.md_epoch += 1
            engine.subscribed.clear()
            engine.requested.clear()
            engine.requested_at.clear()
            engine.retry_at.clear()
            engine.retry_counts.clear()
            engine.quotes.clear()
            engine.emit("md_state", status="已连接，登录中", epoch=engine.md_epoch)
            request = module.CThostFtdcReqUserLoginField()
            if not engine.config.md_anonymous:
                request.BrokerID, request.UserID, request.Password = engine.config.market_credentials()
            engine.request(engine.md.ReqUserLogin, request, "md")

        @protected
        def OnFrontDisconnected(self, reason):
            engine.md_logged_in = False
            engine.md_epoch += 1
            engine.subscribed.clear()
            engine.requested.clear()
            engine.requested_at.clear()
            engine.retry_at.clear()
            engine.retry_counts.clear()
            engine.quotes.clear()
            engine.emit("md_state", status=f"断线({reason})，等待重连", epoch=engine.md_epoch)

        @protected
        def OnRspUserLogin(self, login, info, request_id, last):
            if engine.response_error(info, "md"):
                engine.emit("md_state", status="登录失败", epoch=engine.md_epoch)
                return
            engine.md_logged_in = True
            engine.emit("md_state", status="登录成功，等待订阅行情", epoch=engine.md_epoch)
            engine.update_subscriptions()

        @protected
        def OnRspSubMarketData(self, instrument, info, request_id, last):
            symbol = instrument.InstrumentID if instrument else ""
            for key in list(engine.wanted):
                if key.split(".", 1)[1] == symbol:
                    error = info.ErrorID if info else 0
                    engine.requested_at.pop(key, None)
                    engine.emit("subscription", key=key, error=error, epoch=engine.md_epoch)
                    if not error:
                        engine.subscribed.add(key)
                        engine.retry_at.pop(key, None)
                        engine.retry_counts.pop(key, None)
                    else:
                        engine.requested.discard(key)
                        engine.subscription_failed(key)

        @protected
        def OnRtnDepthMarketData(self, data):
            row = copied(data, "quote")
            for key in list(engine.wanted):
                exchange, symbol = key.split(".", 1)
                if symbol == row["InstrumentID"] and (not row["ExchangeID"] or row["ExchangeID"] == exchange):
                    with engine.lock:
                        old = engine.quotes.get(key, {})
                        last_price = row["LastPrice"]
                        prices = [last_price] if math.isfinite(last_price) and 0 < last_price < 1e50 else []
                        if old.get("row", {}).get("TradingDay") == row["TradingDay"]:
                            prices.extend(old[field] for field in ("range_low", "range_high") if field in old)
                        message = {"version": VERSION, "kind": "quote", "key": key, "epoch": engine.md_epoch, "row": row, "received": time.monotonic()}
                        if prices:
                            message.update(range_low=min(prices), range_high=max(prices))
                        engine.quotes[key] = message

        @protected
        def OnRspError(self, info, request_id, last):
            engine.response_error(info, "md")

    return MarketSpi()


def make_trade_spi(module, engine):
    class TradeSpi(module.CThostFtdcTraderSpi):
        def __init__(self):
            super().__init__()
            self.engine = engine

        @protected
        def OnFrontConnected(self):
            engine.td_logged_in = False
            engine.settled = False
            engine.reconciled = False
            engine.clear_queries()
            engine.emit("td_state", status="已连接，认证/登录中")
            if engine.config.app_id:
                request = module.CThostFtdcReqAuthenticateField()
                request.BrokerID = engine.config.broker
                request.UserID = engine.config.user
                request.AppID = engine.config.app_id
                request.AuthCode = engine.config.auth_code
                engine.request(engine.td.ReqAuthenticate, request, "td")
            else:
                engine.trade_login()

        @protected
        def OnFrontDisconnected(self, reason):
            engine.td_logged_in = False
            engine.reconciled = False
            engine.settled = False
            engine.clear_queries()
            engine.emit("td_state", status=f"断线({reason})，等待重连与对账")

        @protected
        def OnRspAuthenticate(self, data, info, request_id, last):
            if not engine.response_error(info, "td"):
                engine.trade_login()

        @protected
        def OnRspUserLogin(self, login, info, request_id, last):
            if engine.response_error(info, "td") or not login:
                return
            engine.td_logged_in = True
            engine.front_id = login.FrontID
            engine.session_id = login.SessionID
            engine.trading_day = login.TradingDay
            engine.emit("td_login", front=login.FrontID, session=login.SessionID, max_ref=login.MaxOrderRef, day=login.TradingDay)

        @protected
        def OnRspSettlementInfoConfirm(self, data, info, request_id, last):
            if not engine.response_error(info, "td") and last:
                engine.settled = True
                engine.emit("settled")

        @protected
        def OnRtnOrder(self, data):
            engine.reconciled = False
            row = copied(data, "orders")
            row["StatusMsg"] = redact(row["StatusMsg"], engine.config)
            engine.emit("order", row=row)

        @protected
        def OnRtnTrade(self, data):
            engine.reconciled = False
            engine.emit("trade", row=copied(data, "trades"))

        @protected
        def OnRspOrderInsert(self, data, info, request_id, last):
            if info and info.ErrorID:
                engine.insert_error(data, info, request_id)

        @protected
        def OnErrRtnOrderInsert(self, data, info):
            engine.insert_error(data, info)

        @protected
        def OnRspOrderAction(self, data, info, request_id, last):
            if info and info.ErrorID:
                engine.cancel_error(data, info, request_id)

        @protected
        def OnErrRtnOrderAction(self, data, info):
            engine.cancel_error(data, info)

        @protected
        def OnRspError(self, info, request_id, last):
            engine.response_error(info, "td")
            with engine.lock:
                if engine.query and engine.query["id"] == request_id:
                    engine.query["error"] = True
                    engine.query["done"] = True

    def query_callback(kind):
        @protected
        def callback(self, data, info, request_id, last):
            with engine.lock:
                active = engine.query
                if not active or active["id"] != request_id:
                    return
                if info and info.ErrorID:
                    active["error"] = True
                    engine.emit("error", channel="td", message=f"{kind} 查询失败，代码 {info.ErrorID}")
                if data:
                    row = copied(data, kind)
                    if kind == "orders":
                        row["StatusMsg"] = redact(row["StatusMsg"], engine.config)
                    if kind == "settlement":
                        row["Content"] = redact(row["Content"], engine.config)
                    if len(active["rows"]) >= 100000:
                        engine.fatal("查询超过缓存上限，需重新连接")
                    else:
                        active["rows"].append(row)
                if last:
                    active["done"] = True
        return callback

    for name, kind in (("OnRspQryInstrument", "instruments"), ("OnRspQryOrder", "orders"), ("OnRspQryTrade", "trades"), ("OnRspQryInvestorPosition", "positions"), ("OnRspQryTradingAccount", "account"), ("OnRspQrySettlementInfo", "settlement")):
        setattr(TradeSpi, name, query_callback(kind))
    return TradeSpi()


def build_insert(module, config, row):
    request = module.CThostFtdcInputOrderField()
    values = {"BrokerID": config.broker, "InvestorID": config.user, "UserID": config.user, "ExchangeID": row["ExchangeID"], "InstrumentID": row["InstrumentID"], "OrderRef": row["OrderRef"], "Direction": row["Direction"], "CombOffsetFlag": row["CombOffsetFlag"], "CombHedgeFlag": module.THOST_FTDC_HF_Speculation, "OrderPriceType": module.THOST_FTDC_OPT_LimitPrice, "LimitPrice": float(row["LimitPrice"]), "VolumeTotalOriginal": row["VolumeTotalOriginal"], "TimeCondition": module.THOST_FTDC_TC_GFD, "VolumeCondition": module.THOST_FTDC_VC_AV, "MinVolume": 1, "ContingentCondition": module.THOST_FTDC_CC_Immediately, "ForceCloseReason": module.THOST_FTDC_FCC_NotForceClose, "IsAutoSuspend": 0, "UserForceClose": 0}
    for name, value in values.items():
        setattr(request, name, value)
    return request


def build_cancel(module, config, row):
    request = module.CThostFtdcInputOrderActionField()
    values = {"BrokerID": config.broker, "InvestorID": config.user, "UserID": config.user, "ExchangeID": row["ExchangeID"], "InstrumentID": row["InstrumentID"], "OrderRef": row["OrderRef"], "OrderSysID": row.get("OrderSysID", ""), "FrontID": row["FrontID"], "SessionID": row["SessionID"], "ActionFlag": module.THOST_FTDC_AF_Delete}
    for name, value in values.items():
        setattr(request, name, value)
    return request


class Engine:
    def __init__(self, config, outgoing, flow, md_module, td_module=None):
        self.config = config
        self.outgoing = outgoing
        self.flow = flow
        self.md_module, self.td_module = md_module, td_module
        self.lock = threading.RLock()
        self.failed = threading.Event()
        self.fatal_sent = False
        self.sequence = 0
        self.md_epoch = 0
        self.md_logged_in = False
        self.td_logged_in = False
        self.settled = False
        self.reconciled = False
        self.front_id = 0
        self.session_id = 0
        self.trading_day = ""
        self.wanted = set()
        self.requested = set()
        self.requested_at = {}
        self.retry_at = {}
        self.retry_counts = {}
        self.subscribed = set()
        self.quotes = {}
        self.query = None
        self.query_queue = []
        self.sync = None
        self.next_query = 0.0
        self.insert_requests = {}
        self.cancel_requests = {}
        self.md = None
        self.td = None
        self.md_spi = None
        self.td_spi = None

    def emit(self, kind, **values):
        try:
            self.outgoing.put_nowait({"version": VERSION, "kind": kind, **values})
        except queue.Full:
            self.failed.set()
            self.reconciled = False

    def fatal(self, message):
        self.failed.set()
        self.reconciled = False
        self.emit("fatal", message=message)

    def request_id(self):
        with self.lock:
            self.sequence += 1
            return self.sequence

    def response_error(self, info, channel):
        if info and info.ErrorID:
            self.emit("error", channel=channel, message=f"{channel} 接口错误 {info.ErrorID}: {redact(info.ErrorMsg, self.config)}")
            return True
        return False

    def request(self, method, request, channel):
        request_id = self.request_id()
        result = method(request, request_id)
        if result:
            self.emit("error", channel=channel, message=f"{channel} 请求未发送，代码 {result}")
        return result

    def start(self):
        self.config.validate()
        self.md = self.md_module.CThostFtdcMdApi.CreateFtdcMdApi(str(self.flow / "md") + os.sep)
        self.md_spi = make_market_spi(self.md_module, self)
        self.md.RegisterSpi(self.md_spi)
        self.md.RegisterFront(self.config.md_front)
        if self.config.td_front:
            module = self.td_module
            self.td = module.CThostFtdcTraderApi.CreateFtdcTraderApi(str(self.flow / "td") + os.sep)
            self.td_spi = make_trade_spi(module, self)
            self.td.RegisterSpi(self.td_spi)
            self.td.SubscribePrivateTopic(module.THOST_TERT_QUICK)
            self.td.SubscribePublicTopic(module.THOST_TERT_QUICK)
            self.td.RegisterFront(self.config.td_front)
            self.td.Init()
        self.md.Init()

    def trade_login(self):
        request = self.td_module.CThostFtdcReqUserLoginField()
        request.BrokerID = self.config.broker
        request.UserID = self.config.user
        request.Password = self.config.password
        self.request(self.td.ReqUserLogin, request, "td")

    def update_subscriptions(self):
        if not self.md_logged_in or not self.md:
            return
        now = time.monotonic()
        for key, requested in list(self.requested_at.items()):
            if key in self.wanted and now - requested >= 10:
                self.requested.discard(key)
                self.requested_at.pop(key, None)
                self.subscription_failed(key)
                self.emit("subscription", key=key, error=-10001, epoch=self.md_epoch)
        removed = {key for key in self.requested - self.wanted if time.monotonic() >= self.retry_at.get(key, 0)}
        added = {key for key in self.wanted - self.requested if time.monotonic() >= self.retry_at.get(key, 0)}
        for keys, method in ((removed, self.md.UnSubscribeMarketData), (added, self.md.SubscribeMarketData)):
            if not keys:
                continue
            symbols = [key.split(".", 1)[1].encode("ascii") for key in keys]
            result = method(symbols, len(symbols))
            if result:
                for key in keys:
                    self.emit("subscription", key=key, error=result, epoch=self.md_epoch)
                    self.subscription_failed(key)
            elif keys is added:
                self.requested.update(keys)
                for key in keys - self.subscribed:
                    self.requested_at.setdefault(key, now)
            else:
                self.requested.difference_update(keys)
                self.subscribed.difference_update(keys)
                for key in keys:
                    self.requested_at.pop(key, None)
                    self.retry_at.pop(key, None)
                    self.retry_counts.pop(key, None)

    def subscription_failed(self, key):
        count = min(self.retry_counts.get(key, 0) + 1, 5)
        self.retry_counts[key] = count
        self.retry_at[key] = time.monotonic() + min(30, 2 ** count)

    def clear_queries(self):
        with self.lock:
            self.query = None
            self.query_queue = []
            self.sync = None

    def command(self, message):
        with self.lock:
            self.handle_command(message)

    def handle_command(self, message):
        try:
            message = command_message(message)
        except ValueError as error:
            self.fatal(str(error))
            return
        name = message["command"]
        if name == "probe_market_disconnect":
            from .market_transport_probe import drop_own_market_connection
            try:
                count = drop_own_market_connection(self.config)
                self.emit("probe_fault", connections_closed=count)
            except (ValueError, OSError) as error:
                self.emit("error", channel="md", message=str(error))
        elif name == "subscriptions":
            self.wanted = set(message["keys"])
            self.update_subscriptions()
        elif name == "sync":
            if not self.td_logged_in or self.sync:
                return
            self.reconciled = False
            self.sync = message["cycle"]
            self.query_queue = (["instruments", "settlement"] if message.get("instruments") else []) + ["orders", "trades", "positions", "account"]
        elif name == "confirm_settlement":
            if not self.td_logged_in or self.sync:
                self.emit("error", channel="td", message="请等待登录及查询完成后确认结算")
                return
            request = self.td_module.CThostFtdcSettlementInfoConfirmField()
            request.BrokerID = self.config.broker
            request.InvestorID = self.config.user
            self.request(self.td.ReqSettlementInfoConfirm, request, "td")
        elif name in {"insert", "cancel"}:
            row = message["row"]
            key = order_key(row)
            if not (not self.config.paper_trading and self.config.environment == "simulation" and self.config.allow_orders and self.td_logged_in and self.settled and (name == "cancel" or self.reconciled) and not self.failed.is_set()):
                self.emit("insert_error" if name == "insert" else "cancel_error", order_key=key, message="SDK 交易保护：只读、未对账或连接状态已改变", not_sent=True, cancel_token=message.get("cancel_token", ""))
                return
            if name == "insert":
                if (row.get("TradingDay"), row.get("FrontID"), row.get("SessionID")) != (self.trading_day, self.front_id, self.session_id):
                    self.emit("insert_error", order_key=key, message="SDK 交易保护：报单所属交易日或登录会话已改变，未发送", not_sent=True)
                    return
                received = message.get("quote_received")
                if not self.md_logged_in or message.get("quote_epoch") != self.md_epoch or type(received) not in {int, float} or not math.isfinite(received) or not 0 <= time.monotonic() - received <= self.config.stale_seconds:
                    self.emit("insert_error", order_key=key, message="SDK 交易保护：行情已断开、连接代号不同或报价过期，请重新选择价格", not_sent=True)
                    return
            request_id = self.request_id()
            if name == "insert":
                self.insert_requests[request_id] = key
                request = build_insert(self.td_module, self.config, row)
                result = self.td.ReqOrderInsert(request, request_id)
                self.reconciled = False
            else:
                self.cancel_requests[request_id] = key
                request = build_cancel(self.td_module, self.config, row)
                result = self.td.ReqOrderAction(request, request_id)
            if result:
                self.emit("insert_error" if name == "insert" else "cancel_error", order_key=key, message=f"请求未发送，代码 {result}", not_sent=True, cancel_token=message.get("cancel_token", ""))

    def insert_error(self, data, info, request_id=None):
        key = self.insert_requests.get(request_id)
        if key is None and data:
            key = f"{self.trading_day}:{self.front_id}:{self.session_id}:{data.OrderRef.strip()}"
        self.emit("insert_error", order_key=key or "", message=f"报单失败 {info.ErrorID if info else ''}: {redact(info.ErrorMsg if info else '', self.config)}")

    def cancel_error(self, data, info, request_id=None):
        key = self.cancel_requests.get(request_id)
        if key is None and data:
            key = f"{self.trading_day}:{data.FrontID}:{data.SessionID}:{data.OrderRef.strip()}"
        self.emit("cancel_error", order_key=key or "", message=f"撤单失败 {info.ErrorID if info else ''}: {redact(info.ErrorMsg if info else '', self.config)}")

    def pump(self):
        if self.failed.is_set():
            if not self.fatal_sent:
                try:
                    self.outgoing.put_nowait({"kind": "fatal", "message": "SDK 事件处理故障或队列积压，禁止交易；需断开后重新查询对账"})
                    self.fatal_sent = True
                except queue.Full:
                    pass
            return
        with self.lock:
            self.update_subscriptions()
            quotes = list(self.quotes.values())
            self.quotes.clear()
            active = self.query
            if active and not active["done"] and time.monotonic() - active["started"] > 60:
                active["error"] = True
                active["done"] = True
                self.emit("error", channel="td", message=f"{active['kind']} 查询超时，须重新对账")
            if active and active["done"]:
                self.query = None
                if active["error"]:
                    self.query_queue = []
                    self.sync = None
                else:
                    for start in range(0, max(1, len(active["rows"])), 200):
                        self.emit("query_rows", query=active["kind"], cycle=active["cycle"], rows=active["rows"][start:start + 200])
                    if not self.query_queue:
                        cycle = self.sync
                        self.sync = None
                        self.reconciled = True
                        self.emit("sync_done", cycle=cycle)
            if self.sync and self.query is None and self.query_queue and time.monotonic() >= self.next_query:
                kind = self.query_queue.pop(0)
                class_name, method_name = QUERY_TYPES[kind]
                request = getattr(self.td_module, class_name)()
                if hasattr(request, "BrokerID"):
                    request.BrokerID = self.config.broker
                if hasattr(request, "InvestorID"):
                    request.InvestorID = self.config.user
                request_id = self.request_id()
                self.query = {"id": request_id, "kind": kind, "cycle": self.sync, "rows": [], "done": False, "error": False, "started": time.monotonic()}
                result = getattr(self.td, method_name)(request, request_id)
                self.next_query = time.monotonic() + 1.1
                if result:
                    self.query["done"] = True
                    self.query["error"] = True
                    self.emit("error", channel="td", message=f"{kind} 查询未发送，代码 {result}")
        for quote in quotes:
            try:
                self.outgoing.put_nowait(quote)
            except queue.Full:
                with self.lock:
                    self.quotes.setdefault(quote["key"], quote)

    def close(self):
        for api in (self.md, self.td):
            if api:
                api.RegisterSpi(None)
                api.Release()


def worker_main(config, incoming, outgoing, flow_path, locale_path):
    engine = None
    try:
        from .transport import check_market_transport
        config.validate()
        failure = check_market_transport(config.md_front)
        if failure:
            outgoing.put({"kind": "fatal", "message": failure}, timeout=0.2)
            return
        outgoing.put({"kind": "md_state", "status": "连接中：TCP 可达，等待 CTP 前置连接及登录回调", "epoch": 0}, timeout=0.2)
        if sys.platform == "linux" and Path(locale_path).is_dir():
            os.environ.setdefault("LOCPATH", str(Path(locale_path)))
        from openctp_ctp import mdapi
        tdapi = None
        if config.td_front:
            from openctp_ctp import tdapi
        flow = Path(flow_path)
        flow.mkdir(parents=True, exist_ok=True, mode=0o700)
        for child in ("md", "td"):
            (flow / child).mkdir(exist_ok=True, mode=0o700)
        engine = Engine(config, outgoing, flow, mdapi, tdapi)
        engine.start()
        running = True
        while running:
            for _ in range(50):
                try:
                    message = incoming.get_nowait()
                except queue.Empty:
                    break
                if message["command"] == "stop":
                    running = False
                    break
                engine.command(message)
            engine.pump()
            time.sleep(0.02)
    except BaseException as error:
        try:
            outgoing.put({"kind": "fatal", "message": f"CTP 进程启动/运行失败：{type(error).__name__}；检查依赖、locale 与配置"}, timeout=0.2)
        except queue.Full:
            pass
    finally:
        if engine:
            engine.close()
