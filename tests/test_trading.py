import unittest
from decimal import Decimal

from pytrade.domain import CATALOG, Fill, Ledger, Status
from pytrade.simulation import Simulator


class TradingTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(max_order=20, max_position=5)
        self.key = "SHFE.au2612"

    def order(self, side="买", offset="开仓", quantity=3, price=46000):
        order = self.ledger.submit(self.key, side, offset, price, quantity)
        self.ledger.acknowledge(order.id)
        return order

    def fill(self, order, event="fill1", quantity=1, price=45825):
        return Fill(event, order.id, order.key, order.side, order.offset, price, quantity)

    def test_contract_pnl_tracks_partial_closes_duplicates_short_and_flat(self):
        opened = self.order(quantity=3)
        self.ledger.apply_fill(self.fill(opened, quantity=3))
        close = self.order("卖", "平仓", 2, 45830)
        fill = self.fill(close, "partial-close", 2, 45835)
        self.ledger.apply_fill(fill)
        self.assertFalse(self.ledger.apply_fill(fill))
        self.assertEqual(self.ledger.contract_pnl(self.key, 45820), (Decimal("300"), Decimal("-100")))
        self.assertEqual(self.ledger.contract_pnl(self.key), (None, None))
        last_close = self.order("卖", "平仓", 1, 45820)
        self.ledger.apply_fill(self.fill(last_close, "last-close", 1, 45820))
        self.assertEqual(self.ledger.contract_pnl(self.key), (Decimal("300"), Decimal("0")))
        short = self.order("卖", "开仓", 2, 45820)
        self.ledger.apply_fill(self.fill(short, "short-open", 2, 45820))
        self.assertEqual(self.ledger.contract_pnl(self.key, 45810), (Decimal("700"), Decimal("400")))
        self.assertEqual(self.ledger.contract_pnl("SHFE.ag2612", 100), (Decimal("0"), Decimal("0")))
        self.assertEqual(self.ledger.realized, Decimal("300"))

    def test_price_precision(self):
        item = CATALOG[self.key]
        self.assertEqual(item.ticks("916.66"), 45833)
        self.assertEqual(item.price(45833), "916.66")
        for value in ("916.661", "NaN", "Infinity", "0", "-1"):
            with self.assertRaises((ValueError, ArithmeticError)):
                item.ticks(value)

    def test_partial_duplicate_and_complete(self):
        order = self.order()
        fill = self.fill(order)
        self.assertTrue(self.ledger.apply_fill(fill))
        self.assertEqual(order.status, Status.PARTIAL)
        self.assertFalse(self.ledger.apply_fill(fill))
        self.assertEqual(order.filled, 1)
        self.ledger.apply_fill(self.fill(order, "fill2", 2))
        self.assertEqual(order.status, Status.FILLED)
        self.assertEqual(self.ledger.positions[(self.key, "买")].quantity, 3)

    def test_risk_includes_pending_orders(self):
        self.ledger.submit(self.key, "买", "开仓", 46000, 4)
        with self.assertRaises(ValueError):
            self.ledger.submit(self.key, "买", "开仓", 46000, 2)

    def test_close_reservation_and_profit(self):
        order = self.order()
        self.ledger.apply_fill(self.fill(order, quantity=3))
        close = self.order("卖", "平仓", 2, 45830)
        with self.assertRaises(ValueError):
            self.order("卖", "平仓", 2, 45830)
        self.ledger.apply_fill(self.fill(close, "close", 2, 45835))
        self.assertEqual(self.ledger.realized, Decimal("400.00"))
        self.assertEqual(self.ledger.positions[(self.key, "买")].quantity, 1)

    def test_failed_cancel_keeps_reservation(self):
        order = self.order(quantity=5)
        self.assertFalse(self.ledger.cancel_report(order.id, False))
        with self.assertRaises(ValueError):
            self.order(quantity=1)
        self.assertTrue(self.ledger.cancel_report(order.id))
        self.order(quantity=5)

    def test_rejected_order_releases_risk(self):
        order = self.ledger.submit(self.key, "买", "开仓", 46000, 5)
        self.ledger.acknowledge(order.id, False)
        self.assertEqual(order.status, Status.REJECTED)
        self.order(quantity=5)

    def test_bad_fill_does_not_mutate(self):
        order = self.order()
        for fill in (self.fill(order, price=47000), self.fill(order, quantity=4), Fill("bad", order.id, "DCE.i2701", "买", "开仓", 1, 1)):
            with self.assertRaises(ValueError):
                self.ledger.apply_fill(fill)
        self.assertEqual(order.filled, 0)
        self.assertEqual(self.ledger.positions, {})

    def test_request_is_not_fill_and_terminal_is_final(self):
        order = self.ledger.submit(self.key, "买", "开仓", 46000, 1)
        self.assertEqual(order.status, Status.PENDING)
        with self.assertRaises(ValueError):
            self.ledger.apply_fill(self.fill(order))
        self.ledger.cancel_report(order.id)
        self.ledger.acknowledge(order.id)
        self.assertEqual(order.status, Status.CANCELED)
        with self.assertRaises(ValueError):
            self.ledger.apply_fill(self.fill(order))

    def test_sell_close_short_profit(self):
        order = self.order("卖", quantity=2, price=45800)
        self.ledger.apply_fill(self.fill(order, quantity=2))
        close = self.order("买", "平仓", 2, 45830)
        self.ledger.apply_fill(self.fill(close, "close", 2, 45815))
        self.assertEqual(self.ledger.realized, Decimal("400.00"))

    def test_simulator_depth_and_delayed_fill(self):
        simulator = Simulator(self.ledger)
        snapshot = simulator.advance()[self.key]
        self.assertEqual(len(snapshot.bids), 5)
        self.assertEqual(len(simulator.snapshots["DCE.i2701"].bids), 1)
        order = self.ledger.submit(self.key, "买", "开仓", snapshot.last + 100, 3)
        simulator.advance()
        self.assertEqual(order.status, Status.WORKING)
        self.assertEqual(order.filled, 0)
        simulator.advance()
        self.assertEqual(order.status, Status.PARTIAL)
        simulator.advance()
        self.assertEqual(order.status, Status.FILLED)


if __name__ == "__main__":
    unittest.main()
