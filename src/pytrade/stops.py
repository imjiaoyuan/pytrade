import uuid
from dataclasses import asdict, dataclass
from typing import Protocol


class HostedStopProvider(Protocol):
    def capabilities(self) -> dict: ...
    def create(self, request: dict) -> str: ...
    def query(self, reference: str) -> dict: ...
    def cancel(self, reference: str) -> None: ...


class UnavailableHostedStops:
    def capabilities(self):
        return dict(supported=False, reason="尚未配置并验证提供托管止损的模拟服务")

    def create(self, request):
        raise ValueError(self.capabilities()["reason"])

    def query(self, reference):
        raise ValueError(self.capabilities()["reason"])

    def cancel(self, reference):
        raise ValueError(self.capabilities()["reason"])


@dataclass
class Stop:
    id: str
    key: str
    position_side: str
    offset: str
    trigger: int
    quantity: int
    group: int
    mode: str = "local"
    state: str = "paused"
    message: str = "须手动启用；本地止损退出后失效"
    triggered: bool = False
    order_id: str = ""
    reference: str = ""
    creation_attempted: bool = False


FINAL = {"filled", "canceled"}


class StopManager:
    def __init__(self, broker, records=None, provider=None):
        self.broker = broker
        self.provider = provider or UnavailableHostedStops()
        self.stops = {}
        for record in records or []:
            stop = Stop(**record)
            if not isinstance(stop.id, str) or not stop.id or stop.id in self.stops:
                raise ValueError("止损标识缺失或重复")
            if stop.mode not in {"local", "server"} or stop.position_side not in {"买", "卖"} or stop.offset not in {"平仓", "平今", "平昨"} or any(type(value) is not int or value <= 0 for value in (stop.trigger, stop.quantity, stop.group)) or stop.group > 20 or stop.state not in {"paused", "armed", "triggered", "canceling", "working", "submitted", "hosted", "canceling_server", "filled", "failed", "canceled"}:
                raise ValueError("止损记录无效")
            if stop.state not in FINAL:
                stop.state = "paused"
                stop.message = "程序重启：须查询对账后手动核对并启用"
            self.stops[stop.id] = stop

    def records(self):
        return [asdict(stop) for stop in self.stops.values()]

    def add(self, key, position_side, offset, trigger, quantity, group, mode="local"):
        if position_side not in {"买", "卖"} or offset not in {"平仓", "平今", "平昨"} or mode not in {"local", "server"}:
            raise ValueError("止损方向、开平或模式无效")
        if any(type(value) is not int or value <= 0 for value in (trigger, quantity, group)) or group > 20:
            raise ValueError("止损价格、数量或组别无效")
        if mode == "server" and not self.provider.capabilities().get("supported"):
            raise ValueError(self.provider.capabilities().get("reason", "当前服务不支持托管止损"))
        self.check_quantity(key, position_side, offset, quantity, group)
        stop = Stop(uuid.uuid4().hex, key, position_side, offset, trigger, quantity, group, mode)
        self.stops[stop.id] = stop
        self.broker.stop_changed()
        return stop

    def check_quantity(self, key, side, offset, quantity, group, exclude=None):
        reserved = sum(stop.quantity for stop in self.stops.values() if stop.id != exclude and stop.state not in FINAL and stop.key == key and stop.position_side == side and stop.offset == offset and stop.group == group)
        if reserved + quantity > self.broker.stop_held(key, side, offset, group):
            raise ValueError("止损数量超过已明确分配的持仓，或与其他止损重复占用")

    def arm(self, identifier):
        stop = self.stops[identifier]
        if stop.state in FINAL:
            raise ValueError("已终结的止损不能重新启用，请新建")
        self.broker.stop_ready(stop.key)
        if stop.order_id:
            status = self.broker.stop_order_state(stop.order_id)
            if status == "filled":
                stop.state, stop.message = "filled", "平仓已全部成交"
                self.broker.stop_changed()
                return
            raise ValueError("已有平仓委托，请先核对原委托；止损不会重复报单")
        self.check_quantity(stop.key, stop.position_side, stop.offset, stop.quantity, stop.group, stop.id)
        if stop.mode == "server":
            if stop.reference:
                result = self.provider.query(stop.reference)
                if not result.get("accepted") or not result.get("reference") == stop.reference:
                    raise ValueError("服务端未确认托管状态，禁止重复创建")
            elif stop.creation_attempted:
                result = self.provider.query(stop.id)
                if not result.get("accepted") or result.get("client_id") != stop.id or not result.get("reference"):
                    raise ValueError("托管创建结果未确认，须按原幂等标识查询，禁止重复创建")
                stop.reference = result["reference"]
            else:
                stop.creation_attempted = True
                self.broker.stop_changed()
                stop.reference = self.provider.create(asdict(stop))
                if not stop.reference:
                    raise ValueError("服务端未返回托管标识")
            stop.state, stop.message = "submitted", "托管请求已提交，等待服务端查询确认"
        else:
            stop.state, stop.message = "armed", "本地监控中；程序退出后失效"
        self.broker.stop_changed()

    def pause(self, identifier):
        stop = self.stops[identifier]
        if stop.mode == "server" and stop.reference:
            raise ValueError("服务端托管须显式撤销，暂停本地监控不会撤销托管")
        if stop.order_id:
            raise ValueError("已产生平仓委托，请在委托页处理；不能通过暂停撤回委托")
        stop.state, stop.message = "paused", "已暂停；仍保留持仓占用"
        self.broker.stop_changed()

    def cancel(self, identifier):
        stop = self.stops[identifier]
        if stop.order_id:
            self.broker.stop_ready(stop.key)
            state = self.broker.stop_order_state(stop.order_id)
            if state not in {"filled", "failed"}:
                raise ValueError("已有平仓委托，须先处理原委托；不能删除止损解除占用")
            stop.state = "filled" if state == "filled" else "canceled"
            stop.message = "原平仓委托已核对终结；剩余持仓须人工处理"
            self.broker.stop_changed()
            return
        if stop.mode == "server" and stop.creation_attempted and not stop.reference:
            raise ValueError("托管创建结果未确认，须先查询原请求，不能释放持仓占用")
        if stop.mode == "server" and stop.reference:
            self.provider.cancel(stop.reference)
            stop.state, stop.message = "canceling_server", "等待服务端确认撤销"
        else:
            stop.state, stop.message = "canceled", "已撤销本地止损"
        self.broker.stop_changed()

    def poll(self):
        before = self.records()
        for stop in list(self.stops.values()):
            if stop.state in FINAL or stop.state in {"paused", "failed"}:
                continue
            try:
                self.step(stop)
            except (ValueError, KeyError, ArithmeticError) as error:
                stop.message = str(error)
        if self.records() != before:
            self.broker.stop_changed()

    def observe(self, key, low, high):
        changed = False
        for stop in self.stops.values():
            if stop.key != key or stop.mode != "local" or stop.state not in {"armed", "triggered", "canceling"} or stop.triggered:
                continue
            hit = low <= stop.trigger if stop.position_side == "买" else high >= stop.trigger
            if hit:
                stop.triggered = True
                stop.state = "triggered"
                stop.message = "已观察到触发价，等待有效行情与对账后处理平仓"
                changed = True
        if changed:
            self.broker.stop_changed()

    def step(self, stop):
        if stop.mode == "server":
            result = self.provider.query(stop.reference)
            if result.get("reference") != stop.reference:
                raise ValueError("服务端回报标识不匹配")
            if result.get("canceled"):
                stop.state, stop.message = "canceled", "服务端已确认撤销"
            elif result.get("filled"):
                if result.get("order_id") and self.broker.stop_order_state(result["order_id"]) == "filled":
                    stop.state, stop.message = "filled", "服务端与交易接口均已确认全部成交"
                else:
                    stop.state, stop.message = "hosted", "服务端报告成交，等待交易接口核对；保留占用"
            elif result.get("accepted") and stop.state != "canceling_server":
                stop.state, stop.message = "hosted", "服务端已查询确认托管"
            return
        if stop.order_id:
            state = self.broker.stop_order_state(stop.order_id)
            if state == "filled":
                stop.state, stop.message = "filled", "平仓已全部成交"
            elif state == "failed":
                stop.state, stop.message = "failed", "平仓委托拒绝或已撤销；须人工处理剩余持仓"
            else:
                stop.message = "限价平仓等待回报；部分成交不自动追价"
            return
        self.broker.stop_ready(stop.key)
        snapshot = self.broker.stop_quote(stop.key)
        if not stop.triggered:
            hit = snapshot.last <= stop.trigger if stop.position_side == "买" else snapshot.last >= stop.trigger
            if not hit:
                stop.message = "本地监控中；程序退出后失效"
                return
            stop.triggered = True
            stop.state = "triggered"
            self.broker.stop_changed()
        conflicts = self.broker.stop_conflicts(stop)
        if conflicts:
            stop.state = "canceling"
            for identifier in conflicts:
                self.broker.stop_cancel_order(identifier)
            stop.message = "已触发，等待冲突平仓委托撤单回报及重新对账"
            return
        self.check_quantity(stop.key, stop.position_side, stop.offset, stop.quantity, stop.group, stop.id)
        levels = snapshot.bids if stop.position_side == "买" else snapshot.asks
        if not levels:
            raise ValueError("止损已触发，但缺少有效对手价；等待有效行情")
        stop.state = "working"
        stop.order_id = self.broker.stop_submit(stop, levels[0][0])
        stop.message = "限价平仓已提交，等待接口回报；不自动追价"

