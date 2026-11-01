from .domain import ACTIVE, Fill, Status


def dependency_ready(ledger, order):
    if not order.after_order:
        return True
    previous = ledger.orders.get(order.after_order)
    if previous is None or (previous.status not in ACTIVE and previous.status != Status.FILLED):
        order.status, order.message = Status.CANCELED, "关联平仓单未全部成交，后续开仓已自动取消"
        return False
    return previous.status == Status.FILLED


def match_order(ledger, order, levels, liquidity, event_prefix, ordinary_cap=None):
    eligible = [(price, liquidity.get(price, 0)) for price, _ in levels if (price <= order.ticks if order.side == "买" else price >= order.ticks)]
    if order.order_type == "FOK" and sum(quantity for _, quantity in eligible) < order.remaining:
        order.status, order.message = Status.CANCELED, "本地 FOK 可见剩余数量不足，整单未成交并自动撤销"
        return
    for price, available in eligible:
        quantity = min(order.remaining, available)
        if order.order_type == "GFD" and ordinary_cap:
            quantity = min(quantity, ordinary_cap)
        if quantity:
            ledger.apply_fill(Fill(f"{event_prefix}:{order.id}:{price}", order.id, order.key, order.side, order.offset, price, quantity))
            liquidity[price] -= quantity
        if not order.remaining or (ordinary_cap and order.order_type == "GFD"):
            break
    if order.order_type in {"FAK", "FOK"} and order.remaining:
        order.status, order.message = Status.CANCELED, f"本地 {order.order_type} 首次撮合剩余自动撤销"
