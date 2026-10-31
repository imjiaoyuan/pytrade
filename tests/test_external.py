import json
import queue
import time
import unittest
from decimal import Decimal
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from pytrade.ctp_worker import FIELDS, QUERY_TYPES, Engine, build_cancel, build_insert, make_market_spi, make_trade_spi
from pytrade.domain import Instrument
from pytrade.external import ConnectionConfig, ExternalSession, normalize_quote, order_key, redact


class FakeGateway:
    def __init__(self):
        self.commands = []

    def send(self, value):
        self.commands.append(value)


def config(**values):
    defaults = dict(md_front="tcp://127.0.0.1:10001", td_front="tcp://127.0.0.1:10002", broker="test", user="testuser", password="test-secret", allow_orders=True, max_order=5, max_position=5, max_total=20, max_group=10, max_cancels=100, max_daily_loss="1000")
    return ConnectionConfig(**(defaults | values))


def instrument_row():
    return dict(InstrumentID="au2612", ExchangeID="SHFE", InstrumentName="黄金", PriceTick=0.02, VolumeMultiple=1000, ExpireDate="20261231", IsTrading=1, ProductClass="1", MaxLimitOrderVolume=100, MinLimitOrderVolume=1)


def quote():
    return dict(InstrumentID="au2612", ExchangeID="SHFE", LastPrice=916.66000000001, BidPrice1=916.64, BidVolume1=3, AskPrice1=916.68, AskVolume1=2, Volume=10, TradingDay="20261030", ActionDay="20261029", UpdateTime="21:01:00", UpperLimitPrice=1000.0, LowerLimitPrice=800.0)


