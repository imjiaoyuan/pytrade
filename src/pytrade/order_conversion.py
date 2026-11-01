from dataclasses import dataclass


@dataclass(frozen=True)
class OffsetLeg:
    offset: str
    quantity: int


def convert_offsets(quantity, today, yesterday, exchange, mode="net"):
    if any(type(value) is not int or value < 0 for value in (quantity, today, yesterday)) or not quantity or mode not in {"net", "close", "lock"}:
        raise ValueError("开平转换参数无效")
    split = exchange in {"SHFE", "INE"}
    if mode == "lock":
        if today and not split:
            return (OffsetLeg("开仓", quantity),)
        close = min(quantity, yesterday)
        result = [OffsetLeg("平昨" if split else "平仓", close)] if close else []
    else:
        close = min(quantity, today + yesterday)
        if mode == "close" and close < quantity:
            raise ValueError("可平持仓不足")
        if split:
            td = min(today, close)
            result = ([OffsetLeg("平今", td)] if td else []) + ([OffsetLeg("平昨", close - td)] if close > td else [])
        else:
            result = [OffsetLeg("平仓", close)] if close else []
    if quantity > close:
        result.append(OffsetLeg("开仓", quantity - close))
    return tuple(result)


def local_net_plan(ledger, key, side, quantity, group):
    from .domain import ACTIVE
    position_side = "卖" if side == "买" else "买"
    held = ledger.risk.held(group, key, "3" if side == "买" else "2")
    pending = sum(order.remaining for order in ledger.orders.values() if order.key == key and order.side == side and order.offset == "平仓" and order.group == group and order.status in ACTIVE)
    stops = 0
    if ledger.stop_manager:
        stops = sum(stop.quantity for stop in ledger.stop_manager.stops.values() if stop.key == key and stop.group == group and stop.position_side == position_side and stop.state not in {"filled", "canceled"})
    return convert_offsets(quantity, max(0, held - pending - stops), 0, "LOCAL", "net")
