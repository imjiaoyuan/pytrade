from dataclasses import dataclass, field
from decimal import Decimal

from .domain import ACTIVE, Status


@dataclass(frozen=True)
class SpreadQuote:
    bid: Decimal
    ask: Decimal
    bid_volume: int
    ask_volume: int


def calculate_spread(first, second, first_tick, second_tick, factor=Decimal(1), ratio_a=1, ratio_b=1):
    factor = Decimal(str(factor))
    if not factor.is_finite() or factor <= 0 or any(type(ratio) is not int or ratio <= 0 for ratio in (ratio_a, ratio_b)):
        raise ValueError("价差系数与腿比例必须为正数")
    if first.key == second.key or not all((first.bids, first.asks, second.bids, second.asks)):
        raise ValueError("价差需要两个不同合约的完整一档盘口")
    if first.trading_day != second.trading_day:
        raise ValueError("价差腿的交易日不一致")
    return SpreadQuote(
        first_tick * first.bids[0][0] - factor * second_tick * second.asks[0][0],
        first_tick * first.asks[0][0] - factor * second_tick * second.bids[0][0],
        min(first.bids[0][1] // ratio_a, second.asks[0][1] // ratio_b),
        min(first.asks[0][1] // ratio_a, second.bids[0][1] // ratio_b),
    )


@dataclass
class AlgoTask:
    id: str
    kind: str
    key: str
    side: str
    offset: str
    quantity: int
    limit: Decimal
    group: int
    second_key: str = ""
    factor: Decimal = Decimal(1)
    ratio_a: int = 1
    ratio_b: int = 1
    status: str = "运行"
    message: str = "等待下一次有效报价"
    orders: list[str] = field(default_factory=list)
    hedge_orders: list[str] = field(default_factory=list)
    day: str = ""
    last_quotes: dict = field(default_factory=dict, repr=False)
    session: object = field(default=None, repr=False)
    ledger: object = field(default=None, repr=False)
    retained: set = field(default_factory=set, repr=False)
    working_seen: set = field(default_factory=set, repr=False)
    guard: object = field(default=None, repr=False)

    @property
    def keys(self):
        return (self.key, self.second_key) if self.second_key else (self.key,)


class LocalAlgoEngine:
    def __init__(self, controller):
        self.controller = controller
        self.tasks = {}

    def quote(self, key):
        controller = self.controller
        if controller.paper:
            return controller.paper.check_ready(key)
        if controller.external or not controller.offline_mode or controller.paused:
            raise ValueError("策略仅用于运行中的本地模拟")
        snapshot = controller.simulator.snapshots.get(key)
        instrument = controller.ledger.catalog.get(key)
        if snapshot is None or instrument is None or instrument.product_class != "1":
            raise ValueError("策略仅支持有行情的期货合约")
        return snapshot

    def start(self, kind, key, side, offset, quantity, limit, group, second_key="", factor=Decimal(1), ratio_a=1, ratio_b=1, guard=None):
        limit = Decimal(str(limit))
        if kind not in {"best", "sniper", "stop", "spread"} or side not in {"买", "卖"} or offset not in {"开仓", "平仓"} or not limit.is_finite():
            raise ValueError("策略类型、方向、开平或价格无效")
        ledger = self.controller.ledger
        if type(quantity) is not int or not 1 <= quantity <= ledger.max_order or type(group) is not int or not 1 <= group <= 20:
            raise ValueError("策略手数或组别无效")
        first = self.quote(key)
        if kind == "spread":
            second = self.quote(second_key)
            calculate_spread(first, second, ledger.catalog[key].tick, ledger.catalog[second_key].tick, factor, ratio_a, ratio_b)
            if max(quantity * ratio_a, quantity * ratio_b) > ledger.max_order:
                raise ValueError("单腿数量超过单笔模拟上限")
        else:
            ledger.catalog[key].ticks(str(limit))
            second_key = ""
        if offset == "平仓":
            self.check_close(key, side, quantity * (ratio_a if second_key else 1), group)
            if second_key:
                self.check_close(second_key, "卖" if side == "买" else "买", quantity * ratio_b, group)
        task = AlgoTask(f"ALGO{len(self.tasks) + 1}", kind, key, side, offset, quantity, limit, group, second_key, Decimal(str(factor)), ratio_a, ratio_b, day=first.trading_day, session=self.controller.external, ledger=ledger, guard=guard)
        if self.controller.paper:
            try:
                for symbol in task.keys:
                    task.session.retain(symbol)
                    task.retained.add(symbol)
            except ValueError:
                self.release(task)
                raise
        self.tasks[task.id] = task
        return task

    def check_close(self, key, side, quantity, group):
        ledger = self.controller.ledger
        position_side = ledger.position_side(side, "平仓")
        held = ledger.positions.get((key, position_side))
        orders = [order for order in ledger.orders.values() if order.key == key and order.side == side and order.offset == "平仓" and order.status in ACTIVE]
        available = (held.quantity if held else 0) - sum(order.remaining for order in orders)
        grouped = ledger.risk.held(group, key, "2" if position_side == "买" else "3") - sum(order.remaining for order in orders if order.group == group)
        if ledger.stop_manager:
            grouped -= sum(stop.quantity for stop in ledger.stop_manager.stops.values() if stop.key == key and stop.group == group and stop.position_side == position_side and stop.state not in {"filled", "canceled"})
        if quantity > min(available, grouped):
            raise ValueError(f"{key} 策略可平持仓不足，已扣除挂单、分组及止损占用")

    def release(self, task):
        for key in list(task.retained):
            try:
                task.session.release(key)
            except ValueError:
                pass
            task.retained.discard(key)

    def stop(self, task_id):
        task = self.tasks[task_id]
        errors = []
        for identifier in task.orders + task.hedge_orders:
            order = task.ledger.orders[identifier]
            if order.status in ACTIVE:
                try:
                    task.ledger.cancel_report(identifier)
                except ValueError as error:
                    errors.append(str(error))
        task.status = "停止失败" if errors else "已停止"
        task.message = "；".join(errors) if errors else "不再自动报单；已有成交及残腿请在持仓中核对"
        if self.controller.paper and task.ledger is self.controller.ledger:
            self.controller.paper.sync_references()
        self.release(task)

    def suspend(self, message):
        for task in self.tasks.values():
            if task.status == "运行":
                task.status, task.message = "已暂停", message + "；挂单和已成交持仓须人工核对，策略不自动恢复"
                self.release(task)

    def send(self, task, key, side, quantity, ticks):
        snapshot = self.quote(key)
        if task.guard:
            task.guard(snapshot, side, task.offset, ticks)
        sender = self.controller.paper or self.controller.ledger
        return sender.submit(key, side, task.offset, ticks, quantity, task.group)

    def update(self, snapshot):
        for task in self.tasks.values():
            if task.status != "运行" or snapshot.key not in task.keys or task.last_quotes.get(snapshot.key) is snapshot:
                continue
            task.last_quotes[snapshot.key] = snapshot
            if task.ledger is not self.controller.ledger or task.session is not self.controller.external or snapshot.trading_day != task.day:
                task.status, task.message = "已暂停", "模式、账户会话或交易日已改变，不自动重放策略"
                self.release(task)
                continue
            try:
                for key in task.keys:
                    self.quote(key)
                self.update_spread(task) if task.kind == "spread" else self.update_single(task)
            except (ValueError, ArithmeticError) as error:
                task.status, task.message = "已暂停", f"{error}；已有委托及残腿请人工核对"
                self.release(task)
            if task.status != "运行":
                self.release(task)

    def update_single(self, task):
        ledger = task.ledger
        filled = sum(ledger.orders[identifier].filled for identifier in task.orders)
        if filled >= task.quantity:
            task.status, task.message = "已完成", f"本地成交 {filled} 手"
            return
        if any(ledger.orders[identifier].status == Status.REJECTED for identifier in task.orders):
            raise ValueError("策略委托已拒绝，不自动重发")
        snapshot = self.quote(task.key)
        instrument = ledger.catalog[task.key]
        limit_ticks = instrument.ticks(str(task.limit))
        active = [ledger.orders[identifier] for identifier in task.orders if ledger.orders[identifier].status in ACTIVE]
        levels = snapshot.bids if task.side == "买" else snapshot.asks
        if task.kind == "best":
            if not levels:
                raise ValueError("同侧盘口缺失")
            ticks = levels[0][0]
            allowed = ticks <= limit_ticks if task.side == "买" else ticks >= limit_ticks
            if active:
                if not allowed or active[0].ticks != ticks:
                    ledger.cancel_report(active[0].id)
                    task.message = "已收到本地撤单回报，等待下一次报价再挂"
                return
            if not allowed:
                task.message = "最优价超出策略限价，等待"
                return
        else:
            levels = snapshot.asks if task.side == "买" else snapshot.bids
            if active:
                if task.kind == "sniper":
                    if active[0].id in task.working_seen:
                        ledger.cancel_report(active[0].id)
                    elif active[0].status != Status.PENDING:
                        task.working_seen.add(active[0].id)
                return
            if not levels:
                raise ValueError("对手盘口缺失")
            ticks = levels[0][0]
            if task.kind == "stop":
                if task.orders:
                    task.status, task.message = "已终结", f"条件单已结束，成交 {filled}/{task.quantity} 手；不自动重复触发"
                    return
                triggered = snapshot.last >= limit_ticks if task.side == "买" else snapshot.last <= limit_ticks
                if not triggered:
                    task.message = "条件未触发"
                    return
            elif not (ticks <= limit_ticks if task.side == "买" else ticks >= limit_ticks):
                task.message = "对手价未到策略限价，等待"
                return
        quantity = task.quantity - filled
        if task.kind == "sniper":
            quantity = min(quantity, levels[0][1])
        order = self.send(task, task.key, task.side, quantity, ticks)
        task.orders.append(order.id)
        task.message = f"{order.id} 等待本地回报；成交 {filled}/{task.quantity}"

    def update_spread(self, task):
        ledger = task.ledger
        first, second = (self.quote(key) for key in task.keys)
        quote = calculate_spread(first, second, ledger.catalog[task.key].tick, ledger.catalog[task.second_key].tick, task.factor, task.ratio_a, task.ratio_b)
        filled_a = sum(ledger.orders[identifier].filled for identifier in task.orders)
        filled_b = sum(ledger.orders[identifier].filled for identifier in task.hedge_orders)
        target_b = filled_a * task.ratio_b // task.ratio_a
        task.message = f"价差 {quote.bid}/{quote.ask}；A成交 {filled_a}，B成交 {filled_b}，待补B {max(0, target_b - filled_b)}；分别成交有残腿风险"
        if filled_a == task.quantity * task.ratio_a and filled_b == task.quantity * task.ratio_b:
            task.status = "已完成"
            return
        if filled_b < target_b and any(ledger.orders[identifier].status in {Status.CANCELED, Status.REJECTED} for identifier in task.hedge_orders):
            raise ValueError("补腿委托已撤销或拒绝，不自动追价；须处理残腿")
        if filled_b < target_b and not any(ledger.orders[identifier].status in ACTIVE for identifier in task.hedge_orders):
            side = "卖" if task.side == "买" else "买"
            levels = second.bids if side == "卖" else second.asks
            task.hedge_orders.append(self.send(task, task.second_key, side, target_b - filled_b, levels[0][0]).id)
            return
        if task.orders:
            if not any(ledger.orders[identifier].status in ACTIVE for identifier in task.orders + task.hedge_orders) and filled_b == target_b:
                task.status = "已终结"
                task.message += "；主动腿已终结，未完成份数不自动重发；请核对残腿"
            return
        price, volume = (quote.ask, quote.ask_volume) if task.side == "买" else (quote.bid, quote.bid_volume)
        if volume < task.quantity or not (price <= task.limit if task.side == "买" else price >= task.limit):
            return
        levels = first.asks if task.side == "买" else first.bids
        task.orders.append(self.send(task, task.key, task.side, task.quantity * task.ratio_a, levels[0][0]).id)
