from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from datetime import date
from uuid import uuid4

from .risk import RiskBook, RiskLimits


@dataclass(frozen=True)
class Instrument:
    key: str
    name: str
    tick: Decimal
    multiplier: int
    initial_ticks: int
    depth: int = 5
    expire_date: str = ""
    source: str = "offline"
    fetched_at: str = ""
    trading_day: str = ""
    product_class: str = "1"
    min_order_volume: int = 1
    max_order_volume: int = 0
    trading_enabled: bool = False

    def price(self, ticks: int) -> str:
        return format(self.tick * ticks, "f")

    def ticks(self, value: str) -> int:
        number = Decimal(value) / self.tick
        if not number.is_finite() or number != number.to_integral_value() or number <= 0:
            raise ValueError("价格必须为正数且对齐最小变动价位")
        return int(number)


INSTRUMENTS = (
    Instrument("SHFE.ag2612", "白银", Decimal("1"), 15, 15006),
    Instrument("SHFE.au2612", "黄金", Decimal("0.02"), 1000, 45825),
    Instrument("SHFE.cu2612", "沪铜", Decimal("10"), 5, 8530),
    Instrument("DCE.i2701", "铁矿石", Decimal("0.5"), 100, 1596, 1),
    Instrument("CZCE.MA701", "甲醇", Decimal("1"), 10, 2250, 1),
)
CATALOG = {item.key: item for item in INSTRUMENTS}


@dataclass(frozen=True)
class Snapshot:
    key: str
    last: int
    bids: tuple[tuple[int, int], ...]
    asks: tuple[tuple[int, int], ...]
    volume: int
    time: str
    source: str = "offline"
    trading_day: str = ""
    action_day: str = ""
    received_monotonic: float = 0.0
    pre_settlement: Decimal | None = None
    open_interest: Decimal | None = None
    pre_open_interest: Decimal | None = None
    upper_limit: int | None = None
    lower_limit: int | None = None


@dataclass
class MarketCapability:
    source: str
    key: str
    observed_max_depth: int = 0
    current_bid_depth: int = 0
    current_ask_depth: int = 0
    observed_at: str = ""

    def observe(self, snapshot, timestamp):
        self.current_bid_depth = len(snapshot.bids)
        self.current_ask_depth = len(snapshot.asks)
        self.observed_max_depth = max(self.observed_max_depth, self.current_bid_depth, self.current_ask_depth)
        self.observed_at = timestamp


class Status(StrEnum):
    PENDING = "待回报"
    WORKING = "已受理"
    PARTIAL = "部分成交"
    FILLED = "全部成交"
    CANCELED = "已撤单"
    REJECTED = "已拒绝"


ACTIVE = {Status.PENDING, Status.WORKING, Status.PARTIAL}


@dataclass
class Order:
    id: str
    key: str
    side: str
    offset: str
    ticks: int
    quantity: int
    filled: int = 0
    status: Status = Status.PENDING
    message: str = "等待本地模拟器回报"
    group: int = 1
    stop_id: str = ""
    order_type: str = "GFD"
    after_order: str = ""

    @property
    def remaining(self):
        return self.quantity - self.filled


@dataclass
class Position:
    quantity: int = 0
    cost_ticks: Decimal = Decimal("0")


@dataclass(frozen=True)
class Fill:
    event_id: str
    order_id: str
    key: str
    side: str
    offset: str
    ticks: int
    quantity: int


