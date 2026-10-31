from .domain import ACTIVE, Status
from .records import Journal, load_journal
from .stops import StopManager


class OfflineStops:
    def __init__(self, controller, online=False):
        self.controller = controller
        self.online = online
        self.path = controller.state_dir / ("paper_stops.json" if online else "offline_stops.json")
        self.writer = None
        records = []
        self.error = ""
        if self.path.exists():
            try:
                records = load_journal(self.path).get("stops", [])
            except (OSError, ValueError, TypeError):
                self.error = "离线止损记录损坏，请保留文件并核对"
        try:
            self.stops = StopManager(self, records)
        except (ValueError, TypeError, KeyError):
            self.error = "离线止损记录无效，请保留文件并核对"
            self.stops = StopManager(self)

    def stop_ready(self, key):
        if self.online:
            if not self.controller.paper or self.error:
                raise ValueError(self.error or "在线模拟会话已改变")
            self.controller.paper.check_ready(key)
        elif self.error or self.controller.external or self.controller.paused or key not in self.controller.simulator.snapshots:
            raise ValueError(self.error or "离线行情暂停或模式已改变，本地止损暂停")
        if self.writer and self.writer.error:
            raise ValueError(self.writer.error)

    def stop_quote(self, key):
        return self.controller.paper.check_ready(key) if self.online else self.controller.simulator.snapshots[key]

    def stop_held(self, key, side, offset, group):
        if offset != "平仓":
            raise ValueError("本地模拟使用平仓，不区分今昨仓")
        return self.controller.ledger.risk.held(group, key, "2" if side == "买" else "3")

    def stop_conflicts(self, stop):
        direction = "卖" if stop.position_side == "买" else "买"
        return [order.id for order in self.controller.ledger.orders.values() if order.key == stop.key and order.side == direction and order.offset == "平仓" and order.group == stop.group and order.status in ACTIVE]

    def stop_cancel_order(self, identifier):
        self.controller.ledger.cancel_report(identifier)

    def stop_submit(self, stop, ticks):
        if self.online:
            return self.controller.paper.submit(stop.key, "卖" if stop.position_side == "买" else "买", "平仓", ticks, stop.quantity, stop.group, stop.id).id
        return self.controller.ledger.submit(stop.key, "卖" if stop.position_side == "买" else "买", "平仓", ticks, stop.quantity, stop.group, stop.id).id

    def stop_order_state(self, identifier):
        order = self.controller.ledger.orders.get(identifier)
        if order is None:
            return "failed"
        if order.status == Status.FILLED:
            return "filled"
        return "failed" if order.status not in ACTIVE else "working"

    def stop_changed(self):
        if self.error:
            raise ValueError(self.error)
        if self.writer is None:
            self.writer = Journal(self.path)
        try:
            self.writer.submit(dict(version=2, orders={}, trades={}, stops=self.stops.records()))
        except ValueError as error:
            self.error = str(error)
            self.controller.ledger.trading_error = self.error
            raise

    def poll(self):
        if self.writer and self.writer.error:
            self.error = self.writer.error
        if self.error:
            self.controller.ledger.trading_error = self.error
            return
        try:
            self.stops.poll()
        except ValueError as error:
            self.error = str(error)
            self.controller.ledger.trading_error = self.error
            self.controller.log(self.error)

    def close(self):
        for stop in self.stops.stops.values():
            if stop.state not in {"filled", "canceled", "failed"}:
                stop.state, stop.message = "paused", "程序退出后本地止损失效"
        if self.writer:
            try:
                self.stop_changed()
            finally:
                self.writer.close()
                self.writer = None
