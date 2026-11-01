import unittest
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

from pytrade.domain import CATALOG, Fill, Ledger, Snapshot, Status
from pytrade.local_algos import LocalAlgoEngine, calculate_spread
from pytrade.matching import match_order
from pytrade.order_conversion import convert_offsets, local_net_plan


class ConversionMatchingTests(unittest.TestCase):
    def test_net_and_close_split_today_yesterday_and_open(self):
        legs = convert_offsets(8, 2, 3, "SHFE")
        self.assertEqual([(leg.offset, leg.quantity) for leg in legs], [("平今", 2), ("平昨", 3), ("开仓", 3)])
        self.assertEqual([(leg.offset, leg.quantity) for leg in convert_offsets(4, 2, 3, "DCE")], [("平仓", 4)])
        with self.assertRaisesRegex(ValueError, "可平"):
            convert_offsets(8, 2, 3, "SHFE", "close")

    def test_lock_uses_exchange_specific_today_handling(self):
        self.assertEqual([(leg.offset, leg.quantity) for leg in convert_offsets(4, 2, 3, "DCE", "lock")], [("开仓", 4)])
        self.assertEqual([(leg.offset, leg.quantity) for leg in convert_offsets(4, 2, 3, "INE", "lock")], [("平昨", 3), ("开仓", 1)])

    def ledger_order(self, kind, quantity):
        ledger = Ledger()
        order = ledger.submit("SHFE.ag2612", "买", "开仓", 102, quantity, order_type=kind)
        ledger.acknowledge(order.id)
        return ledger, order

    def test_fok_does_not_partially_fill_or_consume_insufficient_book(self):
        ledger, order = self.ledger_order("FOK", 5)
        book = {101: 2, 102: 2}
        match_order(ledger, order, ((101, 2), (102, 2)), book, "q1")
        self.assertEqual((order.status, order.filled), (Status.CANCELED, 0))
        self.assertFalse(ledger.fills)
        self.assertEqual(book, {101: 2, 102: 2})

    def test_fok_fills_all_levels_and_ignores_ordinary_simulation_cap(self):
        ledger, order = self.ledger_order("FOK", 5)
        book = {101: 3, 102: 2}
        match_order(ledger, order, ((101, 3), (102, 2)), book, "q1", ordinary_cap=2)
        self.assertEqual((order.status, order.filled), (Status.FILLED, 5))
        self.assertEqual(book, {101: 0, 102: 0})

    def test_fak_fills_available_then_cancels_remainder_despite_manual_cancel_lock(self):
        ledger, order = self.ledger_order("FAK", 5)
        ledger.cancel_caps[order.key] = 0
        book = {101: 2, 102: 1}
        match_order(ledger, order, ((101, 2), (102, 1)), book, "q1")
        self.assertEqual((order.status, order.filled), (Status.CANCELED, 3))
        self.assertEqual(ledger.risk.cancel_count, 0)

    def test_shared_liquidity_and_limit_price_are_respected(self):
        ledger, first = self.ledger_order("GFD", 3)
        second = ledger.submit(first.key, "买", "开仓", 102, 3, order_type="FOK")
        ledger.acknowledge(second.id)
        book = {101: 3, 103: 20}
        levels = ((101, 3), (103, 20))
        match_order(ledger, first, levels, book, "q1")
        match_order(ledger, second, levels, book, "q1")
        self.assertEqual(first.filled, 3)
        self.assertEqual(second.filled, 0)
        self.assertEqual(book[103], 20)

    def test_net_plan_respects_group_and_pending_close_reservations(self):
        ledger = Ledger()
        order = ledger.submit("SHFE.ag2612", "卖", "开仓", 100, 4, group=2)
        ledger.acknowledge(order.id)
        ledger.apply_fill(Fill("f1", order.id, order.key, "卖", "开仓", 100, 4))
        ledger.submit(order.key, "买", "平仓", 100, 2, group=2)
        plan = local_net_plan(ledger, order.key, "买", 5, 2)
        self.assertEqual([(leg.offset, leg.quantity) for leg in plan], [("平仓", 2), ("开仓", 3)])
        self.assertEqual(local_net_plan(ledger, order.key, "买", 5, 1)[0].offset, "开仓")