@dataclass
class Ledger:
    max_order: int = 20
    max_position: int = 100
    orders: dict[str, Order] = field(default_factory=dict)
    positions: dict[tuple[str, str], Position] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    seen_events: set[str] = field(default_factory=set)
    realized: Decimal = Decimal("0")
    realized_by_contract: dict[str, Decimal] = field(default_factory=dict)
    risk: RiskBook = field(default_factory=RiskBook)
    limits: RiskLimits = field(default_factory=lambda: RiskLimits(100, 100, 1000, "100000"))
    marks: dict[str, int] = field(default_factory=dict)
    stop_manager: object = field(default=None, repr=False)
    catalog: dict[str, Instrument] = field(default_factory=lambda: dict(CATALOG))
    trading_error: str = ""
    risk_day: str = ""
    session_id: str = field(default_factory=lambda: uuid4().hex)
    cancel_caps: dict[str, int] = field(default_factory=dict)
    cancel_counts: dict[str, int] = field(default_factory=dict)

    def position_side(self, side, offset):
        return side if offset == "开仓" else ("卖" if side == "买" else "买")

    def submit(self, key, side, offset, ticks, quantity, group=1, stop_id="", order_type="GFD", after_order=""):
        if self.trading_error:
            raise ValueError(self.trading_error)
        if type(group) is not int or not 1 <= group <= 20:
            raise ValueError("分组须为 1–20")
        if key not in self.catalog or side not in {"买", "卖"} or offset not in {"开仓", "平仓"}:
            raise ValueError("合约、方向或开平无效")
        if order_type not in {"GFD", "FAK", "FOK"}:
            raise ValueError("未知委托有效期")
        if after_order:
            previous = self.orders.get(after_order)
            if previous is None or (previous.key, previous.side, previous.group, previous.offset, offset) != (key, side, group, "平仓", "开仓"):
                raise ValueError("先平后开关联委托无效")
        if type(ticks) is not int or ticks <= 0 or type(quantity) is not int or not 1 <= quantity <= self.max_order:
            raise ValueError(f"单笔数量须为 1–{self.max_order} 手，价格须为正整数 tick")
        position_side = self.position_side(side, offset)
        held = self.positions.get((key, position_side), Position()).quantity
        reserved = sum(o.remaining for o in self.orders.values() if o.key == key and o.status in ACTIVE and o.offset == offset and self.position_side(o.side, o.offset) == position_side)
        if offset == "平仓" and quantity > held - reserved:
            raise ValueError("可平持仓不足，已扣除未成交平仓委托")
        if offset == "开仓" and held + reserved + quantity > self.max_position:
            raise ValueError(f"超过单合约单边 {self.max_position} 手模拟持仓限额")
        self.update_risk()
        if offset == "开仓":
            positions = [dict(Position=position.quantity) for position in self.positions.values()]
            pending = [dict(VolumeTotalOriginal=order.quantity, VolumeTraded=order.filled, Group=order.group) for order in self.orders.values() if order.status in ACTIVE and order.offset == "开仓"]
            self.risk.check_open(self.limits, positions, pending, quantity, group)
        else:
            grouped = self.risk.held(group, key, "2" if position_side == "买" else "3")
            pending = sum(order.remaining for order in self.orders.values() if order.status in ACTIVE and order.key == key and order.offset == "平仓" and order.side == side and order.group == group)
            stop_reserved = sum(stop.quantity for stop in self.stop_manager.stops.values() if stop.key == key and stop.group == group and stop.position_side == position_side and stop.state not in {"filled", "canceled"} and stop.id != stop_id) if self.stop_manager else 0
            if quantity + pending + stop_reserved > grouped:
                raise ValueError("分组可平持仓不足，含未成交委托与止损占用")
        order = Order(f"SIM{self.session_id}:{len(self.orders) + 1:06}", key, side, offset, ticks, quantity, group=group, stop_id=stop_id, order_type=order_type, after_order=after_order)
        if after_order:
            order.message = "等待关联平仓单全部成交后再受理开仓"
        self.orders[order.id] = order
        return order

    def acknowledge(self, order_id, accepted=True, message=""):
        order = self.orders[order_id]
        if order.status == Status.PENDING:
            order.status = Status.WORKING if accepted else Status.REJECTED
            order.message = message or ("本地模拟器已受理" if accepted else "模拟拒单")

    def cancel_report(self, order_id, accepted=True):
        order = self.orders[order_id]
        if order.status not in ACTIVE:
            order.message = "撤单失败：委托已终结"
            return False
        self.update_risk()
        cap = self.cancel_caps.get(order.key)
        if cap is not None and self.cancel_counts.get(order.key, 0) >= cap:
            raise ValueError("本合约本地模拟撤单限制已达到；可在 DOM 调整限制")
        self.risk.reserve_cancel(self.limits, str(self.risk.cancel_count + 1))
        if not accepted:
            order.message = "模拟撤单失败，委托仍有效"
            return False
        order.status = Status.CANCELED
        self.cancel_counts[order.key] = self.cancel_counts.get(order.key, 0) + 1
        order.message = "本地模拟器撤单回报"
        return True

    def apply_fill(self, fill):
        if fill.event_id in self.seen_events:
            return False
        order = self.orders[fill.order_id]
        if order.status not in {Status.WORKING, Status.PARTIAL}:
            raise ValueError("当前委托状态不接受成交回报")
        if (fill.key, fill.side, fill.offset) != (order.key, order.side, order.offset):
            raise ValueError("成交回报与委托不匹配")
        if type(fill.quantity) is not int or not 0 < fill.quantity <= order.remaining:
            raise ValueError("成交数量无效")
        if type(fill.ticks) is not int or fill.ticks <= 0 or (order.side == "买" and fill.ticks > order.ticks) or (order.side == "卖" and fill.ticks < order.ticks):
            raise ValueError("成交价格超出限价")
        position_key = (order.key, self.position_side(order.side, order.offset))
        position = self.positions.get(position_key, Position())
        if order.offset == "平仓" and fill.quantity > position.quantity:
            raise ValueError("平仓成交超过持仓")
        if order.offset == "开仓":
            position.quantity += fill.quantity
            position.cost_ticks += fill.ticks * fill.quantity
        else:
            closed_cost = position.cost_ticks if fill.quantity == position.quantity else Decimal(position.cost_ticks) * fill.quantity / position.quantity
            direction = 1 if position_key[1] == "买" else -1
            instrument = self.catalog[order.key]
            profit = (Decimal(fill.ticks) * fill.quantity - closed_cost) * direction * instrument.tick * instrument.multiplier
            self.realized += profit
            self.realized_by_contract[order.key] = self.realized_by_contract.get(order.key, Decimal("0")) + profit
            position.cost_ticks = Decimal(position.cost_ticks) - closed_cost
            position.quantity -= fill.quantity
        self.positions[position_key] = position
        identifier = "|".join((*order.key.split(".", 1), "2" if position_key[1] == "买" else "3", "", "1"))
        allocation = self.risk.allocations.setdefault(identifier, {})
        allocation[str(order.group)] = max(0, allocation.get(str(order.group), 0) + (fill.quantity if order.offset == "开仓" else -fill.quantity))
        order.filled += fill.quantity
        order.status = Status.FILLED if order.remaining == 0 else Status.PARTIAL
        order.message = "本地模拟成交回报"
        self.fills.append(fill)
        self.seen_events.add(fill.event_id)
        return True

    def contract_pnl(self, key, last=None):
        instrument = self.catalog[key]
        floating = Decimal("0")
        for side, direction in (("买", 1), ("卖", -1)):
            position = self.positions.get((key, side))
            if position and position.quantity:
                if last is None:
                    return None, None
                floating += (Decimal(last) * position.quantity - position.cost_ticks) * direction * instrument.tick * instrument.multiplier
        return self.realized_by_contract.get(key, Decimal("0")) + floating, floating


    def update_risk(self):
        day = self.risk_day or date.today().strftime("%Y%m%d")
        new_day = self.risk.day != day
        pnl = self.realized
        for (key, side), position in self.positions.items():
            instrument = self.catalog[key]
            pnl += (self.marks.get(key, instrument.initial_ticks) * position.quantity - position.cost_ticks) * instrument.tick * instrument.multiplier * (1 if side == "买" else -1)
        if new_day:
            if self.risk.day:
                self.risk.baseline = str(pnl)
            self.risk.day = day
            self.risk.cancel_count = 0
            self.cancel_counts.clear()
            self.risk.cancel_tokens = {}
            self.risk.loss_locked = False
        self.risk.pnl = pnl - Decimal(self.risk.baseline or "0")
        if self.risk.pnl <= -Decimal(self.limits.loss):
            self.risk.loss_locked = True