class ExternalTests(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.session = ExternalSession(config(), self.gateway)
        self.session.handle(dict(kind="td_login", front=1, session=2, max_ref="41", day="20261030"))
        self.finish_sync(instruments=True)
        self.session.settled = True
        self.session.retain("SHFE.au2612")
        self.session.handle(dict(kind="md_state", epoch=1, status="登录成功"))
        self.session.handle(dict(kind="subscription", epoch=1, key="SHFE.au2612", error=0))
        self.feed_quote()

    def finish_sync(self, instruments=False, positions=None, orders=None):
        session = self.session
        if not session.syncing:
            session.synchronize(instruments)
        cycle = session.sync_cycle
        records = dict(account=[dict(CurrencyID="CNY", Balance=100000, Available=100000, Deposit=0, Withdraw=0, TradingDay="20261030")], positions=positions or [], orders=orders or [], trades=[])
        if instruments:
            records["instruments"] = [instrument_row()]
            records["settlement"] = [dict(Content="test settlement")]
        for kind, rows in records.items():
            session.handle(dict(kind="query_rows", cycle=cycle, query=kind, rows=rows))
        session.handle(dict(kind="sync_done", cycle=cycle))

    def feed_quote(self, row=None, epoch=1):
        self.session.handle(dict(kind="quote", key="SHFE.au2612", epoch=epoch, row=row or quote(), received=time.monotonic()))

    def submit(self, side="买", offset="开仓", quantity=1):
        return self.session.submit("SHFE.au2612", side, offset, 45833, quantity)

    def test_config_protects_readonly_and_redacts(self):
        with self.assertRaises(ValueError):
            config(environment="readonly").validate()
        for value in ("http://host:123", "tcp://user:pass@host:123", "tcp://host", "tcp://host:999999"):
            with self.assertRaises(ValueError):
                config(md_front=value).validate()
        self.assertNotIn("test-secret", repr(config()))
        self.assertEqual(redact("testuser test-secret", config()), "*** ***")

    def test_request_is_pending_not_fill(self):
        key = self.submit(quantity=3)
        row = self.session.orders[key]
        self.assertEqual(row["OrderStatus"], "pending")
        self.assertEqual(row["OrderRef"], "42")
        self.assertEqual(row["VolumeTraded"], 0)
        self.assertFalse(self.session.ready)
        self.assertEqual(self.gateway.commands[-1]["command"], "insert")
        with self.assertRaises(ValueError):
            self.submit()

    def test_missing_query_category_never_marks_account_reconciled(self):
        self.session.synchronize()
        cycle = self.session.sync_cycle
        for kind in ("orders", "trades", "account"):
            rows = [dict(CurrencyID="CNY", Balance=100000, Available=100000, Deposit=0, Withdraw=0)] if kind == "account" else []
            self.session.handle(dict(kind="query_rows", cycle=cycle, query=kind, rows=rows))
        self.session.handle(dict(kind="sync_done", cycle=cycle))
        self.assertFalse(self.session.ready)
        self.assertTrue(self.session.syncing)
        self.assertGreater(self.session.sync_cycle, cycle)
        with self.assertRaises(ValueError):
            self.submit()

    def test_late_query_completion_after_disconnect_is_ignored(self):
        self.session.synchronize()
        cycle = self.session.sync_cycle
        self.session.handle(dict(kind="td_state", status="断线"))
        self.session.handle(dict(kind="query_rows", cycle=cycle, query="account", rows=[dict(Balance=999)]))
        self.session.handle(dict(kind="sync_done", cycle=cycle))
        self.assertFalse(self.session.ready)
        self.assertEqual(self.session.accounts[0]["Balance"], 100000)
        self.assertNotIn("account", self.session.query_rows)

    def test_conflicting_duplicate_trade_blocks_all_trading(self):
        trade = dict(TradingDay="20261030", ExchangeID="SHFE", InstrumentID="au2612", TradeID="conflict", Volume=1, Price="916.66")
        self.session.handle(dict(kind="trade", row=trade))
        self.session.handle(dict(kind="trade", row=trade | dict(Volume=2)))
        self.assertTrue(self.session.faulted)
        self.assertEqual(next(iter(self.session.trades.values()))["Volume"], 1)
        with self.assertRaises(ValueError):
            self.submit()

    def test_duplicate_position_snapshot_cannot_double_available_quantity(self):
        position = dict(ExchangeID="SHFE", InstrumentID="au2612", PosiDirection="2", PositionDate="1", HedgeFlag="1", Position=2)
        self.finish_sync(positions=[position, dict(position)])
        self.assertTrue(self.session.faulted)
        self.assertEqual(self.session.positions, [])
        with self.assertRaises(ValueError):
            self.submit("卖", "平今", 3)

    def test_negative_position_snapshot_blocks_trading(self):
        position = dict(ExchangeID="SHFE", InstrumentID="au2612", PosiDirection="2", PositionDate="1", HedgeFlag="1", Position=-2)
        self.finish_sync(positions=[position])
        self.assertTrue(self.session.faulted)

    def test_conflicting_trade_in_query_cannot_overwrite_realtime_fill(self):
        trade = dict(TradingDay="20261030", ExchangeID="SHFE", InstrumentID="au2612", TradeID="query-conflict", Volume=1, Price="916.66")
        self.session.handle(dict(kind="trade", row=trade))
        self.session.synchronize()
        cycle = self.session.sync_cycle
        for kind in ("positions", "orders"):
            self.session.handle(dict(kind="query_rows", cycle=cycle, query=kind, rows=[]))
        self.session.handle(dict(kind="query_rows", cycle=cycle, query="trades", rows=[trade | dict(Volume=3)]))
        self.session.handle(dict(kind="query_rows", cycle=cycle, query="account", rows=[dict(CurrencyID="CNY", Balance=1000, Available=1000, Deposit=0, Withdraw=0)]))
        self.session.handle(dict(kind="sync_done", cycle=cycle))
        self.assertTrue(self.session.faulted)
        self.assertEqual(next(iter(self.session.trades.values()))["Volume"], 1)

    def test_duplicate_account_currency_blocks_reconciliation(self):
        self.session.synchronize()
        cycle = self.session.sync_cycle
        for kind in ("positions", "orders", "trades"):
            self.session.handle(dict(kind="query_rows", cycle=cycle, query=kind, rows=[]))
        account = dict(CurrencyID="CNY", Balance=1000, Available=1000, Deposit=0, Withdraw=0)
        self.session.handle(dict(kind="query_rows", cycle=cycle, query="account", rows=[account, dict(account)]))
        self.session.handle(dict(kind="sync_done", cycle=cycle))
        self.assertTrue(self.session.faulted)
        self.assertFalse(self.session.ready)

    def test_limit_change_before_durable_dispatch_prevents_insert(self):
        self.session.journal_path = Path("unused")
        self.session.writer = SimpleNamespace(written=0, error="", submit=lambda state, **kwargs: 1)
        identifier = self.submit(quantity=2)
        self.session.config.max_total = 1
        self.session.writer.written = 1
        self.session.dispatch_pending()
        self.assertFalse(any(command["command"] == "insert" for command in self.gateway.commands))
        self.assertEqual(self.session.orders[identifier]["DispatchState"], "not_sent")

    def test_trade_arriving_before_durable_dispatch_requires_new_reconciliation(self):
        self.session.journal_path = Path("unused")
        self.session.writer = SimpleNamespace(written=0, error="", submit=lambda state, **kwargs: 1)
        identifier = self.submit()
        self.session.handle(dict(kind="trade", row=dict(TradingDay="20261030", ExchangeID="SHFE", InstrumentID="au2612", TradeID="external", Volume=1)))
        self.session.writer.written = 1
        self.session.dispatch_pending()
        self.assertFalse(any(command["command"] == "insert" for command in self.gateway.commands))
        self.assertEqual(self.session.orders[identifier]["DispatchState"], "not_sent")

    def test_missing_initial_quote_and_subscription_failure_are_visible(self):
        key = "SHFE.au2612"
        self.session.handle(dict(kind="subscription", key=key, epoch=1, error=9))
        self.assertNotIn(key, self.session.raw_quotes)
        self.session.quote_wait_started[key] = time.monotonic() - 100
        self.session.periodic()
        self.assertIn("无有效行情超时", self.session.md_status)
        self.session.release(key)
        self.session.handle(dict(kind="subscription", key=key, epoch=1, error=0))
        self.assertNotIn(key, self.session.subscribed)

    def test_only_verified_futures_may_trade(self):
        self.session.verified.clear()
        with self.assertRaises(ValueError):
            self.submit()
        self.session.verified.add("SHFE.au2612")
        self.session.catalog["SHFE.au2612"] = replace(self.session.catalog["SHFE.au2612"], max_order_volume=1)
        with self.assertRaises(ValueError):
            self.submit(quantity=2)

    def test_stale_missing_wrong_day_and_limits_block(self):
        for change in (dict(TradingDay="20261029"), dict(UpperLimitPrice=900), dict(LowerLimitPrice=950), dict(UpperLimitPrice=float("inf"))):
            self.feed_quote(quote() | change)
            with self.assertRaises(ValueError):
                self.submit()
        self.feed_quote()
        snapshot = self.session.snapshots["SHFE.au2612"]
        object.__setattr__(snapshot, "received_monotonic", time.monotonic() - 50)
        with self.assertRaises(ValueError):
            self.submit()

    def test_reconnect_drops_quotes_and_requires_new_settlement_sync(self):
        self.session.handle(dict(kind="md_state", status="断线", epoch=2))
        self.assertFalse(self.session.snapshots)
        self.feed_quote(epoch=1)
        self.assertFalse(self.session.snapshots)
        self.session.handle(dict(kind="td_state", status="断线"))
        self.assertFalse(self.session.logged_in)
        self.assertFalse(self.session.settled)
        with self.assertRaises(ValueError):
            self.submit()
        self.assertFalse(any(c["command"] == "insert" for c in self.gateway.commands))

    def test_no_invented_depth_and_tick_tolerance(self):
        instrument = self.session.catalog["SHFE.au2612"]
        normalized = normalize_quote(quote(), instrument, "source")
        self.assertEqual(normalized.last, 45833)
        self.assertEqual(len(normalized.bids), 1)
        self.assertEqual(normalized.trading_day, "20261030")
        self.assertEqual(normalized.action_day, "20261029")
        for value in (float("inf"), float("nan"), 1.7976931348623157e308, 916.661):
            with self.assertRaises((ValueError, ArithmeticError)):
                normalize_quote(quote() | {"LastPrice": value}, instrument, "source")
        normalized = normalize_quote(quote() | {"BidPrice2": 0, "BidVolume2": 20}, instrument, "source")
        self.assertEqual(len(normalized.bids), 1)

    def test_pending_risk_and_close_today_yesterday(self):
        positions = [dict(ExchangeID="SHFE", InstrumentID="au2612", PosiDirection="2", HedgeFlag="1", PositionDate="1", Position=3, ShortFrozen=1, LongFrozen=0), dict(ExchangeID="SHFE", InstrumentID="au2612", PosiDirection="2", HedgeFlag="1", PositionDate="2", Position=1, ShortFrozen=0, LongFrozen=0)]
        self.session.positions = positions
        for position in positions:
            self.session.assign_position(position, 1, position["Position"])
        self.assertEqual(self.session.close_available("SHFE.au2612", "1", "3"), 2)
        self.assertEqual(self.session.close_available("SHFE.au2612", "1", "4"), 1)
        with self.assertRaises(ValueError):
            self.submit("卖", "平仓")
        with self.assertRaises(ValueError):
            self.submit("卖", "平今", 3)
        key = self.submit("卖", "平今", 2)
        self.assertEqual(self.session.orders[key]["CombOffsetFlag"], "3")

    def test_pending_open_counts_towards_limit(self):
        self.session.positions = [dict(ExchangeID="SHFE", InstrumentID="au2612", PosiDirection="2", Position=4)]
        with self.assertRaises(ValueError):
            self.submit(quantity=2)
        key = self.submit(quantity=1)
        self.session.ready = True
        with self.assertRaises(ValueError):
            self.submit(quantity=1)
        self.session.handle(dict(kind="insert_error", order_key=key, message="rejected"))
        self.assertEqual(self.session.orders[key]["OrderStatus"], "rejected")

    def test_duplicate_and_late_order_reports_never_regress(self):
        key = self.submit(quantity=3)
        row = self.session.orders[key] | dict(OrderStatus="1", VolumeTraded=1, OrderSysID="SYS")
        self.session.handle(dict(kind="order", row=row))
        revision = self.session.revision
        self.session.handle(dict(kind="order", row=dict(row)))
        self.assertEqual(self.session.revision, revision)
        self.session.handle(dict(kind="order", row=row | dict(OrderStatus="3", VolumeTraded=0)))
        self.assertEqual(self.session.orders[key]["VolumeTraded"], 1)
        self.session.handle(dict(kind="order", row=row | dict(OrderStatus="5")))
        self.session.handle(dict(kind="order", row=row | dict(OrderStatus="1", VolumeTraded=2)))
        self.assertEqual(self.session.orders[key]["OrderStatus"], "5")
        self.assertEqual(self.session.orders[key]["VolumeTraded"], 2)
        self.session.handle(dict(kind="order", row=row | dict(OrderStatus="0", VolumeTraded=3)))
        self.assertEqual(self.session.orders[key]["OrderStatus"], "0")

    def test_duplicate_trades_do_not_change_position_snapshot(self):
        row = dict(TradingDay="20261030", ExchangeID="SHFE", InstrumentID="au2612", TradeID="T1", Volume=1)
        self.session.handle(dict(kind="trade", row=row))
        revision = self.session.revision
        self.session.handle(dict(kind="trade", row=dict(row)))
        self.assertEqual(len(self.session.trades), 1)
        self.assertEqual(self.session.revision, revision)
        self.assertEqual(self.session.positions, [])
        self.assertFalse(self.session.ready)

    def test_trade_during_query_forces_another_sync(self):
        self.session.synchronize()
        cycle = self.session.sync_cycle
        self.session.handle(dict(kind="trade", row=dict(TradingDay="20261030", ExchangeID="SHFE", InstrumentID="au2612", TradeID="late")))
        self.session.handle(dict(kind="query_rows", cycle=cycle, query="account", rows=[dict(Balance=1)]))
        self.session.handle(dict(kind="sync_done", cycle=cycle))
        self.assertFalse(self.session.ready)
        self.assertTrue(self.session.syncing)
        self.assertGreater(self.session.sync_cycle, cycle)

    def test_cancel_failure_and_unknown_blocking(self):
        key = self.submit()
        self.session.ready = False
        self.session.orders[key]["OrderStatus"] = "3"
        self.session.cancel(key)
        self.assertIn(key, self.session.canceling)
        self.session.handle(dict(kind="cancel_error", order_key=key, message="cancel failed"))
        self.assertEqual(self.session.orders[key]["OrderStatus"], "3")
        self.assertNotIn(key, self.session.canceling)
        self.session.orders[key]["OrderStatus"] = "a"
        self.finish_sync()
        self.assertFalse(self.session.ready)

    def test_day_is_part_of_order_identity(self):
        row = dict(FrontID=1, SessionID=2, OrderRef="1", TradingDay="20261030")
        self.assertNotEqual(order_key(row), order_key(row | dict(TradingDay="20261102")))

    def test_journal_survives_restart_and_broken_journal_blocks(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            self.session.journal_path = path
            key = self.submit()
            self.session.flush()
            stored = path.read_text()
            self.assertNotIn("test-secret", stored)
            self.assertNotIn("testuser", stored)
            self.session.close()
            restored = ExternalSession(config(), FakeGateway(), journal_path=path)
            self.assertEqual(restored.orders[key]["OrderStatus"], "pending")
            restored.close()
            path.write_text("broken")
            damaged = ExternalSession(config(), FakeGateway(), journal_path=path)
            self.assertTrue(damaged.faulted)
            damaged.close()

    def test_readonly_blocks_both_insert_and_cancel(self):
        self.session.config.allow_orders = False
        with self.assertRaises(ValueError):
            self.submit()
        with self.assertRaises(ValueError):
            self.session.cancel("any")


    def test_new_limits_must_be_explicit_before_external_orders(self):
        with self.assertRaises(ValueError):
            config(max_total=0).validate()
        with self.assertRaises(ValueError):
            config(max_daily_loss="NaN").validate()

    def test_delayed_journal_dispatch_and_disconnect_never_replays(self):
        class Writer:
            written = 0
            error = ""
            def submit(self, state, **kwargs):
                return 1
        session = self.session
        session.journal_path = Path("unused")
        session.writer = Writer()
        key = self.submit()
        self.assertEqual(session.orders[key]["OrderStatus"], "pending")
        self.assertFalse(any(command["command"] == "insert" for command in self.gateway.commands))
        session.writer.written = 1
        session.dispatch_pending()
        session.dispatch_pending()
        self.assertEqual(sum(command["command"] == "insert" for command in self.gateway.commands), 1)
        session.ready = True
        session.writer.written = 0
        self.submit()
        session.handle(dict(kind="td_state", status="断线"))
        session.writer.written = 1
        session.dispatch_pending()
        self.assertEqual(sum(command["command"] == "insert" for command in self.gateway.commands), 1)

    def test_failed_background_write_blocks_pending_insert(self):
        class Writer:
            written = 0
            error = ""
            def submit(self, state, **kwargs):
                return 1
        self.session.journal_path = Path("unused")
        self.session.writer = Writer()
        self.submit()
        self.session.writer.error = "disk failure"
        self.session.dispatch_pending()
        self.assertTrue(self.session.faulted)
        self.assertFalse(any(command["command"] == "insert" for command in self.gateway.commands))

    def test_old_market_state_and_subscription_error_do_not_clear_new_quote(self):
        snapshot = self.session.snapshots["SHFE.au2612"]
        self.session.handle(dict(kind="subscription", key="SHFE.au2612", epoch=0, error=1))
        self.session.handle(dict(kind="md_state", epoch=0, status="旧断线"))
        self.assertIs(self.session.snapshots["SHFE.au2612"], snapshot)
        self.session.release("SHFE.au2612")
        self.finish_sync()
        self.assertNotIn("SHFE.au2612", self.session.snapshots)

    def test_group_is_preserved_in_reports_and_partial_fills(self):
        key = self.session.submit("SHFE.au2612", "买", "开仓", 45833, 3, group=2)
        report = {name: value for name, value in self.session.orders[key].items() if name not in {"Group", "StopID", "Strategy"}}
        report.update(OrderStatus="1", VolumeTraded=1, OrderSysID="SYS")
        self.session.handle(dict(kind="order", row=report))
        self.assertEqual(self.session.orders[key]["Group"], 2)
        self.session.handle(dict(kind="trade", row=dict(TradingDay="20261030", ExchangeID="SHFE", InstrumentID="au2612", TradeID="T2", OrderSysID="SYS", Direction="0", OffsetFlag="0", Volume=1)))
        row = dict(ExchangeID="SHFE", InstrumentID="au2612", PosiDirection="2", HedgeFlag="1", PositionDate="1", Position=1)
        self.finish_sync(positions=[row], orders=[report])
        self.assertEqual(self.session.risk.held(2), 1)
        self.finish_sync(positions=[row], orders=[report])
        self.assertEqual(self.session.risk.held(2), 1)

    def test_cancel_failure_keeps_count_but_unsent_releases(self):
        key = self.submit()
        self.session.orders[key]["OrderStatus"] = "3"
        self.session.cancel(key)
        token = self.gateway.commands[-1]["cancel_token"]
        self.session.handle(dict(kind="cancel_error", order_key=key, message="not sent", not_sent=True, cancel_token=token))
        self.assertEqual(self.session.risk.cancel_count, 0)
        self.session.cancel(key)
        self.session.handle(dict(kind="cancel_error", order_key=key, message="exchange rejection"))
        self.assertEqual(self.session.risk.cancel_count, 1)

    def test_stop_keeps_subscription_after_dom_release(self):
        row = dict(ExchangeID="SHFE", InstrumentID="au2612", PosiDirection="2", HedgeFlag="1", PositionDate="1", Position=2)
        self.session.positions = [row]
        self.session.assign_position(row, 1, 2)
        stop = self.session.stops.add("SHFE.au2612", "买", "平今", 45800, 1, 1)
        self.session.stops.arm(stop.id)
        self.session.release("SHFE.au2612")
        self.assertEqual(self.session.references["SHFE.au2612"], 1)
        self.session.stops.pause(stop.id)
        self.assertNotIn("SHFE.au2612", self.session.references)

    def test_subscription_failure_retries_with_backoff(self):
        module = SimpleNamespace(CThostFtdcMdSpi=object, CThostFtdcReqUserLoginField=Field)
        engine = Engine(config(), queue.Queue(100), Path("/tmp"), module)
        calls = []
        engine.md = SimpleNamespace(SubscribeMarketData=lambda symbols, size: calls.append(symbols) or 0, UnSubscribeMarketData=lambda symbols, size: 0)
        engine.md_logged_in = True
        engine.wanted = {"SHFE.au2612"}
        spi = make_market_spi(module, engine)
        engine.update_subscriptions()
        spi.OnRspSubMarketData(SimpleNamespace(InstrumentID="au2612"), SimpleNamespace(ErrorID=1), 1, True)
        engine.pump()
        self.assertEqual(len(calls), 1)
        engine.retry_at["SHFE.au2612"] = 0
        engine.pump()
        self.assertEqual(len(calls), 2)


    def test_archive_preserves_unknown_orders_and_keeps_terminal_history(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'transactions.json'
            session = self.session
            session.journal_path = path
            old = dict(TradingDay='20261029', FrontID=1, SessionID=1, OrderRef='1', OrderStatus='0')
            unknown = old | dict(OrderRef='2', OrderStatus='a')
            session.orders[order_key(old)] = old
            session.orders[order_key(unknown)] = unknown
            session.archive_history()
            session.persist()
            session.flush()
            self.assertNotIn(order_key(old), session.orders)
            self.assertIn(order_key(unknown), session.orders)
            archives = list((path.parent / 'archive').glob('*.json'))
            self.assertEqual(len(archives), 1)
            archived = json.loads(archives[0].read_text())
            self.assertIn(order_key(old), archived['orders'])
            self.assertNotIn(order_key(old), json.loads(path.read_text())['orders'])
            session.close()


    def test_market_disconnect_while_journal_waits_rejects_without_sending(self):
        class Writer:
            written = 0
            error = ""
            def submit(self, state, **kwargs):
                return 1
        self.session.journal_path = Path("unused")
        self.session.writer = Writer()
        key = self.submit()
        self.session.handle(dict(kind="md_state", epoch=2, status="断线"))
        self.session.writer.written = 1
        self.session.dispatch_pending()
        self.assertFalse(any(command["command"] == "insert" for command in self.gateway.commands))
        self.assertEqual(self.session.orders[key]["OrderStatus"], "rejected")
        self.assertEqual(self.session.orders[key]["DispatchState"], "not_sent")

    def test_future_timestamp_and_unknown_message_version_block_trading(self):
        snapshot = self.session.snapshots["SHFE.au2612"]
        object.__setattr__(snapshot, "received_monotonic", time.monotonic() + 100)
        with self.assertRaises(ValueError):
            self.submit()
        self.session.handle(dict(version=2, kind="settled"))
        self.assertTrue(self.session.faulted)
        self.assertFalse(self.session.logged_in)


    def test_journal_snapshot_order_rows_do_not_change_after_new_report(self):
        key = self.submit()
        snapshot = self.session.journal_state()
        report = self.session.orders[key] | dict(OrderStatus="3", OrderSysID="SYS")
        self.session.handle(dict(kind="order", row=report))
        self.assertEqual(snapshot["orders"][key]["OrderStatus"], "pending")
        self.assertEqual(self.session.orders[key]["OrderStatus"], "3")

    def test_metadata_refresh_removes_stale_trading_permission(self):
        self.session.synchronize(instruments=True)
        cycle = self.session.sync_cycle
        for name in ("instruments", "orders", "trades", "positions"):
            self.session.handle(dict(kind="query_rows", query=name, cycle=cycle, rows=[]))
        self.session.handle(dict(kind="query_rows", query="account", cycle=cycle, rows=[dict(CurrencyID="CNY", Balance=100000, Available=100000, Deposit=0, Withdraw=0)]))
        self.session.handle(dict(kind="sync_done", cycle=cycle))
        self.assertFalse(self.session.verified)
        self.assertFalse(self.session.ready)
        with self.assertRaises(ValueError):
            self.submit()

    def test_malformed_order_event_faults_and_clears_quotes(self):
        self.session.handle(dict(kind="order", row=dict(VolumeTraded="bad", VolumeTotalOriginal=1)))
        self.assertTrue(self.session.faulted)
        self.assertFalse(self.session.snapshots)
        with self.assertRaises(ValueError):
            self.submit()


class Field:
    def __init__(self):
        self.BrokerID = ""
        self.InvestorID = ""


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.module = SimpleNamespace(CThostFtdcTraderSpi=object, CThostFtdcInputOrderField=Field, CThostFtdcInputOrderActionField=Field, CThostFtdcQryOrderField=Field, THOST_FTDC_HF_Speculation="1", THOST_FTDC_OPT_LimitPrice="2", THOST_FTDC_TC_GFD="3", THOST_FTDC_VC_AV="1", THOST_FTDC_CC_Immediately="1", THOST_FTDC_FCC_NotForceClose="0", THOST_FTDC_AF_Delete="0")
        self.outgoing = queue.Queue(maxsize=100)
        self.engine = Engine(config(), self.outgoing, Path("/tmp"), None, self.module)
        self.spi = make_trade_spi(self.module, self.engine)

    def test_exact_request_and_cancel_identifiers(self):
        row = dict(ExchangeID="SHFE", InstrumentID="au2612", OrderRef="42", Direction="1", CombOffsetFlag="3", LimitPrice="916.66", VolumeTotalOriginal=2, FrontID=9, SessionID=11, OrderSysID=" 123")
        request = build_insert(self.module, config(), row)
        self.assertEqual(request.CombOffsetFlag, "3")
        self.assertEqual(request.VolumeCondition, "1")
        self.assertEqual(request.OrderPriceType, "2")
        self.assertEqual(Decimal(str(request.LimitPrice)), Decimal("916.66"))
        cancel = build_cancel(self.module, config(), row)
        self.assertEqual((cancel.FrontID, cancel.SessionID, cancel.OrderRef, cancel.OrderSysID), (9, 11, "42", " 123"))

    def test_child_process_guard_independent_of_ui(self):
        row = dict(FrontID=1, SessionID=2, OrderRef="1")
        self.engine.command(dict(command="insert", row=row))
        self.assertEqual(self.outgoing.get_nowait()["kind"], "insert_error")
        self.engine.config.environment = "readonly"
        self.engine.td_logged_in = self.engine.reconciled = self.engine.settled = True
        self.engine.command(dict(command="cancel", row=row))
        self.assertEqual(self.outgoing.get_nowait()["kind"], "cancel_error")

    def test_query_error_never_completes_reconciliation(self):
        self.engine.sync = 1
        self.engine.query = dict(id=2, kind="orders", cycle=1, rows=[], error=False, done=False, started=time.monotonic())
        self.spi.OnRspQryOrder(None, SimpleNamespace(ErrorID=3), 2, True)
        self.engine.pump()
        events = []
        while not self.outgoing.empty():
            events.append(self.outgoing.get_nowait())
        self.assertFalse(self.engine.reconciled)
        self.assertFalse(any(event["kind"] == "sync_done" for event in events))

    def test_market_reconnect_relogs_and_resubscribes_without_trades(self):
        module = SimpleNamespace(CThostFtdcMdSpi=object, CThostFtdcReqUserLoginField=Field)
        logins, subscriptions, removals = [], [], []
        self.engine.md = SimpleNamespace(ReqUserLogin=lambda request, request_id: logins.append(request) or 0, SubscribeMarketData=lambda symbols, size: subscriptions.append(symbols) or 0, UnSubscribeMarketData=lambda symbols, size: removals.append(symbols) or 0)
        self.engine.md_module = module
        self.engine.wanted = {"SHFE.au2612"}
        spi = make_market_spi(module, self.engine)
        spi.OnFrontConnected()
        spi.OnRspUserLogin(None, None, 1, True)
        self.assertEqual(subscriptions, [[b"au2612"]])
        spi.OnFrontDisconnected(1)
        self.assertFalse(self.engine.md_logged_in)
        spi.OnFrontConnected()
        spi.OnRspUserLogin(None, None, 2, True)
        self.assertEqual(len(logins), 2)
        self.assertEqual(len(subscriptions), 2)
        self.engine.command(dict(command="subscriptions", keys=[]))
        self.assertEqual(removals, [[b"au2612"]])
        self.assertFalse(self.engine.td_logged_in)

    def test_market_sdk_uses_independent_credentials_and_anonymous_sends_none(self):
        module = SimpleNamespace(CThostFtdcMdSpi=object, CThostFtdcReqUserLoginField=Field)
        requests = []
        self.engine.md = SimpleNamespace(ReqUserLogin=lambda request, request_id: requests.append(request) or 0)
        self.engine.config = config(md_anonymous=False, md_separate_credentials=True, md_broker="md-broker", md_user="md-user", md_password="md-password")
        spi = make_market_spi(module, self.engine)
        spi.OnFrontConnected()
        self.assertEqual((requests[-1].BrokerID, requests[-1].UserID, requests[-1].Password), ("md-broker", "md-user", "md-password"))
        self.assertEqual(self.engine.config.user, "testuser")
        self.engine.config.md_separate_credentials = False
        spi.OnFrontConnected()
        self.assertEqual((requests[-1].BrokerID, requests[-1].UserID, requests[-1].Password), ("test", "testuser", "test-secret"))
        self.engine.config.md_anonymous = True
        spi.OnFrontConnected()
        self.assertEqual(requests[-1].BrokerID, "")
        self.assertFalse(hasattr(requests[-1], "Password"))
        for secret in ("md-user", "md-password"):
            self.assertNotIn(secret, repr(self.engine.config))
            self.assertNotIn(secret, redact(secret, self.engine.config))

    def test_authenticated_market_config_requires_complete_selected_account(self):
        for values in (dict(md_anonymous=False, user=""), dict(md_anonymous=False, md_separate_credentials=True), dict(probe_mode=True, td_front="", allow_orders=False, user="", password="", md_user="md-user")):
            with self.assertRaises(ValueError):
                config(**values).validate()

    def test_transaction_queue_overflow_enters_fault(self):
        self.engine.outgoing = queue.Queue(maxsize=1)
        self.engine.emit("order", row={})
        self.engine.emit("trade", row={})
        self.assertTrue(self.engine.failed.is_set())
        self.engine.outgoing.get()
        self.engine.pump()
        self.assertEqual(self.engine.outgoing.get()["kind"], "fatal")

    def test_sdk_rechecks_market_epoch_and_freshness_before_insert(self):
        calls = []
        self.engine.td = SimpleNamespace(ReqOrderInsert=lambda request, request_id: calls.append(request) or 0)
        self.engine.td_logged_in = True
        self.engine.settled = True
        self.engine.reconciled = True
        row = dict(TradingDay="20261030", ExchangeID="SHFE", InstrumentID="au2612", OrderRef="42", FrontID=1, SessionID=2, Direction="0", CombOffsetFlag="0", LimitPrice="916.66", VolumeTotalOriginal=1)
        self.engine.trading_day, self.engine.front_id, self.engine.session_id = "20261030", 1, 2
        self.engine.md_epoch = 2
        self.engine.command(dict(command="insert", row=row, quote_epoch=2, quote_received=time.monotonic()))
        self.assertFalse(calls)
        self.engine.md_logged_in = True
        self.engine.command(dict(command="insert", row=row, quote_epoch=1, quote_received=time.monotonic()))
        self.assertFalse(calls)
        self.engine.command(dict(command="insert", row=row, quote_epoch=2, quote_received=time.monotonic() - 100))
        self.assertFalse(calls)
        self.engine.command(dict(command="insert", row=row, quote_epoch=2, quote_received=time.monotonic()))
        self.assertEqual(len(calls), 1)

    def test_sdk_rejects_queued_insert_from_previous_trade_session(self):
        calls = []
        self.engine.td = SimpleNamespace(ReqOrderInsert=lambda request, request_id: calls.append(request) or 0)
        self.engine.td_logged_in = self.engine.reconciled = self.engine.settled = self.engine.md_logged_in = True
        self.engine.trading_day, self.engine.front_id, self.engine.session_id = "20261102", 7, 9
        row = dict(TradingDay="20261030", FrontID=7, SessionID=8, OrderRef="1")
        self.engine.command(dict(command="insert", row=row, quote_epoch=0, quote_received=time.monotonic()))
        event = self.outgoing.get_nowait()
        self.assertTrue(event["not_sent"])
        self.assertFalse(calls)

    def test_missing_subscription_ack_times_out_and_retries(self):
        subscriptions = []
        self.engine.md = SimpleNamespace(SubscribeMarketData=lambda symbols, size: subscriptions.append(symbols) or 0, UnSubscribeMarketData=lambda symbols, size: 0)
        self.engine.md_logged_in = True
        self.engine.wanted = {"SHFE.au2612"}
        self.engine.update_subscriptions()
        self.engine.requested_at["SHFE.au2612"] = time.monotonic() - 11
        self.engine.update_subscriptions()
        self.assertEqual(self.outgoing.get_nowait()["error"], -10001)
        self.assertEqual(len(subscriptions), 1)
        self.engine.retry_at["SHFE.au2612"] = 0
        self.engine.update_subscriptions()
        self.assertEqual(len(subscriptions), 2)

    def test_empty_query_emits_category_before_completion(self):
        self.engine.sync = 1
        self.engine.query = dict(id=2, kind="positions", cycle=1, rows=[], error=False, done=True, started=time.monotonic())
        self.engine.pump()
        self.assertEqual(self.outgoing.get_nowait(), dict(version=1, kind="query_rows", query="positions", cycle=1, rows=[]))
        self.assertEqual(self.outgoing.get_nowait()["kind"], "sync_done")

    def test_full_query_pipeline_waits_for_responses_before_ready(self):
        callbacks = {"instruments": "OnRspQryInstrument", "settlement": "OnRspQrySettlementInfo", "orders": "OnRspQryOrder", "trades": "OnRspQryTrade", "positions": "OnRspQryInvestorPosition", "account": "OnRspQryTradingAccount"}
        calls = []
        api = SimpleNamespace()
        for kind, (class_name, method_name) in QUERY_TYPES.items():
            setattr(self.module, class_name, Field)
            def request(field, request_id, query=kind):
                self.assertEqual(field.BrokerID, "test")
                self.assertEqual(field.InvestorID, "testuser")
                calls.append(query)
                return 0
            setattr(api, method_name, request)
        self.engine.td = api
        self.engine.td_logged_in = True
        self.engine.command(dict(command="sync", cycle=3, instruments=True))
        while self.engine.sync:
            self.engine.next_query = 0
            self.engine.pump()
            active = self.engine.query
            if active:
                self.assertFalse(self.engine.reconciled)
                query = active["kind"]
                values = {name: "" for name in FIELDS[query]}
                row = SimpleNamespace(**values)
                getattr(self.spi, callbacks[query])(row, None, active["id"], True)
        self.assertEqual(calls, ["instruments", "settlement", "orders", "trades", "positions", "account"])
        self.assertTrue(self.engine.reconciled)
        events = []
        while not self.outgoing.empty():
            events.append(self.outgoing.get_nowait())
        self.assertEqual(events[-1], dict(version=1, kind="sync_done", cycle=3))
        self.assertEqual(len([event for event in events if event["kind"] == "query_rows"]), 6)


if __name__ == "__main__":
    unittest.main()
