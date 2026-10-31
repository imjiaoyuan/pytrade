import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer, Qt, Signal
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

from pytrade.external import ConnectionConfig
from pytrade.flow import ApplicationFlow


class FakeGateway(QObject):
    event = Signal(dict)
    stopped = Signal()

    def __init__(self, config, state_dir, parent=None):
        super().__init__(parent)
        self.closing = False
        self.commands = []

    def send(self, value):
        self.commands.append(value)

    def close(self):
        self.closing = True
        QTimer.singleShot(0, self.stopped.emit)


class StartupFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name)
        self.patch = patch("pytrade.app.CtpGateway", FakeGateway)
        self.patch.start()
        self.flow = ApplicationFlow(self.path)
        self.flow.show()

    def tearDown(self):
        self.flow.quit()
        for _ in range(5):
            self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.patch.stop()
        self.directory.cleanup()

    def test_launch_and_service_selection_never_open_dom_or_connect(self):
        self.assertIsNone(self.flow.controller)
        self.assertTrue(self.flow.startup.isVisible())
        self.flow.startup.services.selectRow(0)
        self.assertIsNone(self.flow.controller)
        self.assertTrue(self.flow.startup.front.text().startswith("tcp://"))
        self.assertEqual(self.flow.startup.services.item(0, 2).text(), "未测")

    def test_offline_entry_requires_confirmed_contracts_even_with_saved_layout(self):
        state = dict(schema_version=1, controller=[0, 0, 740, 380], windows=[dict(key="SHFE.ag2612", group=1, geometry=[10, 10, 368, 740])])
        (self.path / "layout.json").write_text(json.dumps(state))
        self.flow.startup.login_button.click()
        controller = self.flow.controller
        self.assertTrue(controller.isVisible())
        self.assertTrue(controller.contract_selector.isVisible())
        self.assertFalse(self.flow.startup.isVisible())
        self.assertEqual(controller.windows, [])
        chooser = controller
        chooser.contracts.item(0, 0).setCheckState(Qt.CheckState.Checked)
        key = chooser.contracts.item(0, 1).text()
        self.assertEqual(controller.windows, [])
        chooser.search.setText("au")
        self.assertIn(key, chooser.checked_contracts)
        chooser.open_selected()
        self.assertEqual([window.instrument.key for window in controller.windows], [key])

    def test_market_login_waits_for_actual_login_callback(self):
        startup = self.flow.startup
        startup.mode.setCurrentIndex(startup.mode.findData("market"))
        startup.front.setText("tcp://127.0.0.1:10001")
        startup.login_button.click()
        controller = self.flow.controller
        self.assertTrue(startup.busy)
        self.assertFalse(controller.isVisible())
        self.assertEqual(controller.windows, [])
        controller.gateway.event.emit(dict(kind="md_state", epoch=1, status="已连接，登录中"))
        self.assertIsNone(controller.contract_selector)
        self.assertTrue(startup.isVisible())
        controller.gateway.event.emit(dict(kind="md_state", epoch=1, status="登录成功，等待订阅行情"))
        self.assertFalse(startup.isVisible())
        self.assertTrue(controller.details_visible)
        self.assertTrue(controller.contracts.isVisible())
        self.assertTrue(controller.contract_selector.isVisible())
        self.assertEqual(controller.windows, [])

    def test_paper_login_requires_service_selection_but_no_trade_account(self):
        startup = self.flow.startup
        startup.mode.setCurrentIndex(startup.mode.findData("paper"))
        self.assertFalse(startup.login_button.isEnabled())
        self.assertIn("选择行情服务", startup.login_button.text())
        startup.services.selectRow(1)
        self.assertTrue(startup.login_button.isEnabled())
        startup.login_button.click()
        config = self.flow.controller.external.config
        self.assertTrue(config.paper_trading)
        self.assertFalse(config.td_front)
        self.assertFalse(config.user)
        self.assertFalse(config.allow_orders)
        self.flow.controller.gateway.event.emit(dict(kind="md_state", epoch=1, status="登录成功"))
        self.assertFalse(startup.isVisible())
        self.assertIsNotNone(self.flow.controller.paper)

    def test_authenticated_market_login_preserves_credentials_and_never_connects_trader(self):
        startup = self.flow.startup
        startup.mode.setCurrentIndex(startup.mode.findData("market"))
        startup.md_anonymous.setChecked(False)
        startup.front.setText("tcp://127.0.0.1:10001")
        startup.settings_config = ConnectionConfig(md_front=startup.front.text(), td_front="tcp://127.0.0.1:10002", broker="synthetic-broker", user="synthetic-md-user", password="old-password", app_id="synthetic-app", auth_code="synthetic-auth")
        startup.password.setText("synthetic-md-password")
        startup.login_button.click()
        session = self.flow.controller.external
        self.assertEqual(session.config.market_credentials(), ("synthetic-broker", "synthetic-md-user", "synthetic-md-password"))
        self.assertFalse(session.config.md_anonymous)
        self.assertFalse(session.config.td_front)
        self.assertFalse(session.config.allow_orders)
        self.assertFalse(session.config.auth_code)
        self.assertIsNone(session.journal_path)
        self.flow.controller.gateway.event.emit(dict(kind="md_state", epoch=1, status="登录成功"))
        self.assertFalse(startup.isVisible())
        self.assertFalse(startup.password.text())
        self.assertIsNone(startup.settings_config)

    def test_market_anonymous_selection_does_not_reuse_account_credentials(self):
        startup = self.flow.startup
        startup.mode.setCurrentIndex(startup.mode.findData("market"))
        startup.front.setText("tcp://127.0.0.1:10001")
        startup.settings_config = ConnectionConfig(md_front=startup.front.text(), broker="synthetic", user="synthetic-user", password="synthetic-password")
        startup.password.setText("synthetic-password")
        startup.login_button.click()
        config = self.flow.controller.external.config
        self.assertTrue(config.md_anonymous)
        self.assertEqual(config.market_credentials(), ("", "", ""))
        self.assertFalse(config.td_front)

    def test_market_mode_uses_independent_market_account_after_mode_switch(self):
        startup = self.flow.startup
        startup.settings_config = ConnectionConfig(md_front="tcp://127.0.0.1:10001", td_front="tcp://127.0.0.1:10002", broker="synthetic-td", user="td-user", password="td-password", md_separate_credentials=True, md_broker="synthetic-md", md_user="md-user", md_password="md-password")
        startup.md_anonymous.setChecked(False)
        startup.mode.setCurrentIndex(startup.mode.findData("market"))
        startup.front.setText(startup.settings_config.md_front)
        self.assertEqual(startup.password.text(), "md-password")
        startup.login_button.click()
        config = self.flow.controller.external.config
        self.assertEqual(config.market_credentials(), ("synthetic-md", "md-user", "md-password"))
        self.assertFalse(config.td_front)

    def test_failed_login_stays_on_startup_and_can_retry(self):
        startup = self.flow.startup
        startup.mode.setCurrentIndex(startup.mode.findData("market"))
        startup.front.setText("tcp://127.0.0.1:10001")
        startup.login_button.click()
        self.flow.controller.gateway.event.emit(dict(kind="error", channel="md", message="登录失败测试"))
        for _ in range(3):
            self.app.processEvents()
        self.assertTrue(startup.isVisible())
        self.assertFalse(startup.busy)
        self.assertIsNone(self.flow.controller.gateway)
        self.assertEqual(self.flow.controller.windows, [])
        self.assertIn("登录失败测试", startup.logs.toPlainText())
        self.assertIsNone(self.flow.controller.contract_selector)

    def test_readonly_login_keeps_trade_credentials_even_with_anonymous_market(self):
        startup = self.flow.startup
        startup.mode.setCurrentIndex(startup.mode.findData("readonly"))
        startup.front.setText("tcp://127.0.0.1:10001")
        startup.settings_config = ConnectionConfig(md_front=startup.front.text(), td_front="tcp://127.0.0.1:10002", broker="synthetic", user="synthetic-user", password="old", app_id="synthetic-app", auth_code="synthetic-code")
        startup.password.setText("synthetic-password")
        self.assertIn("交易账户仍需登录", startup.market_hint.text())
        startup.login_button.click()
        session = self.flow.controller.external
        self.assertEqual(session.config.environment, "readonly")
        self.assertEqual(session.config.password, "synthetic-password")
        self.assertEqual(session.config.user, "synthetic-user")
        self.assertTrue(session.config.md_anonymous)
        self.assertFalse(session.config.allow_orders)
        gateway = self.flow.controller.gateway
        gateway.event.emit(dict(kind="md_state", epoch=1, status="登录成功"))
        self.assertTrue(startup.isVisible())
        gateway.event.emit(dict(kind="td_login", front=1, session=1, max_ref="0", day="20261030"))
        self.assertTrue(startup.isVisible())
        self.assertTrue(session.syncing)
        self.assertIn("正在查询合约", startup.logs.toPlainText())
        for path in self.path.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'synthetic-password', path.read_bytes())
                self.assertNotIn(b'synthetic-code', path.read_bytes())

    def test_readonly_account_editor_uses_selected_environment_and_can_cancel(self):
        startup = self.flow.startup
        startup.settings_config = ConnectionConfig(md_front="tcp://127.0.0.1:10001", environment="simulation")
        startup.mode.setCurrentIndex(startup.mode.findData("readonly"))
        observed = []
        def cancel(dialog):
            observed.append(dialog.environment.currentData())
            observed.append(dialog.allow_orders.isChecked())
            return 0
        with patch("pytrade.startup.ConnectionDialog.exec", cancel):
            self.assertFalse(startup.edit_account())
        self.assertEqual(observed, ["readonly", False])
        self.assertIsNone(self.flow.controller)

    def test_login_timeout_releases_gateway_and_stays_on_login(self):
        startup = self.flow.startup
        startup.mode.setCurrentIndex(startup.mode.findData("market"))
        startup.front.setText("tcp://127.0.0.1:10001")
        startup.login_button.click()
        controller = self.flow.controller
        controller.external.md_state_at = time.monotonic() - 31
        controller.advance()
        self.flow.poll_status()
        for _ in range(3):
            self.app.processEvents()
        self.assertIsNone(controller.gateway)
        self.assertTrue(startup.isVisible())
        self.assertFalse(startup.busy)

    def test_trade_login_alone_does_not_bypass_initial_query(self):
        startup = self.flow.startup
        startup.mode.setCurrentIndex(startup.mode.findData("simulation"))
        startup.front.setText("tcp://127.0.0.1:10001")
        startup.settings_config = ConnectionConfig(md_front=startup.front.text(), td_front="tcp://127.0.0.1:10002", broker="test", user="testuser", password="test-secret")
        startup.password.setText("test-secret")
        startup.login_button.click()
        gateway = self.flow.controller.gateway
        gateway.event.emit(dict(kind="md_state", epoch=1, status="登录成功"))
        gateway.event.emit(dict(kind="td_login", front=1, session=2, max_ref="0", day="20261030"))
        self.assertTrue(startup.isVisible())
        session = self.flow.controller.external
        gateway.event.emit(dict(kind="sync_done", cycle=session.sync_cycle))
        self.assertTrue(startup.isVisible())
        for name in ("instruments", "settlement", "orders", "trades", "positions", "account"):
            gateway.event.emit(dict(kind="query_rows", query=name, cycle=session.sync_cycle, rows=[]))
        gateway.event.emit(dict(kind="sync_done", cycle=session.sync_cycle))
        self.assertFalse(startup.isVisible())
        self.assertFalse(session.ready)
        self.assertEqual(startup.password.text(), "")
        self.assertIsNone(startup.settings_config)
        self.assertEqual(self.flow.controller.windows, [])

    def test_opening_and_arranging_dom_preserves_controls_and_reuses_window(self):
        self.flow.enter_offline()
        controller = self.flow.controller
        controller.resize(860, 450)
        controller.move(30, 40)
        self.app.processEvents()
        original_geometry = controller.geometry().getRect()
        check = controller.contracts.item(0, 0)
        check.setCheckState(Qt.CheckState.Checked)
        controller.open_selected()
        self.assertTrue(controller.details_visible)
        self.assertTrue(controller.tabs.isVisible())
        self.assertEqual(controller.geometry().getRect(), original_geometry)
        self.assertEqual(len(controller.windows), 1)
        controller.open_contract_row(0, 1)
        controller.align_windows()
        self.assertEqual(controller.geometry().getRect(), original_geometry)
        self.assertEqual(len(controller.windows), 1)
        controller.set_details_visible(False)
        compact_size = controller.size()
        controller.windows[0].close()
        self.assertFalse(controller.details_visible)
        self.assertEqual(controller.size(), compact_size)
        self.assertEqual(len(controller.windows), 0)
        self.assertEqual(controller.checked_contracts, set())
        controller.set_details_visible(True)
        self.assertEqual(controller.geometry().getRect(), original_geometry)

    def test_return_to_login_closes_dom_without_auto_reopening(self):
        self.flow.enter_offline()
        controller = self.flow.controller
        controller.open_dom("SHFE.ag2612")
        controller.connect_ctp()
        self.assertTrue(self.flow.startup.isVisible())
        self.assertFalse(controller.isVisible())
        self.assertEqual(controller.windows, [])
        self.assertFalse(controller.contract_selector.isVisible())

    def test_contract_popup_preserves_filtered_selection_and_opens_only_chosen_group(self):
        self.flow.enter_offline()
        controller = self.flow.controller
        selector = controller.contract_selector
        self.assertTrue(selector.isVisible())
        self.assertFalse(selector.isModal())
        geometry = controller.geometry()
        keys = [selector.table.item(row, 1).text() for row in (0, 1)]
        selector.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
        selector.search.setText(keys[1])
        self.assertEqual(selector.table.rowCount(), 1)
        selector.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
        self.assertEqual(selector.selected, set(keys))
        self.assertEqual(controller.windows, [])
        selector.group.setValue(2)
        selector.open_button.click()
        self.assertFalse(selector.isVisible())
        self.assertEqual({window.instrument.key for window in controller.windows}, set(keys))
        self.assertTrue(all(window.group.value() == 2 for window in controller.windows))
        self.assertEqual(controller.geometry(), geometry)
        self.assertFalse(controller.ledger.orders)
        controller.select_contracts()
        selector.open_button.click()
        self.assertEqual(len(controller.windows), 2)

    def test_contract_popup_double_click_opens_one_and_close_opens_none(self):
        self.flow.enter_offline()
        controller = self.flow.controller
        selector = controller.contract_selector
        selector.reject()
        self.assertEqual(controller.windows, [])
        self.assertTrue(controller.isVisible())
        controller.select_contracts()
        key = selector.table.item(0, 1).text()
        selector.table.cellDoubleClicked.emit(0, 1)
        self.assertEqual([window.instrument.key for window in controller.windows], [key])
        self.assertFalse(selector.isVisible())

    def test_offline_home_button_freezes_hidden_simulator_and_allows_reentry(self):
        self.flow.enter_offline()
        controller = self.flow.controller
        controller.contract_selector.reject()
        controller.open_dom("SHFE.ag2612")
        controller.home_button.click()
        self.assertTrue(self.flow.startup.isVisible())
        self.assertFalse(controller.isVisible())
        self.assertFalse(controller.windows)
        sequence = controller.simulator.sequence
        QTest.qWait(600)
        self.assertEqual(controller.simulator.sequence, sequence)
        self.flow.startup.login_button.click()
        self.assertTrue(controller.isVisible())
        self.assertFalse(self.flow.startup.isVisible())
        self.assertTrue(controller.contract_selector.isVisible())
        controller.contract_selector.reject()
        QTest.qWait(600)
        self.assertGreater(controller.simulator.sequence, sequence)

    def test_home_is_accessible_from_dom_and_contract_list(self):
        self.flow.enter_offline()
        controller = self.flow.controller
        controller.contract_selector.home_button.click()
        self.assertTrue(self.flow.startup.isVisible())
        self.assertFalse(controller.contract_selector.isVisible())
        self.flow.startup.login_button.click()
        controller.contract_selector.reject()
        window = controller.open_dom("SHFE.ag2612")
        window.home_button.click()
        self.assertTrue(self.flow.startup.isVisible())
        self.assertFalse(controller.windows)
        self.assertFalse(controller.isVisible())

    def test_return_home_releases_market_connection_and_can_switch_to_offline(self):
        startup = self.flow.startup
        startup.mode.setCurrentIndex(startup.mode.findData("paper"))
        startup.services.selectRow(0)
        startup.login_button.click()
        controller = self.flow.controller
        gateway = controller.gateway
        gateway.event.emit(dict(kind="md_state", epoch=1, status="登录成功"))
        controller.home_button.click()
        self.assertTrue(startup.isVisible())
        self.assertTrue(gateway.closing)
        for _ in range(5):
            self.app.processEvents()
        self.assertIsNone(controller.gateway)
        self.assertFalse(startup.busy)
        self.assertTrue(all(command["command"] == "subscriptions" for command in gateway.commands))
        startup.mode.setCurrentIndex(startup.mode.findData("offline"))
        startup.login_button.click()
        self.assertTrue(controller.isVisible())
        self.assertTrue(controller.offline_mode)
        self.assertTrue(controller.contract_selector.isVisible())

    def test_quitting_during_login_does_not_reopen_workspace(self):
        startup = self.flow.startup
        startup.mode.setCurrentIndex(startup.mode.findData("market"))
        startup.front.setText("tcp://127.0.0.1:10001")
        startup.login_button.click()
        gateway = self.flow.controller.gateway
        self.flow.quit()
        gateway.event.emit(dict(kind="md_state", epoch=1, status="登录成功"))
        for _ in range(3):
            self.app.processEvents()
        self.assertTrue(gateway.closing)
        self.assertFalse(self.flow.connecting)
        self.assertTrue(self.flow.quitting)


if __name__ == "__main__":
    unittest.main()
