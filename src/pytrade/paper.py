import time
from datetime import datetime
from decimal import Decimal

from .domain import ACTIVE, Fill, Status
from .external import finite_decimal


class PaperSimulator:
    def __init__(self, ledger, session):
        if not session.config.paper_trading or session.config.td_front or session.config.allow_orders:
            raise ValueError("在线模拟必须使用无交易接口的行情连接")
        self.ledger = ledger
        self.session = session
        self.paused = False
        self.sequence = 0
        self.tokens = {}
        self.books = {}
        self.retained = set()
        self.error = ""
        self.sync_references()

    def check_ready(self, key, allow_paused=False):
        if not self.session.config.paper_trading or self.session.config.td_front or self.session.config.allow_orders:
            raise ValueError("在线模拟配置已改变，禁止撮合")
        if self.ledger.trading_error:
            raise ValueError(self.ledger.trading_error)
        if (self.paused and not allow_paused) or self.session.faulted or getattr(self.session.gateway, "closing", False):
            raise ValueError("在线模拟已暂停或行情连接故障，禁止撮合")
        snapshot = self.session.snapshots.get(key)
        if snapshot is None or not 0 <= time.monotonic() - snapshot.received_monotonic <= self.session.config.stale_seconds:
            raise ValueError("真实行情缺失或过期，暂停在线模拟；不会使用离线生成报价")
        try:
            datetime.strptime(snapshot.trading_day, "%Y%m%d")
        except ValueError:
            raise ValueError("行情交易日无效，暂停在线模拟") from None
        if self.ledger.risk_day and snapshot.trading_day < self.ledger.risk_day:
            raise ValueError("行情交易日落后，暂停在线模拟")
        instrument = self.session.catalog[key]
        if instrument.product_class != "1" or (instrument.expire_date and instrument.expire_date < snapshot.trading_day):
            raise ValueError("在线模拟仅支持未到期的期货普通限价单")
        original = self.ledger.catalog.get(key)
        if original and (original.tick, original.multiplier) != (instrument.tick, instrument.multiplier):
            raise ValueError("合约参数已改变，请新建模拟会话后再交易")
        return snapshot

    def submit(self, key, side, offset, ticks, quantity, group=1, stop_id=""):
        self.check_ready(key)
        instrument = self.session.catalog[key]
        raw = self.session.raw_quotes[key]["row"]
        try:
            lower, upper = (finite_decimal(raw[name]) for name in ("LowerLimitPrice", "UpperLimitPrice"))
        except (KeyError, ValueError, ArithmeticError):
            raise ValueError("涨跌停价无效，禁止在线模拟报单") from None
        if type(ticks) is not int or not 0 < lower <= instrument.tick * ticks <= upper:
            raise ValueError("模拟委托价格超出有效涨跌停范围")
        self.ledger.catalog.setdefault(key, instrument)
        order = self.ledger.submit(key, side, offset, ticks, quantity, group, stop_id)
        order.message = "在线模拟本地委托，等待下一次真实报价受理；不发送外部委托"
        self.sync_references()
        return order

    def accept(self, snapshot, epoch):
        key = snapshot.key
        token = (epoch, snapshot.received_monotonic)
        previous = self.tokens.get(key)
        if self.session.snapshots.get(key) is not snapshot or epoch != self.session.md_epoch or (previous and previous[0] == epoch and previous[1] >= snapshot.received_monotonic):
            return False
        try:
            self.check_ready(key, allow_paused=True)
        except ValueError as error:
            self.error = str(error)
            return False
        if self.ledger.risk_day and snapshot.trading_day > self.ledger.risk_day:
            self.suspend("交易日切换，旧模拟挂单已撤销；持仓保留")
        self.ledger.risk_day = snapshot.trading_day
        self.tokens[key] = token
        self.ledger.catalog.setdefault(key, self.session.catalog[key])
        self.ledger.marks[key] = snapshot.last
        self.ledger.update_risk()
        self.error = ""
        previous = self.books.get(key, {})
        book = {}
        for side, levels in (("买", snapshot.asks), ("卖", snapshot.bids)):
            for price, quantity in levels:
                old = previous.get((side, price))
                remaining = old[1] if old and old[0] == quantity and old[2] == epoch else quantity
                book[(side, price)] = [quantity, remaining, epoch]
        self.books[key] = book
        self.sequence += 1
        if self.paused:
            return True
        for order in list(self.ledger.orders.values()):
            if order.key != key:
                continue
            if order.status == Status.PENDING:
                self.ledger.acknowledge(order.id, message="在线模拟本地受理，等待后续真实报价撮合")
                continue
            if order.status not in {Status.WORKING, Status.PARTIAL}:
                continue
            levels = snapshot.asks if order.side == "买" else snapshot.bids
            for price, _ in levels:
                if not (order.ticks >= price if order.side == "买" else order.ticks <= price):
                    break
                level = book[(order.side, price)]
                quantity = min(order.remaining, level[1])
                if quantity:
                    self.ledger.apply_fill(Fill(f"PAPER:{epoch}:{self.sequence}:{order.id}:{price}", order.id, key, order.side, order.offset, price, quantity))
                    order.message = "在线模拟本地成交，不是交易所成交；不含手续费和保证金"
                    level[1] -= quantity
                if not order.remaining:
                    break
        self.ledger.update_risk()
        self.sync_references()
        return True

    def sync_references(self):
        wanted = {order.key for order in self.ledger.orders.values() if order.status in ACTIVE}
        wanted.update(key for (key, _), position in self.ledger.positions.items() if position.quantity)
        for key in wanted - self.retained:
            previous = self.session.references.get(key, 0)
            try:
                self.session.retain(key)
            except ValueError as error:
                self.error = str(error)
            if self.session.references.get(key, 0) > previous:
                self.retained.add(key)
        for key in self.retained - wanted:
            self.retained.remove(key)
            try:
                self.session.release(key)
            except ValueError as error:
                self.error = str(error)

    def suspend(self, reason):
        self.tokens.clear()
        self.books.clear()
        for order in self.ledger.orders.values():
            if order.status in ACTIVE:
                order.status, order.message = Status.CANCELED, reason
        if self.ledger.stop_manager:
            for stop in self.ledger.stop_manager.stops.values():
                if stop.state not in {"filled", "canceled", "failed"}:
                    stop.state, stop.message = "paused", reason + "；止损须手动重新启用"
        self.error = reason
        self.sync_references()

    def unrealized(self):
        return sum(((Decimal(self.ledger.marks.get(key, self.ledger.catalog[key].initial_ticks)) * position.quantity - position.cost_ticks) * self.ledger.catalog[key].tick * self.ledger.catalog[key].multiplier * (1 if side == "买" else -1) for (key, side), position in self.ledger.positions.items()), Decimal("0"))