class LocalAlgoTests(unittest.TestCase):
    def setUp(self):
        self.key, self.second = "SHFE.ag2612", "SHFE.au2612"
        self.first_quote = Snapshot(self.key, 100, ((99, 10),), ((101, 10),), 10, "10:00:00")
        self.second_quote = Snapshot(self.second, 100, ((99, 10),), ((101, 10),), 10, "10:00:00")
        self.controller = SimpleNamespace(ledger=Ledger(), paper=None, external=None, offline_mode=True, paused=False, simulator=SimpleNamespace(snapshots={self.key: self.first_quote, self.second: self.second_quote}))
        self.controller.ledger.marks = {self.key: 100, self.second: 100}
        self.engine = LocalAlgoEngine(self.controller)

    def update(self, key=None, **changes):
        key = key or self.key
        old = self.controller.simulator.snapshots[key]
        snapshot = replace(old, volume=old.volume + 1, **changes)
        self.controller.simulator.snapshots[key] = snapshot
        self.engine.update(snapshot)
        return snapshot

    def fill(self, identifier, quantity):
        ledger = self.controller.ledger
        order = ledger.orders[identifier]
        ledger.acknowledge(identifier)
        ledger.apply_fill(Fill(f"f{len(ledger.fills)}", identifier, order.key, order.side, order.offset, order.ticks, quantity))

    def test_spread_uses_opposite_books_exact_decimals_and_leg_ratios(self):
        quote = calculate_spread(self.first_quote, self.second_quote, Decimal("0.02"), Decimal("0.1"), Decimal("1.5"), 2, 3)
        self.assertEqual(quote.bid, Decimal("-13.17"))
        self.assertEqual(quote.ask, Decimal("-12.83"))
        self.assertEqual((quote.bid_volume, quote.ask_volume), (3, 3))
        with self.assertRaisesRegex(ValueError, "交易日"):
            calculate_spread(self.first_quote, replace(self.second_quote, trading_day="20261102"), Decimal(1), Decimal(1))

    def test_best_limit_waits_for_cancel_and_next_quote_before_replacement(self):
        task = self.engine.start("best", self.key, "买", "开仓", 3, 105, 1)
        snapshot = self.update()
        self.assertEqual(len(task.orders), 1)
        self.engine.update(snapshot)
        self.assertEqual(len(task.orders), 1)
        first = self.controller.ledger.orders[task.orders[0]]
        self.fill(first.id, 1)
        self.update(bids=((100, 10),))
        self.assertEqual(first.status, Status.CANCELED)
        self.assertEqual(len(task.orders), 1)
        self.update(bids=((100, 10),))
        replacement = self.controller.ledger.orders[task.orders[-1]]
        self.assertEqual((replacement.ticks, replacement.quantity), (100, 2))
        self.fill(replacement.id, 2)
        self.update()
        self.assertEqual(task.status, "已完成")

    def test_best_limit_cancel_failure_suspends_without_second_order(self):
        task = self.engine.start("best", self.key, "买", "开仓", 2, 105, 1)
        self.update()
        self.controller.ledger.cancel_caps[self.key] = 0
        self.update(bids=((100, 10),))
        self.assertEqual(task.status, "已暂停")
        self.assertEqual(len(task.orders), 1)
        self.assertEqual(self.controller.ledger.orders[task.orders[0]].status, Status.PENDING)

    def test_stop_triggers_once_and_does_not_repeat_after_cancel(self):
        task = self.engine.start("stop", self.key, "买", "开仓", 2, 105, 1)
        self.update(last=104)
        self.assertFalse(task.orders)
        self.update(last=105)
        self.assertEqual(len(task.orders), 1)
        self.controller.ledger.cancel_report(task.orders[0])
        self.update(last=106)
        self.assertEqual(len(task.orders), 1)
        self.assertEqual(task.status, "已终结")

    def test_sniper_respects_price_and_visible_volume(self):
        task = self.engine.start("sniper", self.key, "买", "开仓", 4, 100, 1)
        self.update()
        self.assertFalse(task.orders)
        self.update(asks=((100, 2),))
        self.assertEqual(self.controller.ledger.orders[task.orders[0]].quantity, 2)

    def test_spread_hedges_partial_fills_without_repeating_active_leg(self):
        task = self.engine.start("spread", self.key, "买", "开仓", 3, 100, 1, self.second)
        self.update()
        self.assertEqual(len(task.orders), 1)
        self.assertFalse(task.hedge_orders)
        self.fill(task.orders[0], 1)
        self.update(self.second)
        hedge = self.controller.ledger.orders[task.hedge_orders[0]]
        self.assertEqual((hedge.key, hedge.side, hedge.quantity), (self.second, "卖", 1))
        self.update()
        self.assertEqual(len(task.hedge_orders), 1)
        self.fill(hedge.id, 1)
        self.fill(task.orders[0], 2)
        self.update()
        self.assertEqual(self.controller.ledger.orders[task.hedge_orders[-1]].quantity, 2)
        self.assertEqual(len(task.orders), 1)
        self.fill(task.hedge_orders[-1], 2)
        self.update()
        self.assertEqual(task.status, "已完成")

    def test_day_or_session_change_does_not_replay_algorithms(self):
        task = self.engine.start("best", self.key, "买", "开仓", 2, 105, 1)
        self.update(trading_day="20261102")
        self.assertEqual(task.status, "已暂停")
        self.assertFalse(task.orders)
        self.update(trading_day="20261102")
        self.assertFalse(task.orders)
        with self.assertRaises(ValueError):
            self.controller.external = object()
            self.engine.start("best", self.key, "买", "开仓", 2, 105, 1)

    def test_manual_hedge_cancel_pauses_without_chasing_or_replaying(self):
        task = self.engine.start("spread", self.key, "买", "开仓", 2, 100, 1, self.second)
        self.update()
        self.fill(task.orders[0], 1)
        self.update()
        self.controller.ledger.cancel_report(task.hedge_orders[0])
        self.update(self.second, bids=((90, 20),))
        self.assertEqual(task.status, "已暂停")
        self.assertEqual(len(task.hedge_orders), 1)
        self.assertIn("残腿", task.message)


if __name__ == "__main__":
    unittest.main()
