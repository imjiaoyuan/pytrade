from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from .startup import StartupWindow


class ApplicationFlow:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.controller = None
        self.connecting = False
        self.md_logged_in = False
        self.trade_queried = False
        self.quitting = False
        self.startup = StartupWindow(state_dir)
        self.startup.offline_requested.connect(self.enter_offline)
        self.startup.login_requested.connect(self.login)
        self.startup.cancel_requested.connect(self.cancel_login)
        self.startup.exit_requested.connect(self.quit)

    def show(self):
        self.startup.show()

    def workspace(self):
        if self.controller is None:
            from .app import Controller
            self.controller = Controller(self.state_dir)
            self.controller.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self.controller.use_startup_login = True
            self.controller.log_message.connect(self.startup.log)
            self.controller.connection_requested.connect(self.return_to_login)
            self.controller.timer.timeout.connect(self.poll_status)
        return self.controller

    def poll_status(self):
        if not self.connecting or not self.controller.external or self.controller.gateway.closing:
            return
        session = self.controller.external
        self.startup.statusBar().showMessage(f"行情：{session.md_status}　交易：{session.td_status}")
        if "超时" in session.md_status or "超时" in session.td_status:
            self.startup.log("CTP 连接或登录回调等待超时，本次连接将取消。TCP 可达不代表服务可用；请核对行情服务，匿名服务无需填写交易账号。")
            self.cancel_login()

    def enter_offline(self):
        controller = self.workspace()
        if controller.gateway:
            return
        if not controller.offline_mode:
            controller.enter_offline()
        self.enter_workspace()

    def login(self, config):
        controller = self.workspace()
        controller.timer.start(500)
        self.connecting = True
        self.md_logged_in = False
        self.trade_queried = False
        controller.start_external(config)
        if controller.gateway is None:
            self.connecting = False
            self.startup.set_busy(False)
            return
        controller.gateway.event.connect(self.connection_event)
        controller.gateway.stopped.connect(self.connection_stopped)
        self.startup.update_trade_status("连接中" if config.td_front else "不连接交易")

    def connection_event(self, event):
        if not self.connecting or not self.controller.external or self.controller.gateway.closing:
            return
        session = self.controller.external
        kind = event["kind"]
        if kind == "md_state":
            self.md_logged_in = event["status"].startswith("登录成功")
            self.startup.log(f"行情：{event['status']}")
        elif kind == "td_state":
            self.trade_queried = False
            self.startup.update_trade_status(event["status"])
            self.startup.log(f"交易：{event['status']}")
        elif kind == "td_login":
            self.startup.update_trade_status("登录成功，查询对账中")
            self.startup.log("交易登录成功，正在查询合约、结算单、委托、成交、持仓与资金。")
        elif kind == "sync_done":
            self.trade_queried = session.logged_in and not session.syncing
            self.startup.update_trade_status(session.td_status)
            self.startup.log(f"交易：{session.td_status}")
        elif kind in {"fatal", "error"}:
            self.startup.log("本次登录未完成，请检查日志；连接取消后可以修改配置重试。")
            self.cancel_login()
            return
        trade_required = bool(session.config.td_front)
        if self.md_logged_in and (not trade_required or self.trade_queried):
            self.enter_workspace()

    def enter_workspace(self):
        self.connecting = False
        controller = self.workspace()
        controller.timer.start(500)
        self.startup.set_busy(False)
        self.startup.clear_credentials()
        controller.show()
        controller.raise_()
        self.startup.hide()
        controller.select_contracts(reset=True)

    def cancel_login(self):
        if self.controller and self.controller.gateway:
            self.controller.disconnect_ctp()
            self.startup.log("正在取消连接，等待接口释放。")
        else:
            self.connecting = False
            self.startup.set_busy(False)

    def connection_stopped(self):
        self.startup.update_trade_status("已断开，可重新登录")
        if self.connecting:
            self.connecting = False
            self.startup.set_busy(False)
            self.startup.log("连接已结束，仍停留在登录页。")

    def return_to_login(self):
        controller = self.controller
        controller.timer.stop()
        if controller.contract_selector:
            controller.contract_selector.hide()
        if controller.windows:
            controller.save_layout()
        for window in list(controller.windows):
            window.close()
        controller.hide()
        self.startup.show()
        self.startup.raise_()
        if controller.gateway:
            self.connecting = True
            self.startup.set_busy(True)
            controller.disconnect_ctp()
        else:
            self.startup.set_busy(False)
        self.startup.log("请选择服务及本次账户，重新登录不会重发委托。")

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        self.connecting = False
        self.startup.hide()
        self.startup.deleteLater()
        if self.controller:
            if self.controller.contract_selector:
                self.controller.contract_selector.hide()
            self.controller.close()
        else:
            QApplication.instance().quit()
