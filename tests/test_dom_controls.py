import os
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from pytrade.app import Controller
from pytrade.dom_controls import DomControls
from pytrade.domain import CATALOG, Fill, Ledger, Snapshot, Status
from pytrade.external import ConnectionConfig, normalize_quote


class DomPolicyTests(unittest.TestCase):
    def setUp(self):
        self.key = "SHFE.ag2612"
        self.snapshot = Snapshot(self.key, 100, ((99, 3),), ((101, 4),), 50, "10:00:00", upper_limit=110, lower_limit=90)
        self.ledger = Ledger()

    def validate(self, controls, ticks=100, offset="开仓", snapshot=None):
        controls.validate(snapshot or self.snapshot, "买", offset, ticks, self.ledger.orders.values())

    def test_only_open_rejects_close_without_placing_order(self):
        with self.assertRaisesRegex(ValueError, "仅允许开仓"):
            self.validate(DomControls(only_open=True), offset="平仓")
        self.assertFalse(self.ledger.orders)

    def test_nearby_includes_boundary_and_rejects_outside(self):
        controls = DomControls(nearby=True, nearby_ticks=5)
        self.validate(controls, 95)
        self.validate(controls, 105)
        for ticks in (94, 106):
            with self.assertRaisesRegex(ValueError, "近价"):
                self.validate(controls, ticks)

    def test_limit_lock_rejects_limits_and_missing_values(self):
        controls = DomControls(limit_lock=True)
        self.validate(controls, 100)
        for ticks in (89, 90, 110, 111):
            with self.assertRaises(ValueError):
                self.validate(controls, ticks)
        with self.assertRaisesRegex(ValueError, "缺失"):
            self.validate(controls, snapshot=replace(self.snapshot, upper_limit=None))

    def test_white_lock_only_accepts_visible_book_levels(self):
        controls = DomControls(white_lock=True)
        self.validate(controls, 99)
        self.validate(controls, 101)
        with self.assertRaisesRegex(ValueError, "白格"):
            self.validate(controls, 100)

    def test_pending_lock_includes_partial_and_releases_after_cancel(self):
        controls = DomControls(pending_lock=True)
        order = self.ledger.submit(self.key, "买", "开仓", 100, 2)
        self.ledger.acknowledge(order.id)
        self.ledger.apply_fill(Fill("f1", order.id, self.key, "买", "开仓", 99, 1))
        with self.assertRaisesRegex(ValueError, "已有模拟挂单"):
            self.validate(controls)
        self.validate(controls, ticks=101)
        controls.validate(self.snapshot, "卖", "开仓", 100, self.ledger.orders.values())
        self.ledger.cancel_report(order.id)
        self.validate(controls)

    def test_cancel_cap_applies_to_ledger_and_resets_on_new_day(self):
        self.ledger.risk_day = "20261030"
        self.ledger.cancel_caps[self.key] = 1
        first = self.ledger.submit(self.key, "买", "开仓", 100, 1)
        self.assertFalse(self.ledger.cancel_report(first.id, accepted=False))
        self.assertEqual(self.ledger.cancel_counts.get(self.key, 0), 0)
        self.assertTrue(self.ledger.cancel_report(first.id))
        second = self.ledger.submit(self.key, "买", "开仓", 100, 1)
        with self.assertRaisesRegex(ValueError, "撤单限制"):
            self.ledger.cancel_report(second.id)
        self.assertEqual(second.status, Status.PENDING)
        self.ledger.risk_day = "20261102"
        self.assertTrue(self.ledger.cancel_report(second.id))

    def test_no_cancel_does_not_change_order_or_global_risk_counter(self):
        self.ledger.cancel_caps[self.key] = 0
        order = self.ledger.submit(self.key, "买", "开仓", 100, 1)
        with self.assertRaises(ValueError):
            self.ledger.cancel_report(order.id)
        self.assertEqual(order.status, Status.PENDING)
        self.assertEqual(self.ledger.risk.cancel_count, 0)

    def test_market_optional_fields_are_decimal_and_invalid_stays_missing(self):
        row = dict(LastPrice=100, PreSettlementPrice=99, OpenInterest=200, PreOpenInterest=190, UpperLimitPrice=110, LowerLimitPrice=90)
        quote = normalize_quote(row, CATALOG[self.key], "test")
        self.assertEqual((quote.pre_settlement, quote.open_interest, quote.pre_open_interest), (Decimal(99), Decimal(200), Decimal(190)))
        self.assertEqual((quote.upper_limit, quote.lower_limit), (110, 90))
        row.update(PreSettlementPrice=0, OpenInterest=1.7976931348623157e308, PreOpenInterest=-1, UpperLimitPrice="nan")
        quote = normalize_quote(row, CATALOG[self.key], "test")
        self.assertIsNone(quote.pre_settlement)
        self.assertIsNone(quote.open_interest)
        self.assertIsNone(quote.pre_open_interest)
        self.assertIsNone(quote.upper_limit)


class DomControlUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.controller = Controller(Path(self.directory.name))
        self.window = self.controller.open_dom("SHFE.ag2612")
        self.controller.timer.stop()

    def tearDown(self):
        self.controller.close()
        for _ in range(3):
            self.app.processEvents()
        self.directory.cleanup()

    def test_only_open_blocks_button_submission(self):
        self.window.local_controls["only_open"].setChecked(True)
        with patch("pytrade.dom_ui.QMessageBox.warning") as warning:
            self.window.submit("卖", "平仓")
        self.assertFalse(self.controller.ledger.orders)
        self.assertIn("仅允许开仓", warning.call_args.args[-1])

    def test_click_quantities_are_independent_and_right_defaults_disabled(self):
        window = self.window
        window.click_mode.setCurrentIndex(window.click_mode.findData("mouse_open"))
        index = window.model.index(window.model.rows // 2, 1)
        window.quantity.setValue(5)
        window.left_quantity.setValue(3)
        window.price_action(index)
        self.assertEqual(next(iter(self.controller.ledger.orders.values())).quantity, 3)
        window.price_action(index, right=True)
        self.assertEqual(len(self.controller.ledger.orders), 1)
        window.right_quantity.setValue(2)
        window.price_action(index, right=True)
        self.assertEqual(list(self.controller.ledger.orders.values())[-1].quantity, 2)
        self.assertEqual(window.quantity.value(), 5)

    def test_partial_fill_marks_actual_price_and_counts_one_order(self):
        ledger = self.controller.ledger
        key = self.window.instrument.key
        order = ledger.submit(key, "买", "开仓", 100, 3)
        ledger.acknowledge(order.id)
        for n in (1, 2):
            ledger.apply_fill(Fill(f"f{n}", order.id, key, "买", "开仓", 99, 1))
        self.window.refresh_marks()
        self.assertEqual(self.window.model.pending_by_price, {100: 1})
        self.assertEqual(self.window.model.filled_by_price, {99: 2})
        self.assertEqual(self.window.counters.text(), "C:0　T:1")
        ledger.cancel_report(order.id)
        self.window.refresh_marks()
        self.assertEqual(self.window.model.pending_by_price, {})
        self.assertEqual(self.window.model.filled_by_price, {99: 2})
        self.assertEqual(self.window.counters.text(), "C:1　T:1")

    def test_step_preserves_contract_tick_and_rejects_fractional_tick(self):
        window = self.controller.open_dom("SHFE.au2612")
        window.ladder_step.setValue(0.06)
        window.set_ladder_step()
        self.assertEqual(window.model.price_step, 3)
        self.assertEqual(window.model.price_at(0) - window.model.price_at(1), 3)
        window.ladder_step.setValue(0.03)
        window.set_ladder_step()
        self.assertEqual(window.model.price_step, 3)
        self.assertEqual(window.instrument.tick, Decimal("0.02"))

    def test_cancel_policy_is_shared_between_windows_and_all_manual_paths(self):
        window = self.window
        other = self.controller.open_dom(window.instrument.key, group=2)
        window.cancel_policy.setCurrentIndex(window.cancel_policy.findData(0))
        self.assertEqual(other.cancel_policy.currentData(), 0)
        order = self.controller.ledger.submit(window.instrument.key, "买", "开仓", 1, 1)
        self.controller.cancel_orders(window.instrument.key, shortcut=True)
        self.assertEqual(order.status, Status.PENDING)
        window.close()
        self.controller.cancel_one(order.id)
        self.assertEqual(order.status, Status.PENDING)
        other.cancel_policy.setCurrentIndex(other.cancel_policy.findData(None))
        self.controller.cancel_one(order.id)
        self.assertEqual(order.status, Status.CANCELED)

    def test_market_stats_show_missing_values_and_use_previous_settlement(self):
        snapshot = Snapshot(self.window.instrument.key, 101, (), (), 50, "10:00:00", pre_settlement=Decimal(100), open_interest=Decimal(200), pre_open_interest=Decimal(210))
        self.window.refresh_market_stats(snapshot)
        self.assertEqual(self.window.summary.text(), "+1.00%")
        self.assertEqual(self.window.market_stats.text(), "总持仓 200\n增减仓 -10")
        self.window.refresh_market_stats(replace(snapshot, pre_settlement=None, pre_open_interest=None))
        self.assertEqual(self.window.summary.text(), "涨跌幅 —")
        self.assertIn("增减仓 —", self.window.market_stats.text())

    def test_external_canceled_order_has_no_pending_mark_and_fill_uses_trade_price(self):
        from types import SimpleNamespace
        key = self.window.instrument.key
        session = SimpleNamespace(config=ConnectionConfig(md_front="tcp://127.0.0.1:2", td_front="tcp://127.0.0.1:1"), orders={"a": dict(ExchangeID="SHFE", InstrumentID="ag2612", OrderStatus="5", VolumeTotalOriginal=3, VolumeTraded=1, LimitPrice=100)}, trades={"f": dict(ExchangeID="SHFE", InstrumentID="ag2612", Price=99, Volume=1)})
        with patch.object(self.controller, "external", session):
            self.window.refresh_marks()
            self.window.refresh_controls(False)
            self.assertFalse(self.window.local_controls["only_open"].isEnabled())
            self.assertFalse(self.window.cancel_policy.isEnabled())
            self.assertEqual(self.window.model.pending_by_price, {})
            self.assertEqual(self.window.model.filled_by_price, {99: 1})


if __name__ == "__main__":
    unittest.main()
