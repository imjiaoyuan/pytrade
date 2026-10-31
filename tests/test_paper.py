import time
import unittest
from dataclasses import replace
from decimal import Decimal

from pytrade.domain import CATALOG, Ledger, Status
from pytrade.external import ConnectionConfig, ExternalSession
from pytrade.paper import PaperSimulator


_next_stamp = [0.0]


def next_stamp():
    current = time.monotonic()
    while current <= _next_stamp[0]:
        current = time.monotonic()
    _next_stamp[0] = current
    return current


class Gateway:
    closing = False

    def __init__(self):
        self.commands = []

    def send(self, command):
        self.commands.append(command)


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.key = "SHFE.au2612"
        self.gateway = Gateway()
        self.config = ConnectionConfig(md_front="tcp://127.0.0.1:1", paper_trading=True)
        self.session = ExternalSession(self.config, self.gateway, dict(CATALOG))
        self.session.retain(self.key)
        self.session.handle(dict(kind="md_state", epoch=1, status="合成行情"))
        self.session.handle(dict(kind="subscription", epoch=1, key=self.key, error=0))
        self.ledger = Ledger()
        self.paper = PaperSimulator(self.ledger, self.session)
        self.feed()

    def feed(self, **changes):
        row = dict(LastPrice="916.66", BidPrice1="916.64", BidVolume1=3, AskPrice1="916.68", AskVolume1=3, UpperLimitPrice="1000", LowerLimitPrice="800", TradingDay="20261030", UpdateTime="10:00:00", Volume=1)
        row.update(changes)
        self.session.handle(dict(kind="quote", epoch=self.session.md_epoch, key=self.key, received=next_stamp(), row=row))
        snapshot = self.session.snapshots.get(self.key)
        return self.paper.accept(snapshot, self.session.md_epoch) if snapshot else False

    def submit(self, quantity=1, group=1):
        return self.paper.submit(self.key, "买", "开仓", 45835, quantity, group)

    def test_pending_then_ack_then_fill_only_on_subsequent_quotes(self):
        order = self.submit(2)
        self.assertEqual(order.status, Status.PENDING)
        self.feed()
        self.assertEqual(order.status, Status.WORKING)
        self.assertEqual(order.filled, 0)
        self.feed()
        self.assertEqual(order.status, Status.FILLED)
        self.assertEqual(self.ledger.positions[(self.key, "买")].quantity, 2)
        self.assertTrue(all(command["command"] == "subscriptions" for command in self.gateway.commands))

    def test_displayed_liquidity_shared_fifo_and_unchanged_book_not_reused(self):
        first = self.submit(2, 1)
        second = self.submit(2, 2)
        self.feed()
        self.feed()
        self.assertEqual((first.filled, second.filled), (2, 1))
        snapshot = self.session.snapshots[self.key]
        self.assertFalse(self.paper.accept(snapshot, self.session.md_epoch))
        for _ in range(3):
            self.feed()
        self.assertEqual(second.filled, 1)
        self.feed(AskVolume1=4)
        self.assertEqual(second.filled, 2)
        self.assertEqual(self.ledger.risk.held(2), 2)

    def test_multiple_levels_respect_limit_and_decimal_profit(self):
        self.feed(AskVolume1=1, AskPrice2="916.70", AskVolume2=3)
        order = self.submit(3)
        self.feed(AskVolume1=1, AskPrice2="916.70", AskVolume2=3)
        self.feed(AskVolume1=1, AskPrice2="916.70", AskVolume2=3)
        self.assertEqual(order.filled, 3)
        self.assertEqual([fill.ticks for fill in self.ledger.fills], [45834, 45835])
        close = self.paper.submit(self.key, "卖", "平仓", 45832, 3)
        self.feed()
        self.feed()
        self.assertEqual(close.status, Status.FILLED)
        self.assertEqual(self.ledger.realized, Decimal("-160.00"))

    def test_stale_quotes_block_new_orders_and_matching(self):
        order = self.submit()
        snapshot = self.session.snapshots[self.key]
        object.__setattr__(snapshot, "received_monotonic", time.monotonic() - 100)
        with self.assertRaises(ValueError):
            self.submit()
        self.assertFalse(self.paper.accept(snapshot, 1))
        self.assertEqual(order.status, Status.PENDING)

    def test_pause_updates_marks_without_fills_and_requires_new_quotes_to_resume(self):
        order = self.submit()
        self.paper.paused = True
        self.feed(LastPrice="916.70")
        self.assertEqual(self.ledger.marks[self.key], 45835)
        self.assertEqual(order.status, Status.PENDING)
        with self.assertRaises(ValueError):
            self.submit()
        self.paper.paused = False
        self.feed()
        self.feed()
        self.assertEqual(order.status, Status.FILLED)

    def test_disconnect_cancels_local_orders_and_old_epoch_cannot_replay(self):
        order = self.submit()
        old = self.session.snapshots[self.key]
        self.session.handle(dict(kind="md_state", epoch=2, status="断线"))
        self.paper.suspend("断线撤销本地挂单")
        self.assertEqual(order.status, Status.CANCELED)
        self.assertFalse(self.paper.accept(old, 1))
        self.session.handle(dict(kind="subscription", epoch=2, key=self.key, error=0))
        self.feed()
        self.assertEqual(order.filled, 0)

    def test_positions_keep_subscription_after_dom_closes(self):
        order = self.submit()
        self.session.release(self.key)
        self.assertEqual(self.session.references[self.key], 1)
        self.feed()
        self.feed()
        self.assertEqual(order.status, Status.FILLED)
        self.assertEqual(self.session.references[self.key], 1)
        close = self.paper.submit(self.key, "卖", "平仓", 45832, 1)
        self.feed()
        self.feed()
        self.assertEqual(close.status, Status.FILLED)
        self.assertNotIn(self.key, self.session.references)

    def test_trading_day_change_cancels_pending_preserves_position_and_resets_day_risk(self):
        filled = self.submit()
        self.feed()
        self.feed()
        pending = self.paper.submit(self.key, "买", "开仓", 45000, 1)
        self.ledger.risk.cancel_count = 5
        self.feed(TradingDay="20261102")
        self.assertEqual(pending.status, Status.CANCELED)
        self.assertEqual(filled.status, Status.FILLED)
        self.assertEqual(self.ledger.positions[(self.key, "买")].quantity, 1)
        self.assertEqual(self.ledger.risk.day, "20261102")
        self.assertEqual(self.ledger.risk.cancel_count, 0)
        self.assertFalse(self.feed(TradingDay="20261030"))

    def test_paper_config_cannot_open_trader_or_enable_external_orders(self):
        for changes in (dict(td_front="tcp://127.0.0.1:2"), dict(allow_orders=True), dict(environment="readonly"), dict(probe_mode=True)):
            with self.assertRaises(ValueError):
                replace(self.config, **changes).validate()

    def test_invalid_day_and_limit_metadata_block_submission(self):
        self.feed(TradingDay="bad")
        with self.assertRaises(ValueError):
            self.submit()
        self.feed(UpperLimitPrice=float("inf"))
        with self.assertRaises(ValueError):
            self.submit()
        self.feed()
        self.session.catalog[self.key] = replace(CATALOG[self.key], product_class="2")
        with self.assertRaises(ValueError):
            self.submit()

    def test_worker_paper_guard_rejects_insert_even_if_allow_orders_is_mutated(self):
        import queue
        from pathlib import Path
        from pytrade.ctp_worker import Engine
        outgoing = queue.Queue()
        engine = Engine(self.config, outgoing, Path("/tmp"), None)
        engine.config.allow_orders = True
        engine.td_logged_in = engine.settled = engine.reconciled = True
        engine.command(dict(command="insert", row=dict(TradingDay="20261030", FrontID=1, SessionID=2, OrderRef="1")))
        event = outgoing.get_nowait()
        self.assertEqual(event["kind"], "insert_error")
        self.assertTrue(event["not_sent"])

    def test_late_quote_does_not_restore_consumed_book_or_fill_twice(self):
        order = self.submit(4)
        self.feed()
        previous = self.session.raw_quotes[self.key]
        self.feed()
        self.assertEqual(order.filled, 3)
        current = self.session.snapshots[self.key]
        self.session.handle(previous)
        self.assertIs(self.session.snapshots[self.key], current)
        self.assertFalse(self.paper.accept(current, 1))
        self.assertEqual(order.filled, 3)
