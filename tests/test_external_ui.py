import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QTimer, Signal, Qt, QPoint
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from pytrade.app import Controller
from pytrade.connection_ui import ConnectionDialog
from pytrade.external import ConnectionConfig


class FakeGateway(QObject):
    event = Signal(dict)
    stopped = Signal()

    def __init__(self, config, state_dir, parent=None):
        super().__init__(parent)
        self.commands = []
        self.closing = False

    def send(self, message):
        self.commands.append(message)

    def close(self):
        self.closing = True
        QTimer.singleShot(0, self.stopped.emit)


class ExternalUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name)
        self.controller = Controller(self.path)
        self.controller.open_dom("SHFE.ag2612")
        self.controller.open_dom("SHFE.au2612")
        self.controller.timer.stop()
        self.gateway_patch = patch("pytrade.app.CtpGateway", FakeGateway)
        self.gateway_patch.start()

    def tearDown(self):
        self.controller.close()
        for _ in range(5):
            self.app.processEvents()
        self.gateway_patch.stop()
        self.directory.cleanup()

    def test_real_mode_never_advances_offline_generator(self):
        sequence = self.controller.simulator.sequence
        self.controller.start_external(ConnectionConfig(md_front="tcp://127.0.0.1:1"))
        self.controller.advance()
        self.assertEqual(self.controller.simulator.sequence, sequence)
        self.assertEqual(self.controller.simulator.snapshots, {})
        self.assertIsNone(self.controller.windows[0].model.snapshot)
        self.assertIn("等待有效真实行情", self.controller.windows[0].banner.text())

    def start_paper(self):
        self.controller.start_external(ConnectionConfig(md_front="tcp://127.0.0.1:1", paper_trading=True))
        self.controller.gateway.event.emit(dict(kind="md_state", epoch=1, status="登录成功"))
        for key in ("SHFE.ag2612", "SHFE.au2612"):
            self.controller.gateway.event.emit(dict(kind="subscription", epoch=1, key=key, error=0))
        self.paper_quote()
        self.controller.advance()

    def paper_quote(self):
        import time
        self.controller.gateway.event.emit(dict(kind="quote", epoch=1, key="SHFE.ag2612", received=time.monotonic(), row=dict(LastPrice=12520, BidPrice1=12519, BidVolume1=3, AskPrice1=12521, AskVolume1=3, Volume=1, TradingDay="20261030", UpdateTime="10:00:00", UpperLimitPrice=20000, LowerLimitPrice=10000)))

    def test_online_paper_buttons_fill_locally_without_generated_quotes_or_trader_commands(self):
        sequence = self.controller.simulator.sequence
        self.start_paper()
        window = self.controller.windows[0]
        self.assertTrue(all(button.isEnabled() for button in window.order_buttons))
        window.price.setValue(12522)
        window.quantity.setValue(2)
        window.submit("买", "开仓")
        order = next(iter(self.controller.ledger.orders.values()))
        self.assertEqual(order.filled, 0)
        self.paper_quote()
        self.assertEqual(order.filled, 0)
        self.paper_quote()
        self.controller.advance()
        self.assertEqual(order.filled, 2)
        self.assertEqual(window.long_position.text(), "多 2")
        self.assertIn("在线模拟", window.windowTitle())
        self.assertIn("真实行情", window.banner.text())
        self.assertEqual(self.controller.simulator.sequence, sequence)
        self.assertFalse(self.controller.simulator.snapshots)
        self.assertTrue(all(command["command"] == "subscriptions" for command in self.controller.gateway.commands))

    def test_market_only_can_switch_to_paper_without_reconnect_or_trader_commands(self):
        self.controller.start_external(ConnectionConfig(md_front="tcp://127.0.0.1:1"))
        gateway = self.controller.gateway
        gateway.event.emit(dict(kind="md_state", epoch=1, status="登录成功"))
        gateway.event.emit(dict(kind="subscription", epoch=1, key="SHFE.ag2612", error=0))
        self.paper_quote()
        self.controller.advance()
        window = self.controller.windows[0]
        self.assertFalse(window.paper_button.isHidden())
        self.assertFalse(window.order_buttons[0].isEnabled())
        window.paper_button.click()
        self.assertIs(self.controller.gateway, gateway)
        self.assertTrue(window.paper_button.isHidden())
        self.assertTrue(window.order_buttons[0].isEnabled())
        window.quantity.setValue(1)
        window.price.setValue(12521)
        window.submit("买", "开仓")
        self.assertEqual(len(self.controller.ledger.orders), 1)
        self.assertFalse(gateway.closing)
        self.assertTrue(all(command["command"] == "subscriptions" for command in gateway.commands))

    def test_switch_to_paper_keeps_stale_quotes_untradable(self):
        from dataclasses import replace
        self.controller.start_external(ConnectionConfig(md_front="tcp://127.0.0.1:1"))
        gateway = self.controller.gateway
        gateway.event.emit(dict(kind="md_state", epoch=1, status="登录成功"))
        gateway.event.emit(dict(kind="subscription", epoch=1, key="SHFE.ag2612", error=0))
        self.paper_quote()
        key = "SHFE.ag2612"
        self.controller.external.snapshots[key] = replace(self.controller.external.snapshots[key], received_monotonic=1)
        self.controller.enable_online_paper()
        self.assertFalse(self.controller.windows[0].order_buttons[0].isEnabled())
        with self.assertRaisesRegex(ValueError, "过期"):
            self.controller.paper.submit(key, "买", "开仓", 12521, 1)
        self.assertFalse(self.controller.ledger.orders)

    def test_local_ladder_click_rules_require_explicit_selection_and_respect_market_boundary(self):
        window = self.controller.windows[0]
        index = window.model.index(window.model.rows // 2, 1)
        window.price_action(index)
        self.assertFalse(self.controller.ledger.orders)
        window.click_mode.setCurrentIndex(window.click_mode.findData("mouse_open"))
        with patch.object(window, "submit") as submit:
            position = window.table.visualRect(index).center()
            QTest.mouseClick(window.table.viewport(), Qt.MouseButton.LeftButton, pos=position)
            submit.assert_called_once_with("买", "开仓")
            submit.reset_mock()
            window.table.customContextMenuRequested.emit(position)
            submit.assert_called_once_with("卖", "开仓")
        window.click_mode.setCurrentIndex(window.click_mode.findData("columns_close"))
        with patch.object(window, "submit") as submit:
            window.price_action(index)
            submit.assert_not_called()
            window.price_action(window.model.index(index.row(), 0))
            submit.assert_called_once_with("买", "平仓")
        self.controller.start_external(ConnectionConfig(md_front="tcp://127.0.0.1:1"))
        with patch.object(window, "submit") as submit:
            window.price_action(window.model.index(index.row(), 0))
            submit.assert_not_called()

    def test_paper_ladder_click_cannot_use_stale_market(self):
        from dataclasses import replace
        self.start_paper()
        window = self.controller.windows[0]
        key = window.instrument.key
        self.controller.external.snapshots[key] = replace(self.controller.external.snapshots[key], received_monotonic=1)
        self.controller.advance()
        window.click_mode.setCurrentIndex(window.click_mode.findData("mouse_open"))
        window.price_action(window.model.index(window.model.rows // 2, 1))
        self.assertFalse(self.controller.ledger.orders)

    def test_space_cancels_only_current_contract_and_group_in_paper_mode(self):
        from pytrade.domain import Status
        self.start_paper()
        window = self.controller.windows[0]
        first = self.controller.paper.submit(window.instrument.key, "买", "开仓", 12500, 1, 1)
        other = self.controller.paper.submit(window.instrument.key, "买", "开仓", 12500, 1, 2)
        window.activateWindow()
        self.app.processEvents()
        QTest.keyClick(window, Qt.Key.Key_Space)
        self.assertEqual(first.status, Status.CANCELED)
        self.assertEqual(other.status, Status.PENDING)
        self.assertTrue(all(command["command"] == "subscriptions" for command in self.controller.gateway.commands))

    def test_function_keys_switch_visible_groups_without_reassigning_windows(self):
        self.controller.show()
        first = self.controller.windows[0]
        second = self.controller.open_dom(first.instrument.key, group=2)
        first.activateWindow()
        self.app.processEvents()
        QTest.keyClick(first, Qt.Key.Key_F2)
        self.assertEqual(self.controller.group.value(), 2)
        self.assertFalse(first.isVisible())
        self.assertTrue(second.isVisible())
        QTest.keyClick(second, Qt.Key.Key_F1)
        self.assertEqual(self.controller.group.value(), 1)
        self.assertTrue(first.isVisible())
        self.assertFalse(second.isVisible())
        self.assertEqual((first.group.value(), second.group.value()), (1, 2))
        self.controller.activateWindow()
        self.app.processEvents()
        for group in range(1, 11):
            QTest.keyClick(self.controller, Qt.Key(int(Qt.Key.Key_F1) + group - 1))
            self.assertEqual(self.controller.group.value(), group)

    def test_paper_disconnect_keeps_local_positions_and_never_replays_pending_orders(self):
        from pytrade.domain import Status
        self.start_paper()
        filled = self.controller.paper.submit("SHFE.ag2612", "买", "开仓", 12522, 1)
        self.paper_quote()
        self.paper_quote()
        pending = self.controller.paper.submit("SHFE.ag2612", "买", "开仓", 12500, 1)
        self.controller.disconnect_ctp()
        for _ in range(5):
            self.app.processEvents()
        self.assertIsNone(self.controller.external)
        self.assertEqual(filled.status, Status.FILLED)
        self.assertEqual(pending.status, Status.CANCELED)
        self.assertEqual(self.controller.ledger.positions[("SHFE.ag2612", "买")].quantity, 1)
        self.start_paper()
        self.assertEqual(self.controller.ledger.positions[("SHFE.ag2612", "买")].quantity, 1)
        self.assertEqual(pending.status, Status.CANCELED)

    def test_online_paper_stop_uses_local_ledger_and_real_quote_updates(self):
        from pytrade.domain import Status
        from pytrade.tools_ui import RiskDialog, StopsDialog
        self.start_paper()
        opening = self.controller.paper.submit("SHFE.ag2612", "买", "开仓", 12522, 1)
        self.paper_quote()
        self.paper_quote()
        self.assertEqual(opening.status, Status.FILLED)
        risk = RiskDialog(self.controller)
        stops = StopsDialog(self.controller)
        self.assertIsNone(risk.session)
        self.assertIs(stops.manager, self.controller.offline_stops.stops)
        self.assertEqual(stops.offset.count(), 1)
        stop = stops.manager.add("SHFE.ag2612", "买", "平仓", 12521, 1, 1)
        stops.manager.arm(stop.id)
        self.controller.advance()
        self.assertTrue(stop.order_id)
        self.assertEqual(self.controller.ledger.orders[stop.order_id].filled, 0)
        self.paper_quote()
        self.paper_quote()
        self.controller.advance()
        self.assertEqual(stop.state, "filled")
        self.assertTrue(all(command["command"] == "subscriptions" for command in self.controller.gateway.commands))
        risk.close()
        stops.close()

    def test_market_event_routes_into_dom_and_disconnect_clears_it(self):
        self.controller.start_external(ConnectionConfig(md_front="tcp://127.0.0.1:1"))
        gateway = self.controller.gateway
        gateway.event.emit(dict(kind="md_state", epoch=1, status="登录成功"))
        gateway.event.emit(dict(kind="subscription", epoch=1, key="SHFE.ag2612", error=0))
        import time
        gateway.event.emit(dict(kind="quote", key="SHFE.ag2612", epoch=1, received=time.monotonic(), row=dict(LastPrice=12520, BidPrice1=12519, BidVolume1=2, AskPrice1=12520, AskVolume1=3, Volume=5, TradingDay="20261030", ActionDay="20261029", UpdateTime="21:00:01")))
        self.controller.advance()
        window = self.controller.windows[0]
        self.assertEqual(window.model.snapshot.last, 12520)
        self.assertIn("真实行情", window.banner.text())
        gateway.event.emit(dict(kind="md_state", epoch=2, status="断线"))
        self.controller.advance()
        self.assertIsNone(window.model.snapshot)

    def test_market_only_marks_positions_unknown_and_disables_order_actions(self):
        self.controller.start_external(ConnectionConfig(md_front="tcp://127.0.0.1:1"))
        self.controller.advance()
        window = self.controller.windows[0]
        self.assertIn("仅看行情", window.windowTitle())
        self.assertEqual(window.long_position.text(), "多 —")
        self.assertIn("未查询持仓", window.position.text())
        self.assertTrue(all(not button.isEnabled() for button in window.order_buttons))
        self.assertFalse(window.cancel_button.isEnabled())
        self.assertNotIn("待对账", self.controller.statusBar().currentMessage())

    def test_failed_unsubscribe_does_not_prevent_close_or_rebuild_contract_table(self):
        self.controller.start_external(ConnectionConfig(md_front="tcp://127.0.0.1:1"))
        window = self.controller.windows[0]
        key = window.instrument.key
        geometry = self.controller.geometry().getRect()
        with patch.object(self.controller.gateway, "send", side_effect=ValueError("queue full")), patch.object(self.controller, "filter_contracts") as rebuild:
            self.assertTrue(window.close())
            self.assertNotIn(window, self.controller.windows)
            self.assertNotIn(key, self.controller.external.references)
            rebuild.assert_not_called()
        self.assertEqual(self.controller.geometry().getRect(), geometry)

    def test_zoom_resizes_text_and_retains_exact_price_selection_and_workspace(self):
        window = self.controller.windows[1]
        geometry = self.controller.geometry().getRect()
        window.set_zoom(150)
        window.resize(600, 800)
        self.app.processEvents()
        self.assertGreater(window.model.row_height, 13)
        self.assertGreater(window.model.font_size, 11)
        self.assertGreater(window.table.columnWidth(1), 54)
        row = 12
        point = QPoint(window.table.columnViewportPosition(1) + 10, window.table.rowViewportPosition(row) + 5)
        expected = window.model.price_at(row)
        QTest.mouseClick(window.table.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(window.instrument.ticks(window.price.cleanText()), expected)
        window.toggle_maximized()
        self.app.processEvents()
        self.assertTrue(window.isMaximized())
        window.toggle_maximized()
        self.app.processEvents()
        self.assertFalse(window.isMaximized())
        self.assertEqual(self.controller.geometry().getRect(), geometry)

    def test_identical_market_snapshot_does_not_repaint_whole_ladder(self):
        import time
        self.controller.start_external(ConnectionConfig(md_front="tcp://127.0.0.1:1"))
        gateway = self.controller.gateway
        gateway.event.emit(dict(kind="md_state", epoch=1, status="登录成功"))
        gateway.event.emit(dict(kind="subscription", epoch=1, key="SHFE.ag2612", error=0))
        gateway.event.emit(dict(kind="quote", epoch=1, key="SHFE.ag2612", received=time.monotonic(), row=dict(LastPrice=12520, BidPrice1=12519, BidVolume1=2, AskPrice1=12520, AskVolume1=3, Volume=5, TradingDay="20261030", UpdateTime="15:00:00")))
        self.controller.advance()
        window = self.controller.windows[0]
        spy = QSignalSpy(window.model.dataChanged)
        for _ in range(10):
            self.controller.advance()
        self.assertEqual(spy.count(), 0)
        object.__setattr__(window.model.snapshot, "received_monotonic", time.monotonic() - 100)
        self.controller.advance()
        self.assertIn("行情过期", window.banner.text())
        self.assertIn("可能休市", window.banner.toolTip())
        self.assertEqual(spy.count(), 0)

    def test_reordered_records_preserve_selected_order_and_removed_order_clears_selection(self):
        table = self.controller.orders
        self.controller.populate(table, [("first", "SHFE.ag2612"), ("second", "SHFE.au2612")])
        table.selectRow(1)
        self.controller.populate(table, [("second", "SHFE.au2612"), ("first", "SHFE.ag2612")])
        self.assertEqual(table.item(table.currentRow(), 0).text(), "second")
        self.controller.populate(table, [("first", "SHFE.ag2612")])
        self.assertEqual(table.currentRow(), -1)

    def test_profile_never_saves_credentials_or_arms_trading(self):
        dialog = ConnectionDialog(self.path, self.controller)
        dialog.fields["md_front"].setText("tcp://127.0.0.1:1")
        dialog.fields["user"].setText("private-user")
        dialog.fields["password"].setText("private-password")
        dialog.fields["app_id"].setText("app")
        dialog.fields["auth_code"].setText("private-auth")
        dialog.accept_config()
        saved = (self.path / "connection.json").read_text()
        for value in ("private-user", "private-password", "private-auth", "allow_orders"):
            self.assertNotIn(value, saved)
        reopened = ConnectionDialog(self.path, self.controller)
        self.assertFalse(reopened.allow_orders.isChecked())
        self.assertEqual(reopened.fields["user"].text(), "")

    def test_market_only_dialog_strips_stale_trading_settings_and_all_credentials_from_profile(self):
        (self.path / "connection.json").write_text('{"td_front":"tcp://127.0.0.1:2","app_id":"old-app"}')
        dialog = ConnectionDialog(self.path, self.controller, market_only=True)
        dialog.md_anonymous.setChecked(False)
        dialog.fields["md_front"].setText("tcp://127.0.0.1:1")
        dialog.fields["broker"].setText("md-broker")
        dialog.fields["user"].setText("md-private-user")
        dialog.fields["password"].setText("md-private-password")
        dialog.accept_config()
        self.assertFalse(dialog.config.td_front)
        self.assertFalse(dialog.config.app_id)
        self.assertFalse(dialog.config.allow_orders)
        self.assertEqual(dialog.config.market_credentials(), ("md-broker", "md-private-user", "md-private-password"))
        saved = (self.path / "connection.json").read_text()
        self.assertNotIn("md-private-user", saved)
        self.assertNotIn("md-private-password", saved)

    def test_independent_market_credentials_never_replace_trade_account_or_persist(self):
        dialog = ConnectionDialog(self.path, self.controller)
        dialog.fields["md_front"].setText("tcp://127.0.0.1:1")
        dialog.fields["td_front"].setText("tcp://127.0.0.1:2")
        for key, value in dict(broker="td-broker", user="td-user", password="td-password", md_broker="md-broker", md_user="md-user", md_password="md-password").items():
            dialog.fields[key].setText(value)
        dialog.md_anonymous.setChecked(False)
        dialog.md_separate.setChecked(True)
        dialog.accept_config()
        self.assertEqual(dialog.config.market_credentials(), ("md-broker", "md-user", "md-password"))
        self.assertEqual((dialog.config.broker, dialog.config.user, dialog.config.password), ("td-broker", "td-user", "td-password"))
        saved = (self.path / "connection.json").read_text()
        for value in ("md-user", "md-password", "td-user", "td-password"):
            self.assertNotIn(value, saved)

    def test_last_window_close_removes_subscription(self):
        self.controller.start_external(ConnectionConfig(md_front="tcp://127.0.0.1:1"))
        window = self.controller.windows[0]
        key = window.instrument.key
        duplicate = self.controller.open_dom(key)
        window.close()
        self.assertEqual(self.controller.external.references[key], 1)
        duplicate.close()
        self.assertNotIn(key, self.controller.external.references)
        self.assertNotIn(key, self.controller.gateway.commands[-1]["keys"])


    def test_ladder_click_selects_exact_tick_after_resize(self):
        window = self.controller.windows[1]
        window.resize(260, 800)
        self.app.processEvents()
        row = 12
        point = QPoint(window.table.columnViewportPosition(1) + 10, window.table.rowViewportPosition(row) + 5)
        expected = window.instrument.price(window.model.price_at(row))
        QTest.mouseClick(window.table.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(window.instrument.ticks(window.price.cleanText()), window.instrument.ticks(expected))

    def test_offline_stop_orders_are_pending_until_simulator_reports_fill(self):
        controller = self.controller
        key = 'SHFE.ag2612'
        snapshot = controller.simulator.snapshots[key]
        opening = controller.ledger.submit(key, '买', '开仓', snapshot.last + 100, 2, group=2)
        for _ in range(3):
            controller.advance()
        self.assertEqual(opening.filled, 2)
        trigger = controller.simulator.snapshots[key].last + 10
        stop = controller.offline_stops.stops.add(key, '买', '平仓', trigger, 2, 2)
        controller.offline_stops.stops.arm(stop.id)
        with self.assertRaises(ValueError):
            controller.ledger.submit(key, '卖', '平仓', trigger, 1, group=2)
        controller.advance()
        closing = controller.ledger.orders[stop.order_id]
        self.assertEqual(closing.filled, 0)
        self.assertEqual(closing.group, 2)
        self.assertEqual(closing.stop_id, stop.id)
        for _ in range(4):
            controller.simulator.prices[key] = closing.ticks + 20
            controller.advance()
        self.assertEqual(closing.filled, 2)
        self.assertEqual(stop.state, 'filled')


    def test_explicit_layout_restore_keeps_group_center_and_reuses_windows(self):
        controller = self.controller
        window = controller.windows[0]
        window.follow.setChecked(False)
        window.model.center = 15100
        window.set_zoom(150)
        controller.group.setValue(4)
        controller.save_layout()
        controller.group.setValue(1)
        window.model.center = 14900
        window.set_zoom(100)
        controller.restore_layout()
        self.assertEqual(controller.group.value(), 4)
        self.assertEqual(window.model.center, 15100)
        self.assertEqual(window.zoom_percent, 150)
        self.assertFalse(window.model.follow)
        self.assertEqual(len(controller.windows), 2)


    def test_disconnect_preserves_real_contract_windows_without_generating_quotes(self):
        self.controller.start_external(ConnectionConfig(md_front="tcp://127.0.0.1:1"))
        keys = [window.instrument.key for window in self.controller.windows]
        sequence = self.controller.simulator.sequence
        self.controller.disconnect_ctp()
        for _ in range(5):
            self.app.processEvents()
        self.assertEqual([window.instrument.key for window in self.controller.windows], keys)
        self.assertFalse(self.controller.offline_mode)
        self.controller.advance()
        self.assertEqual(self.controller.simulator.sequence, sequence)
        self.assertTrue(all(window.model.snapshot is None for window in self.controller.windows))
        self.controller.enter_offline()
        self.assertTrue(self.controller.offline_mode)
        self.assertTrue(all(window.model.snapshot is not None for window in self.controller.windows))


if __name__ == "__main__":
    unittest.main()
