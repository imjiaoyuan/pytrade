import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from pytrade.app import Controller
from pytrade.domain import Fill, Status
from pytrade.dom_reference_ui import change_step
from pytrade.theme import LEGACY_PALETTE


class ReferenceDomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.controller = Controller(Path(self.directory.name))
        self.controller.timer.stop()
        self.window = self.controller.open_dom("SHFE.ag2612")
        self.snapshot = replace(self.controller.simulator.snapshots[self.window.instrument.key], last=100, bids=((98, 5),), asks=((102, 6),))
        self.controller.simulator.snapshots[self.window.instrument.key] = self.snapshot
        self.window.refresh(self.snapshot)
        self.app.processEvents()

    def tearDown(self):
        self.controller.close()
        self.app.processEvents()
        self.directory.cleanup()

    def test_footer_updates_pnl_and_counts_all_months_without_failed_cancels(self):
        ledger = self.controller.ledger
        key = self.window.instrument.key
        opened = ledger.submit(key, "买", "开仓", 100, 2)
        ledger.acknowledge(opened.id)
        ledger.apply_fill(Fill("footer-open", opened.id, key, "买", "开仓", 100, 2))
        closed = ledger.submit(key, "卖", "平仓", 102, 1)
        ledger.acknowledge(closed.id)
        ledger.apply_fill(Fill("footer-close", closed.id, key, "卖", "平仓", 102, 1))
        second_key = "SHFE.ag2701"
        ledger.catalog[second_key] = replace(self.window.instrument, key=second_key)
        for contract in (key, second_key, "SHFE.au2612"):
            order = ledger.submit(contract, "买", "开仓", 100, 1, group=2)
            ledger.acknowledge(order.id)
            ledger.cancel_report(order.id, True)
        failed = ledger.submit(key, "买", "开仓", 100, 1)
        ledger.acknowledge(failed.id)
        ledger.cancel_report(failed.id, False)
        self.snapshot = replace(self.snapshot, last=103)
        self.window.refresh(self.snapshot)
        self.assertIn("+75.00", self.window.contract_total_pnl.text())
        self.assertIn("+45.00", self.window.contract_floating_pnl.text())
        self.assertIn("1 / 2", self.window.contract_cancels.text())
        last_close = ledger.submit(key, "卖", "平仓", 103, 1)
        ledger.acknowledge(last_close.id)
        ledger.apply_fill(Fill("footer-last", last_close.id, key, "卖", "平仓", 103, 1))
        self.controller.refresh_records()
        self.assertIn("+75.00", self.window.contract_total_pnl.text())
        self.assertIn("0.00", self.window.contract_floating_pnl.text())
        self.controller.offline_mode = False
        self.window.refresh_disconnected()
        self.assertIn("—", self.window.contract_total_pnl.text())
        self.assertIn("— / —", self.window.contract_cancels.text())

    def test_legacy_prices_highlight_last_and_never_fill_missing_depth(self):
        model = self.window.model
        self.assertEqual(model.price_color(100), LEGACY_PALETTE["last_price"])
        for price in (90, 98, 99, 101, 102, 103, 110):
            self.assertEqual(model.price_color(price), LEGACY_PALETTE["price_gap"])
        row = model.rows // 2 + (model.center - 103)
        self.assertEqual(model.data(model.index(row, 2), Qt.ItemDataRole.DisplayRole), "")
        model.snapshot = replace(self.snapshot, asks=())
        self.assertEqual(model.price_color(102), LEGACY_PALETTE["price_gap"])
        model.snapshot = None
        self.assertEqual(model.price_color(98), LEGACY_PALETTE["price_gap"])

    def click_price(self, price, right=False):
        model = self.window.model
        row = model.rows // 2 + (model.center - price) // model.price_step
        self.window.price_action(model.index(row, 1), right=right)

    def test_region_orders_sell_red_buy_blue_skip_gap_and_respect_right_quantity(self):
        self.window.click_mode.setCurrentIndex(self.window.click_mode.findData("regions_open"))
        self.click_price(102)
        self.click_price(98)
        orders = list(self.controller.ledger.orders.values())
        self.assertEqual([(order.side, order.offset, order.ticks) for order in orders], [("卖", "开仓", 102), ("买", "开仓", 98)])
        self.click_price(100)
        self.click_price(102, right=True)
        self.assertEqual(len(self.controller.ledger.orders), 2)
        self.window.right_quantity.setValue(2)
        self.click_price(102, right=True)
        self.assertEqual(list(self.controller.ledger.orders.values())[-1].quantity, 2)

    def test_top_step_buttons_change_display_only_and_floor_at_one_tick(self):
        self.window.price.setValue(100)
        change_step(self.window, 1)
        self.assertEqual(self.window.model.price_step, 2)
        self.assertEqual(self.window.price.value(), 100)
        self.assertEqual(self.window.zoom_percent, 100)
        change_step(self.window, -1)
        change_step(self.window, -1)
        self.assertEqual(self.window.model.price_step, 1)

    def test_reference_controls_share_cancel_policy_and_unknown_position_stays_unknown(self):
        self.window.cancel_policy.setCurrentIndex(self.window.cancel_policy.findData(485))
        self.assertEqual(self.controller.ledger.cancel_caps[self.window.instrument.key], 485)
        self.window.cancel_policy.setCurrentIndex(self.window.cancel_policy.findData(None))
        self.assertNotIn(self.window.instrument.key, self.controller.ledger.cancel_caps)
        self.window.local_controls["only_open"].setChecked(True)
        self.assertEqual(self.window.original_marker.text(), "O")
        self.window.refresh_disconnected()
        self.assertEqual(self.window.net_position_display.text(), "净持仓 —")
        self.assertIn("总持仓 —", self.window.market_stats.text())
        self.assertFalse(self.window.cancel_policy.isEnabled())
        self.assertFalse(self.window.spread_checkbox.isEnabled())
        self.assertTrue(all(not button.isEnabled() for button in self.window.original_modes))

    def advance_algorithm(self, **values):
        self.snapshot = replace(self.snapshot, **values)
        self.controller.simulator.snapshots[self.snapshot.key] = self.snapshot
        self.controller.local_algos.update(self.snapshot)

    def test_a_routes_to_best_limit_and_b_to_sniper_without_immediate_fill(self):
        window = self.window
        window.price.setValue(100)
        window.original_modes[0].click()
        window.submit("买", "开仓")
        first = next(iter(self.controller.local_algos.tasks.values()))
        self.assertEqual(first.kind, "best")
        self.assertFalse(self.controller.ledger.orders)
        self.advance_algorithm()
        self.assertEqual(self.controller.ledger.orders[first.orders[0]].ticks, 98)
        window.original_modes[1].click()
        window.price.setValue(102)
        window.submit("买", "开仓")
        second = list(self.controller.local_algos.tasks.values())[-1]
        self.assertEqual(second.kind, "sniper")
        self.advance_algorithm()
        self.assertEqual(self.controller.ledger.orders[second.orders[0]].ticks, 102)
        self.assertFalse(self.controller.ledger.fills)

    def test_conditional_dom_order_waits_then_triggers_and_honors_changed_locks(self):
        window = self.window
        window.conditional_checkbox.setChecked(True)
        window.price.setValue(105)
        window.submit("买", "开仓")
        task = next(iter(self.controller.local_algos.tasks.values()))
        self.advance_algorithm(last=104)
        self.assertFalse(task.orders)
        window.local_controls["white_lock"].setChecked(True)
        self.advance_algorithm(last=105)
        self.assertEqual(len(task.orders), 1)
        window.algo_binding.stop()
        self.assertEqual(task.status, "已停止")
        window.original_modes[0].click()
        window.price.setValue(110)
        window.submit("买", "开仓")
        blocked = list(self.controller.local_algos.tasks.values())[-1]
        window.local_controls["limit_lock"].setChecked(True)
        self.advance_algorithm()
        self.assertEqual(blocked.status, "已暂停")
        self.assertFalse(blocked.orders)

    def test_dom_spread_parameters_are_used_and_invalid_factor_never_starts(self):
        window = self.window
        second_key = "SHFE.au2612"
        self.controller.open_dom(second_key)
        second_quote = replace(self.snapshot, key=second_key)
        self.controller.simulator.snapshots[second_key] = second_quote
        window.spread_checkbox.setChecked(True)
        second, point, factor = window.spread_fields
        second.setText(second_key)
        point.setText("1000")
        factor.setText("2")
        window.submit("买", "开仓")
        task = next(iter(self.controller.local_algos.tasks.values()))
        self.assertEqual((task.kind, task.second_key, task.factor, task.limit), ("spread", second_key, 2, 1000))
        self.advance_algorithm()
        self.assertEqual(len(task.orders), 1)
        self.assertFalse(task.hedge_orders)
        factor.setText("NaN")
        with patch("pytrade.dom_ui.QMessageBox.warning") as warning:
            window.submit("买", "开仓")
        warning.assert_called_once()
        self.assertEqual(len(self.controller.local_algos.tasks), 1)

    def test_algo_selection_never_bypasses_only_open_or_external_mode(self):
        window = self.window
        window.original_modes[0].click()
        window.local_controls["only_open"].setChecked(True)
        with patch("pytrade.dom_ui.QMessageBox.warning") as warning:
            window.submit("买", "平仓")
        warning.assert_called_once()
        self.assertFalse(self.controller.local_algos.tasks)
        self.controller.offline_mode = False
        window.refresh_controls(False)
        with patch("pytrade.dom_ui.QMessageBox.warning") as warning:
            window.submit("买", "开仓")
        warning.assert_called_once()
        self.assertFalse(self.controller.local_algos.tasks)

    def fill_order(self, identifier):
        ledger = self.controller.ledger
        order = ledger.orders[identifier]
        ledger.acknowledge(identifier)
        ledger.apply_fill(Fill(f"f{len(ledger.fills)}", identifier, order.key, order.side, order.offset, order.ticks, order.remaining))

    def test_spread_close_checks_both_legs_before_start_and_hedges_actual_fills(self):
        window = self.window
        ledger = self.controller.ledger
        second_key = "SHFE.au2612"
        self.controller.open_dom(second_key)
        second_quote = replace(self.snapshot, key=second_key)
        self.controller.simulator.snapshots[second_key] = second_quote
        ledger.marks.update({window.instrument.key: 100, second_key: 100})
        self.fill_order(ledger.submit(window.instrument.key, "卖", "开仓", 100, 1).id)
        window.spread_checkbox.setChecked(True)
        window.spread_fields[0].setText(second_key)
        window.spread_fields[1].setText("1000")
        with patch("pytrade.dom_ui.QMessageBox.warning") as warning:
            window.submit("买", "平仓")
        warning.assert_called_once()
        self.assertFalse(self.controller.local_algos.tasks)
        self.fill_order(ledger.submit(second_key, "买", "开仓", 100, 1).id)
        window.submit("买", "平仓")
        task = next(iter(self.controller.local_algos.tasks.values()))
        self.advance_algorithm()
        self.fill_order(task.orders[0])
        self.controller.local_algos.update(replace(second_quote))
        hedge = ledger.orders[task.hedge_orders[0]]
        self.assertEqual((hedge.side, hedge.offset), ("卖", "平仓"))
        self.fill_order(hedge.id)
        self.advance_algorithm()
        self.assertEqual(task.status, "已完成")
        self.assertEqual(ledger.positions[(window.instrument.key, "卖")].quantity, 0)
        self.assertEqual(ledger.positions[(second_key, "买")].quantity, 0)

    def test_dom_stop_reports_cancel_limit_failure_without_claiming_order_canceled(self):
        window = self.window
        window.price.setValue(100)
        window.original_modes[0].click()
        window.submit("买", "开仓")
        self.advance_algorithm()
        task = next(iter(self.controller.local_algos.tasks.values()))
        self.controller.ledger.cancel_caps[window.instrument.key] = 0
        with patch("pytrade.dom_algo_binding.QMessageBox.warning") as warning:
            window.algo_binding.stop()
        warning.assert_called_once()
        self.assertEqual(task.status, "停止失败")
        self.assertEqual(self.controller.ledger.orders[task.orders[0]].status, Status.PENDING)


if __name__ == "__main__":
    unittest.main()
