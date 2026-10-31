import random
from datetime import datetime

from .domain import CATALOG, Fill, Snapshot, Status


class Simulator:
    def __init__(self, ledger, seed=20261029):
        self.ledger = ledger
        self.catalog = ledger.catalog
        self.random = random.Random(seed)
        self.prices = {key: item.initial_ticks for key, item in self.catalog.items()}
        self.volumes = dict.fromkeys(self.catalog, 0)
        self.snapshots = {}
        self.sequence = 0

    def advance(self):
        self.sequence += 1
        for key, instrument in self.catalog.items():
            last = max(10, self.prices[key] + self.random.choice((-1, 0, 0, 0, 1)))
            self.prices[key] = last
            self.volumes[key] += self.random.randint(1, 12)
            bids = tuple((last - 1 - i, self.random.randint(1, 80)) for i in range(instrument.depth))
            asks = tuple((last + 1 + i, self.random.randint(1, 80)) for i in range(instrument.depth))
            self.snapshots[key] = Snapshot(key, last, bids, asks, self.volumes[key], datetime.now().strftime("%H:%M:%S"))
        self.ledger.marks = dict(self.prices)
        self.ledger.update_risk()
        liquidity = {(key, side, price): quantity for key, snapshot in self.snapshots.items() for side, levels in (("买", snapshot.asks), ("卖", snapshot.bids)) for price, quantity in levels}
        for order in self.ledger.orders.values():
            if order.status == Status.PENDING:
                self.ledger.acknowledge(order.id)
                continue
            if order.status not in {Status.WORKING, Status.PARTIAL}:
                continue
            snapshot = self.snapshots[order.key]
            levels = snapshot.asks if order.side == "买" else snapshot.bids
            for price, available in levels:
                crosses = order.ticks >= price if order.side == "买" else order.ticks <= price
                if not crosses:
                    break
                level_key = (order.key, order.side, price)
                quantity = min(order.remaining, liquidity[level_key], 2)
                if quantity:
                    event = Fill(f"{self.sequence}:{order.id}:{price}", order.id, order.key, order.side, order.offset, price, quantity)
                    self.ledger.apply_fill(event)
                    liquidity[level_key] -= quantity
                break
        return self.snapshots
