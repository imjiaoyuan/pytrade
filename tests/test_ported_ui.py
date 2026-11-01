import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from pytrade.app import Controller
from pytrade.domain import Fill, Status
from pytrade.flow import ApplicationFlow
from pytrade.startup import StartupWindow
from pytrade.connection_ui import ConnectionDialog
from pytrade.external import ConnectionConfig


class PortedUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.controller = Controller(Path(self.directory.name))
        self.controller.timer.stop()
        self.window = self.controller.open_dom("SHFE.ag2612")

    def tearDown(self):
        self.controller.close()
        for _ in range(3):
            self.app.processEvents()
        self.directory.cleanup()

    def test_clear_connection_config_preserves_records_and_removes_trade_credentials(self):
        state = Path(self.directory.name)
        startup = StartupWindow(state)
        startup.front.setText("tcp://127.0.0.1:40011")
        startup.mode.setCurrentIndex(startup.mode.findData("simulation"))
        startup.settings_config = ConnectionConfig(md_front=startup.front.text(), td_front="tcp://127.0.0.1:40001", broker="test", user="synthetic", password="synthetic-password")
        startup.password.setText("synthetic-password")
        startup.accounts.setRowCount(1)
        startup.trade_fronts.setRowCount(1)
        (state / "connection.json").write_text('{"td_front":"tcp://127.0.0.1:40001"}')
        for name in ("risk.json", "accounts.json", "layout.json"):
            (state / name).write_text('{"preserve":true}')
        startup.clear_connection_config()
        self.assertFalse((state / "connection.json").exists())
        self.assertIsNone(startup.settings_config)
        self.assertEqual(startup.password.text(), "")
        self.assertEqual(startup.accounts.rowCount(), 0)
        self.assertEqual(startup.trade_fronts.rowCount(), 0)
        self.assertEqual(startup.mode.currentData(), "paper")
        self.assertEqual(startup.front.text(), "tcp://127.0.0.1:40011")
        for name in ("risk.json", "accounts.json", "layout.json"):
            self.assertEqual((state / name).read_text(), '{"preserve":true}')
        requested = []
        startup.login_requested.connect(requested.append)
        startup.login()
        self.assertTrue(requested[0].paper_trading)
        self.assertEqual((requested[0].td_front, requested[0].user, requested[0].password), ("", "", ""))
        startup.deleteLater()

    def test_failed_clear_preserves_current_config_and_busy_blocks_clear(self):
        state = Path(self.directory.name)
        startup = StartupWindow(state)
        config = ConnectionConfig(md_front="tcp://127.0.0.1:40011")
        startup.settings_config = config
        startup.set_busy(True)
        with patch.object(Path, "unlink") as unlink:
            startup.clear_connection_config()
            unlink.assert_not_called()
        self.assertFalse(startup.clear_config_button.isEnabled())
        startup.set_busy(False)
        with patch.object(Path, "unlink", side_effect=PermissionError("test")), patch("pytrade.startup.QMessageBox.warning") as warning:
            startup.clear_connection_config()
        warning.assert_called_once()
        self.assertIs(startup.settings_config, config)
        startup.deleteLater()

    def test_online_simulation_escape_removes_partial_auth_and_never_creates_trade_connection(self):
        state = Path(self.directory.name)
        startup = StartupWindow(state)
        startup.mode.setCurrentIndex(startup.mode.findData("simulation"))
        startup.front.setText("tcp://127.0.0.1:40011")
        startup.settings_config = ConnectionConfig(md_front=startup.front.text(), td_front="tcp://127.0.0.1:40001", broker="test", user="synthetic", password="synthetic-password", app_id="partial")
        def edit(dialog):
            dialog.use_online_simulation()
            self.assertTrue(dialog.fields["password"].isHidden())
            dialog.md_anonymous.setChecked(False)
            self.assertFalse(dialog.fields["password"].isHidden())
            dialog.md_anonymous.setChecked(True)
            dialog.accept_config()
            return dialog.result()
        with patch.object(ConnectionDialog, "exec", edit), patch("pytrade.connection_ui.QMessageBox.warning") as warning:
            self.assertTrue(startup.edit_account())
        warning.assert_not_called()
        config = startup.settings_config
        self.assertEqual(startup.mode.currentData(), "paper")
        self.assertTrue(config.paper_trading)
        self.assertTrue(config.md_anonymous)
        self.assertFalse(config.allow_orders)
        self.assertEqual((config.td_front, config.broker, config.user, config.password, config.app_id, config.auth_code), ("", "", "", "", "", ""))
        self.assertNotIn("synthetic", (state / "connection.json").read_text())
        startup.deleteLater()

    def test_net_mode_splits_group_position_and_only_open_overrides_it(self):
        ledger = self.controller.ledger
        key = self.window.instrument.key
        order = ledger.submit(key, "卖", "开仓", self.window.model.snapshot.last, 2)
        ledger.acknowledge(order.id)
        ledger.apply_fill(Fill("f1", order.id, key, "卖", "开仓", order.ticks, 2))
        self.window.net_mode.setChecked(True)
        self.window.quantity.setValue(3)
        self.window.submit("买", "开仓")
        new = list(ledger.orders.values())[1:]
        self.assertEqual([(order.offset, order.quantity) for order in new], [("平仓", 2), ("开仓", 1)])
        self.window.local_controls["only_open"].setChecked(True)
        self.window.submit("买", "开仓")
        self.assertEqual(list(ledger.orders.values())[-1].quantity, 3)
        self.assertEqual(list(ledger.orders.values())[-1].offset, "开仓")

    def test_net_batch_failure_does_not_leave_unsent_close_leg(self):
        ledger = self.controller.ledger
        key = self.window.instrument.key
        order = ledger.submit(key, "卖", "开仓", self.window.model.snapshot.last, 1)
        ledger.acknowledge(order.id)
        ledger.apply_fill(Fill("f1", order.id, key, "卖", "开仓", order.ticks, 1))
        self.window.net_mode.setChecked(True)
        self.window.quantity.setValue(3)
        ledger.max_position = 1
        with patch("pytrade.dom_ui.QMessageBox.warning") as warning:
            self.window.submit("买", "开仓")
        warning.assert_called_once()
        self.assertEqual(len(ledger.orders), 1)

    def test_fok_checkbox_and_dropdown_sync_without_turning_fak_into_gfd(self):
        window = self.window
        window.fok_checkbox.setChecked(True)
        self.assertEqual(window.order_type.currentData(), "FOK")
        window.order_type.setCurrentIndex(window.order_type.findData("FAK"))
        self.assertFalse(window.fok_checkbox.isChecked())
        self.assertEqual(window.order_type.currentData(), "FAK")
        window.submit("买", "开仓")
        self.assertEqual(list(self.controller.ledger.orders.values())[-1].order_type, "FAK")
        window.refresh_controls(False)
        self.assertEqual(window.order_type.currentData(), "GFD")
        self.assertFalse(window.order_type.isEnabled())

    def test_strategy_dialog_can_start_and_stop_local_strategy(self):
        self.controller.show_strategies(self.window.instrument.key)
        dialog = self.controller.strategies_dialog
        dialog.quantity.setValue(2)
        dialog.start_task()
        self.controller.advance()
        dialog.refresh()
        self.assertEqual(dialog.table.rowCount(), 1)
        task = next(iter(self.controller.local_algos.tasks.values()))
        self.assertEqual(len(task.orders), 1)
        dialog.table.selectRow(0)
        dialog.stop_task()
        self.assertEqual(task.status, "已停止")
        self.assertEqual(self.controller.ledger.orders[task.orders[0]].status, Status.CANCELED)

    def test_tcp_probe_runs_in_background_and_keeps_selected_service(self):
        startup = StartupWindow(Path(self.directory.name))
        startup.mode.setCurrentIndex(startup.mode.findData("paper"))
        startup.services.selectRow(3)
        selected = startup.front.text()
        result = dict(state="refused", message="test refused", errno=111, elapsed_ms=1.2)
        with patch("pytrade.transport.probe_market_transport", return_value=result) as probe:
            startup.probe_services()
            deadline = time.monotonic() + 3
            while startup.probes_pending and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
            self.assertEqual(startup.probes_pending, 0)
            self.assertEqual(probe.call_count, startup.services.rowCount())
            self.assertEqual(startup.front.text(), selected)
            self.assertIn("连接拒绝", startup.services.item(3, 2).text())
        startup.deleteLater()

    def test_tcp_timeout_advisory_does_not_trigger_login_cancellation(self):
        flow = ApplicationFlow.__new__(ApplicationFlow)
        flow.connecting = True
        flow.controller = SimpleNamespace(external=SimpleNamespace(md_status="连接中：TCP 预检未通过：TCP 连接超时；仍由 CTP 尝试连接", td_status="不连接"), gateway=SimpleNamespace(closing=False))
        flow.startup = MagicMock()
        flow.cancel_login = MagicMock()
        flow.poll_status()
        flow.cancel_login.assert_not_called()
        flow.controller.external.md_status = "连接/登录超时，等待重连或手动断开"
        flow.poll_status()
        flow.cancel_login.assert_called_once()

    def test_simnow_preset_fills_public_parameters_and_clears_old_credentials(self):
        dialog = ConnectionDialog(Path(self.directory.name))
        dialog.fields["user"].setText("old-account")
        dialog.fields["password"].setText("old-password")
        dialog.allow_orders.setChecked(True)
        dialog.preset.setCurrentIndex(dialog.preset.findData("7x24"))
        dialog.apply_preset()
        self.assertEqual(dialog.fields["md_front"].text(), "tcp://182.254.243.31:40011")
        self.assertEqual(dialog.fields["td_front"].text(), "tcp://182.254.243.31:40001")
        self.assertEqual(dialog.fields["broker"].text(), "9999")
        self.assertEqual(dialog.fields["app_id"].text(), "simnow_client_test")
        self.assertEqual(dialog.fields["auth_code"].text(), "0" * 16)
        self.assertEqual(dialog.fields["user"].text(), "")
        self.assertEqual(dialog.fields["password"].text(), "")
        self.assertFalse(dialog.allow_orders.isChecked())
        self.assertFalse((Path(self.directory.name) / "connection.json").exists())
        dialog.deleteLater()

    def test_simnow_market_preset_never_adds_trade_front_or_authentication(self):
        dialog = ConnectionDialog(Path(self.directory.name), market_only=True)
        dialog.preset.setCurrentIndex(dialog.preset.findData("intraday"))
        dialog.apply_preset()
        self.assertEqual(dialog.fields["md_front"].text(), "tcp://182.254.243.31:30011")
        self.assertEqual(dialog.fields["td_front"].text(), "")
        self.assertEqual(dialog.fields["app_id"].text(), "")
        self.assertEqual(dialog.fields["auth_code"].text(), "")
        self.assertTrue(dialog.md_anonymous.isChecked())
        dialog.deleteLater()

    def test_simnow_quick_config_keeps_credentials_local_and_selects_matching_source(self):
        import json
        from PySide6.QtWidgets import QDialog
        startup = StartupWindow(Path(self.directory.name))
        startup.mode.setCurrentIndex(startup.mode.findData("paper"))
        startup.services.selectRow(0)
        def configure(dialog):
            self.assertEqual(dialog.fields["user"].text(), "")
            dialog.fields["user"].setText("synthetic-investor")
            dialog.fields["password"].setText("synthetic-password")
            dialog.accept_config()
            return QDialog.DialogCode.Accepted
        with patch("pytrade.startup.ConnectionDialog.exec", configure):
            self.assertTrue(startup.edit_account(simnow=True))
        self.assertEqual(startup.mode.currentData(), "simulation")
        self.assertEqual(startup.settings_config.user, "synthetic-investor")
        self.assertIn("SimNow", startup.current.text())
        saved = json.loads((Path(self.directory.name) / "connection.json").read_text())
        for field in ("user", "password", "auth_code"):
            self.assertNotIn(field, saved)
        self.assertFalse(startup.settings_config.allow_orders)
        startup.deleteLater()


if __name__ == "__main__":
    unittest.main()
