import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from pytrade.app import Controller
from pytrade.configuration import AccountStore, copy_instrument_table
from pytrade.sounds import SoundTracker


class FeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name)
        self.controller = Controller(self.path)
        self.controller.open_dom("SHFE.ag2612")
        self.controller.timer.stop()

    def tearDown(self):
        self.controller.close()
        for _ in range(3):
            self.app.processEvents()
        self.directory.cleanup()

    def test_legacy_dom_uses_legacy_palette_and_marks(self):
        from pytrade.theme import LEGACY_PALETTE
        window = self.controller.windows[0]
        self.assertGreaterEqual(window.sidebar.width(), 130)
        self.assertIn(LEGACY_PALETTE["topbar"], window.topbar.styleSheet())
        snapshot = self.controller.simulator.snapshots[window.instrument.key]
        window.refresh(snapshot)
        order = self.controller.ledger.submit(window.instrument.key, "买", "开仓", snapshot.last + 20, 2)
        self.advance_until_filled(order.id)
        self.assertIn(order.ticks, window.model.filled_by_price)
        pending = self.controller.ledger.submit(window.instrument.key, "买", "开仓", snapshot.last - 200, 3)
        self.controller.advance()
        self.controller.advance()
        window.refresh_marks()
        self.assertIn(pending.ticks, window.model.pending_by_price)
        window.marks_toggle.setChecked(False)
        self.assertEqual(window.model.filled_by_price, {})
        window.marks_toggle.setChecked(True)
        self.assertIn(pending.ticks, window.model.pending_by_price)

    def advance_until_filled(self, order_id, rounds=40):
        from pytrade.domain import Status
        order = self.controller.ledger.orders[order_id]
        for _ in range(rounds):
            if order.status == Status.FILLED:
                return
            self.controller.advance()
        self.fail("委托未成交")

    def test_position_window_shows_offline_ledger_and_group_bar(self):
        self.controller.show_positions_window()
        position_window = self.controller.position_window
        order = self.controller.ledger.submit("SHFE.ag2612", "买", "开仓", self.controller.simulator.snapshots["SHFE.ag2612"].last + 10, 2)
        self.advance_until_filled(order.id)
        position_window.refresh()
        texts = [position_window.table.item(row, 0).text() for row in range(position_window.table.rowCount())]
        self.assertIn("SHFE.ag2612", texts)
        self.assertIn("离线模拟", position_window.title.text())
        self.controller.show_group_bar(True)
        bar = self.controller.group_bar
        self.assertTrue(bar.isVisible())
        self.assertEqual(len(bar.buttons), 20)
        bar.adjust_buttons()
        self.assertTrue(bar.buttons[0].isChecked())
        self.assertIn("1·1", bar.buttons[0].text())

    def test_close_record_window_lists_closed_trades_and_profit(self):
        from pytrade.domain import Status
        self.controller.show_close_records_window()
        window = self.controller.close_record_window
        opening = self.controller.ledger.submit("SHFE.ag2612", "买", "开仓", self.controller.simulator.snapshots["SHFE.ag2612"].last + 10, 2)
        self.advance_until_filled(opening.id)
        closing = self.controller.ledger.submit("SHFE.ag2612", "卖", "平仓", self.controller.simulator.snapshots["SHFE.ag2612"].last - 10, 2)
        self.advance_until_filled(closing.id)
        window.refresh()
        self.assertEqual(window.table.rowCount(), 1)
        self.assertEqual(window.table.item(0, 0).text(), "SHFE.ag2612")
        self.assertIn("平仓", window.table.item(0, 2).text())
        self.assertIn("合计盈亏", window.title.text())

    def test_close_group_and_cancel_group_targets_only_that_group(self):
        from pytrade.domain import Status
        from pytrade.contract_selector import ContractSelector
        first = self.controller.windows[0]
        second = self.controller.open_dom("SHFE.au2612", group=2)
        pending_group_one = self.controller.ledger.submit("SHFE.ag2612", "买", "开仓", 1, 1, group=1)
        pending_group_two = self.controller.ledger.submit("SHFE.au2612", "买", "开仓", 1, 1, group=2)
        self.controller.advance()
        self.controller.advance()
        self.controller.group.setValue(2)
        self.controller.cancel_group_orders()
        self.assertEqual(self.controller.ledger.orders[pending_group_one.id].status, Status.WORKING)
        self.assertEqual(self.controller.ledger.orders[pending_group_two.id].status, Status.CANCELED)
        self.controller.close_group_doms()
        self.assertNotIn(second, self.controller.windows)
        self.assertIn(first, self.controller.windows)
        self.controller.close_all_doms()
        self.assertEqual(self.controller.windows, [])

    def test_contract_selector_sorts_by_volume_and_filters_options(self):
        from pytrade.contract_selector import ContractSelector
        selector = ContractSelector(self.controller)
        selector.refresh()
        self.assertEqual(selector.table.columnCount(), 7)
        self.assertEqual(selector.category.count(), 4)
        rows = [selector.table.item(row, 1).text() for row in range(selector.table.rowCount())]
        self.assertIn("SHFE.ag2612", rows)
        self.assertIn("SHFE.au2612", rows)
        first_volume = selector.table.item(0, 3).text()
        selector.sort_requested(3)
        top = selector.table.item(0, 3)
        self.assertGreaterEqual(int(top.text()), int(first_volume) if first_volume != "—" else 0)
        selector.category.setCurrentText("期权")
        selector.refresh()
        self.assertEqual(selector.table.rowCount(), 0)

    def test_account_store_rejects_credentials_and_copies_instrument_table(self):
        store = AccountStore(self.path / "accounts.json")
        store.save_profile("模拟A", dict(name="模拟A", md_front="tcp://127.0.0.1:1", broker="9999"))
        with self.assertRaises(ValueError):
            store.save_profile("带账号", dict(name="带账号", user="0001"))
        with self.assertRaises(ValueError):
            store.save_profile("坏档案", dict(name="坏档案", password="secret"))
        with self.assertRaises(ValueError):
            store.save_profile("坏档案2", dict(name="坏档案2", auth_code="code"))
        self.assertNotIn("user", store.read()["模拟A"])
        store.save_profile("模拟B", dict(name="模拟B", md_front="tcp://127.0.0.2:1"))
        with self.assertRaises(ValueError):
            copy_instrument_table(store, "模拟A", "模拟B")
        store.save_instruments("模拟A", {"SHFE.ag2612": dict(tick="1", multiplier=15)})
        self.assertEqual(copy_instrument_table(store, "模拟A", "模拟B"), 1)
        self.assertEqual(store.instruments("模拟B")["SHFE.ag2612"]["multiplier"], 15)
        saved = store.read()
        self.assertNotIn("password", saved["模拟A"])
        self.assertNotIn("auth_code", saved["模拟A"])
        store.delete("模拟B")
        store.save_profile("模拟A2", dict(name="模拟A2", md_front="tcp://127.0.0.3:1"), previous="模拟A")
        self.assertEqual(list(store.read()), ["模拟A2"])
        store.note_used("模拟A2")
        self.assertNotEqual(store.read()["模拟A2"]["used_at"], "未使用")

    def test_nomoney_sound_triggers_only_on_fund_rejection(self):
        tracker = SoundTracker()
        source = object()
        tracker.observe(source, {}, set())
        self.assertEqual(tracker.observe(source, {"a": "资金不足"}, set()), ["nomoney"])
        self.assertEqual(tracker.observe(source, {"a": "资金不足"}, set()), [])
        self.assertEqual(tracker.observe(source, {"b": "已受理"}, set()), ["accepted"])

    def test_dom_window_upgrades_geometry_from_saved_layout(self):
        state = {"schema_version": 1, "controller": [10, 20, 900, 500], "controller_group": 1, "details_visible": True, "windows": [{"key": "SHFE.ag2612", "group": 3, "geometry": [40, 60, 240, 700], "follow": True, "center": 15000, "zoom": 100}]}
        (self.path / "layout.json").write_text(__import__("json").dumps(state), encoding="utf-8")
        restored = Controller(self.path, restore_windows=True)
        restored.timer.stop()
        try:
            self.assertEqual(len(restored.windows), 1)
            self.assertEqual(restored.windows[0].group.value(), 3)
            self.assertGreaterEqual(restored.windows[0].width(), 280)
        finally:
            restored.close()
            for _ in range(3):
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
