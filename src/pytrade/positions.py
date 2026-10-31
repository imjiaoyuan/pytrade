from collections import defaultdict


def position_id(row):
    return "|".join(str(row.get(name, "")) for name in ("ExchangeID", "InstrumentID", "PosiDirection", "PositionDate", "HedgeFlag"))


class PositionIndex:
    def __init__(self, positions, orders):
        self.positions = defaultdict(list)
        self.system_orders = defaultdict(list)
        self.reference_orders = defaultdict(list)
        self.quantities = {}
        for row in positions:
            key = tuple(row.get(name) for name in ("ExchangeID", "InstrumentID", "PosiDirection", "HedgeFlag"))
            self.positions[key].append(row)
            self.quantities[position_id(row)] = row.get("Position", 0)
        for row in orders.values():
            key = tuple(row.get(name) for name in ("TradingDay", "ExchangeID", "InstrumentID"))
            system_id = str(row.get("OrderSysID", "")).strip()
            reference = str(row.get("OrderRef", "")).strip()
            if system_id:
                self.system_orders[(*key, system_id)].append(row)
            if reference:
                self.reference_orders[(*key, reference)].append(row)

    def match_order(self, trade):
        key = tuple(trade.get(name) for name in ("TradingDay", "ExchangeID", "InstrumentID"))
        system_id = str(trade.get("OrderSysID", "")).strip()
        reference = str(trade.get("OrderRef", "")).strip()
        matches = self.system_orders.get((*key, system_id), []) if system_id else self.reference_orders.get((*key, reference), [])
        return matches[0] if len(matches) == 1 else None

    def match_position(self, trade):
        direction, offset = trade.get("Direction"), trade.get("OffsetFlag")
        if trade.get("HedgeFlag", "1") != "1" or direction not in {"0", "1"} or offset not in {"0", "1", "3", "4"}:
            return None
        position_direction = ("2" if direction == "0" else "3") if offset == "0" else ("3" if direction == "0" else "2")
        key = (trade.get("ExchangeID"), trade.get("InstrumentID"), position_direction, "1")
        candidates = self.positions.get(key, [])
        if key[0] in {"SHFE", "INE"}:
            date = "1" if offset in {"0", "3"} else "2"
            candidates = [row for row in candidates if row.get("PositionDate") == date]
        return candidates[0] if len(candidates) == 1 else None


def allocation_changes(positions, orders, trades, processed, day):
    index = PositionIndex(positions, orders)
    deltas = defaultdict(int)
    for identifier, trade in trades.items():
        if identifier in processed:
            continue
        processed.add(identifier)
        if trade.get("TradingDay") != day:
            continue
        order = index.match_order(trade)
        position = index.match_position(trade)
        quantity = trade.get("Volume", 0)
        if not order or not order.get("Group") or order.get("CombHedgeFlag", "1") != "1" or position is None or type(quantity) is not int or quantity <= 0:
            continue
        deltas[(position_id(position), str(order["Group"]))] += quantity if trade["OffsetFlag"] == "0" else -quantity
    return index.quantities, deltas
