import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)

from .connection_ui import ConnectionDialog
from .external import ConnectionConfig
from .resources import reference_path
from .version import VERSION
from .theme import COMBO_POPUP_STYLE


class StartupWindow(QMainWindow):
    offline_requested = Signal()
    login_requested = Signal(object)
    cancel_requested = Signal()
    exit_requested = Signal()

    def __init__(self, state_dir, trial_state=None):
        super().__init__()
        self.state_dir = Path(state_dir)
        self.trial_state = trial_state
        self.settings_config = None
        self.busy = False
        self.setWindowTitle("意意期货交易 · 登录与行情选择")
        self.update_trial_title()
        self.resize(1030, 660)
        self.setMinimumSize(850, 540)
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)
        root.addWidget(QLabel(f"PyTrade {VERSION} · 离线 / 真实行情试用 · 实盘仅登录和查询"))
        split = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(split, 1)
        left = QWidget()
        layout = QVBoxLayout(left)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.services = self.table(("行情名称", "行情地址", "当前延迟", "历史验证"))
        self.services.setColumnWidth(0, 100)
        self.services.setColumnWidth(1, 245)
        self.services.setColumnWidth(2, 70)
        self.services.setColumnWidth(3, 190)
        self.services.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.services, 3)
        self.services.itemSelectionChanged.connect(self.select_service)
        lower = QSplitter(Qt.Orientation.Horizontal)
        self.accounts = self.table(("交易账户",))
        lower.addWidget(self.accounts)
        self.trade_fronts = self.table(("交易地址", "状态"))
        self.trade_fronts.setColumnWidth(0, 290)
        self.trade_fronts.horizontalHeader().setStretchLastSection(True)
        lower.addWidget(self.trade_fronts)
        lower.setSizes([130, 400])
        layout.addWidget(lower, 2)
        address = QHBoxLayout()
        self.account_button = QPushButton("⚙ 账号 / 接口")
        self.account_button.clicked.connect(self.edit_account)
        self.manage_button = QPushButton("账号管理…")
        self.manage_button.clicked.connect(self.manage_accounts)
        self.accounts.cellDoubleClicked.connect(lambda row, column: self.edit_account())
        self.trade_fronts.cellDoubleClicked.connect(lambda row, column: self.edit_account())
        address.addWidget(self.account_button)
        address.addWidget(self.manage_button)
        self.current = QLabel("当前行情服务：尚未选择")
        self.current.setStyleSheet("color:#008c9e")
        address.addWidget(self.current, 1)
        layout.addLayout(address)
        self.front = QLineEdit()
        self.front.setPlaceholderText("选择上方行情服务，或输入自定义 tcp://主机:端口")
        layout.addWidget(self.front)
        self.md_anonymous = QCheckBox("行情使用空凭据登录（无需账号密码）")
        self.md_anonymous.setChecked(True)
        self.md_anonymous.toggled.connect(self.market_login_changed)
        layout.addWidget(self.md_anonymous)
        self.market_hint = QLabel()
        self.market_hint.setWordWrap(True)
        layout.addWidget(self.market_hint)
        mode_line = QHBoxLayout()
        mode_line.addWidget(QLabel("运行模式"))
        self.mode = QComboBox()
        self.mode.setStyleSheet(COMBO_POPUP_STYLE)
        for title, value in (("离线模拟", "offline"), ("在线模拟（真实行情＋本地撮合）", "paper"), ("真实行情（交易不连接）", "market"), ("外部模拟交易", "simulation"), ("实盘只读", "readonly")):
            self.mode.addItem(title, value)
        mode_line.addWidget(self.mode, 1)
        layout.insertLayout(0, mode_line)
        bottom = QHBoxLayout()
        self.password_label = QLabel("交易密码：")
        self.password_label.setStyleSheet("color:#c12727")
        bottom.addWidget(self.password_label)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setMaximumWidth(140)
        self.password_label.setBuddy(self.password)
        self.password.returnPressed.connect(self.login)
        bottom.addWidget(self.password)
        self.refresh_contracts = QCheckBox("刷新合约")
        self.refresh_contracts.setChecked(True)
        self.refresh_contracts.setEnabled(False)
        self.refresh_contracts.setToolTip("交易登录后自动查询接口合约；行情单独登录仅使用历史元数据")
        bottom.addWidget(self.refresh_contracts)
        for title in ("独立", "备用"):
            box = QCheckBox(title)
            box.setEnabled(False)
            box.setToolTip("原版行为尚未确认")
            bottom.addWidget(box)
        self.login_button = QPushButton("进入离线模拟")
        self.login_button.clicked.connect(self.login)
        bottom.addWidget(self.login_button)
        self.cancel_button = QPushButton("取消连接")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self.cancel_requested)
        bottom.addWidget(self.cancel_button)
        layout.addLayout(bottom)
        split.addWidget(left)
        self.logs = QTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setFont(QFont("新宋体", 12))
        self.logs.document().setMaximumBlockCount(500)
        split.addWidget(self.logs)
        split.setSizes([690, 330])
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.front.textChanged.connect(self.update_login_enabled)
        self.load_services()
        self.mode_changed()
        self.log("离线模拟无需选择服务；使用真实行情或交易，请先切换运行模式。")

    def update_trial_title(self, state=None):
        state = state or self.trial_state
        if state is None:
            return
        self.trial_state = state
        self.setWindowTitle(f"意意期货交易 · 登录与行情选择 · {state.label()}")

    def table(self, headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(24)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        return table

    def log(self, message):
        self.logs.append(f"{datetime.now():%H:%M:%S}  {message}")

    def load_services(self):
        path = reference_path("service_catalog.json")
        try:
            rows = [row for row in json.loads(path.read_text(encoding="utf-8")).get("services", []) if row.get("category") in {"original_market", "public_simulation"}]
            self.services.setRowCount(len(rows))
            for index, row in enumerate(rows):
                observed = row.get("status") == "stream_verified" and row.get("quote_count", 0) > 0
                date = row.get("checked_at_utc", "")[:10] or "日期未知"
                evidence = f"{date} 空凭据曾实收" if observed else f"{date} 未收到行情"
                name = row["name"] + ("〔公共模拟〕" if row.get("category") == "public_simulation" else "")
                for column, value in enumerate((name, row["front"], "未测", evidence)):
                    self.services.setItem(index, column, QTableWidgetItem(value))
                tip = "公共模拟环境（SimNow/openctp等），非原软件地址；可用性由服务方控制，深度可能与真实柜台不同。" if row.get("category") == "public_simulation" else "历史行情API实验，不代表原EXE的登录方式或当前可用性；只有实际订阅回调才能确认当前行情。"
                self.services.item(index, 3).setToolTip(tip)
        except (OSError, ValueError, KeyError, TypeError):
            self.log("本地行情服务目录不可读，可手动填写行情前置。")
        try:
            saved = json.loads((self.state_dir / "connection.json").read_text(encoding="utf-8"))
            self.front.setText(str(saved.get("md_front", "")))
            if self.front.text():
                self.current.setText("当前行情服务：已保存地址（未连接）")
        except (OSError, ValueError, AttributeError):
            pass

    def select_service(self):
        row = self.services.currentRow()
        if row >= 0:
            name = self.services.item(row, 0).text()
            address = self.services.item(row, 1).text()
            self.front.setText(address)
            self.current.setText(f"当前行情服务：{name}")
            self.log(f"选择行情服务：{name}  {address}；尚未连接")

    def mode_changed(self, index=None):
        offline = self.mode.currentData() == "offline"
        self.account_button.setText("⚙ 实盘账号 / 接口" if self.mode.currentData() == "readonly" else "⚙ 账号 / 接口")
        self.login_button.setText("进入工作台" if offline else "登录并进入")
        self.services.setEnabled(not offline and not self.busy)
        self.front.setEnabled(not offline and not self.busy)
        self.account_button.setEnabled(not offline and not self.busy)
        if offline:
            self.current.setText("离线模拟：无需行情服务或交易账户")
        else:
            row = self.services.currentRow()
            name = self.services.item(row, 0).text() if row >= 0 else "请选择服务或填写地址"
            self.current.setText(f"当前行情服务：{name}")
        self.md_anonymous.setEnabled(not offline and not self.busy)
        if index is not None and self.settings_config:
            password = self.settings_config.market_credentials()[2] if self.mode.currentData() in {"market", "paper"} else self.settings_config.password
            self.password.setText(password)
        self.market_login_changed()
        self.update_login_enabled()
        self.statusBar().showMessage("离线模拟无需账号" if offline else "行情和交易状态分别验证；登录成功后进入合约选择")
        if index is not None and self.mode.currentData() == "paper":
            self.log("在线模拟无需交易账号；请先点击上方选择行情服务。收到有效真实报价后才进行本地撮合。")

    def update_login_enabled(self):
        offline = self.mode.currentData() == "offline"
        selected = bool(self.front.text().strip())
        self.login_button.setEnabled(not self.busy and (offline or selected))
        self.login_button.setText("进入工作台" if offline else "登录并进入" if selected else "请先选择行情服务")
        self.login_button.setToolTip("请先点击上方任意一行行情服务，地址会自动填入；在线模拟无需交易账号" if not offline and not selected else "")

    def market_login_changed(self):
        mode = self.mode.currentData()
        authenticated = not self.md_anonymous.isChecked()
        needs_password = mode in {"readonly", "simulation"} or (mode in {"market", "paper"} and authenticated)
        self.password_label.setText("行情密码：" if mode in {"market", "paper"} else "交易密码：")
        self.password_label.setVisible(needs_password)
        self.password.setVisible(needs_password)
        self.password.setEnabled(not self.busy and needs_password)
        self.password.setPlaceholderText("请输入行情密码" if mode in {"market", "paper"} else "请输入交易密码")
        self.password.setToolTip("账号、BrokerID 和接口地址在“账号 / 接口”填写；密码仅本次连接使用")
        if mode == "offline":
            self.market_hint.setText("离线模拟不连接行情或交易服务。")
        elif authenticated:
            self.market_hint.setText("行情账户登录：点击“账号 / 接口”填写服务方提供的 BrokerID、账号和密码；仅行情模式不连接交易接口。")
        else:
            self.market_hint.setText("空凭据行情：选择服务后登录，无需填写密码。历史通过不保证当前可用；仅行情模式不连接交易接口。")
        if mode == "paper":
            self.market_hint.setText("在线模拟：真实行情驱动本地限价撮合，不连接交易账户、不发送外部委托；不含手续费/保证金，持仓仅本次会话有效。" + ("行情需要账户时请在“账号 / 接口”填写。" if authenticated else "选择允许空凭据的服务即可尝试连接。"))
        elif mode in {"readonly", "simulation"}:
            self.market_hint.setText(("实盘登录与查询" if mode == "readonly" else "外部模拟账户登录") + "：在“账号 / 接口”填写期货公司提供的交易前置、BrokerID、期货账号及交易密码；需要认证时填写 AppID 和授权码。上面的空凭据选项仅适用于行情，交易账户仍需登录。" + ("实盘报撤单保持关闭。" if mode == "readonly" else ""))

    def manage_accounts(self):
        from .accounts_ui import AccountManagerDialog
        dialog = AccountManagerDialog(self, self.state_dir)
        dialog.exec()

    def edit_account(self):
        previous_mode = self.mode.currentData()
        market_only = previous_mode in {"market", "paper"}
        dialog = ConnectionDialog(self.state_dir, self, market_only=market_only)
        dialog.fields["md_front"].setText(self.front.text())
        if self.settings_config:
            config = self.settings_config
            for key, edit in dialog.fields.items():
                edit.setText(str(getattr(config, key)))
            dialog.fields["md_front"].setText(self.front.text())
            dialog.environment.setCurrentIndex(dialog.environment.findData(config.environment))
            dialog.allow_orders.setChecked(config.allow_orders)
            dialog.md_anonymous.setChecked(config.md_anonymous)
            dialog.md_separate.setChecked(config.md_separate_credentials)
            dialog.max_order.setValue(config.max_order)
            dialog.max_position.setValue(config.max_position)
            dialog.stale.setValue(config.stale_seconds)
            for key, field in dialog.risk_fields.items():
                field.setText(str(getattr(config, key)))
            if market_only:
                broker, user, password = config.market_credentials()
                for key, value in (("broker", broker), ("user", user), ("password", password)):
                    dialog.fields[key].setText(value)
        if previous_mode in {"readonly", "simulation"}:
            dialog.environment.setCurrentIndex(dialog.environment.findData(previous_mode))
        dialog.md_anonymous.setChecked(self.md_anonymous.isChecked())
        if market_only:
            for key in ("td_front", "app_id", "auth_code"):
                dialog.fields[key].clear()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings_config = dialog.config
            self.front.setText(dialog.config.md_front)
            self.password.setText(dialog.config.password)
            self.md_anonymous.setChecked(dialog.config.md_anonymous)
            if dialog.config.td_front:
                self.mode.setCurrentIndex(self.mode.findData(dialog.config.environment))
                self.accounts.setRowCount(1)
                self.accounts.setItem(0, 0, QTableWidgetItem("CTP 账户（本次）"))
                self.trade_fronts.setRowCount(1)
                self.trade_fronts.setItem(0, 0, QTableWidgetItem(dialog.config.td_front))
                self.trade_fronts.setItem(0, 1, QTableWidgetItem("未连接"))
            else:
                self.mode.setCurrentIndex(self.mode.findData("paper" if previous_mode == "paper" else "market"))
                self.accounts.setRowCount(0)
                self.trade_fronts.setRowCount(0)
            self.log("本次账户配置已更新，尚未发起连接；密码与授权码不保存。")
            return True
        return False

    def login(self):
        if self.busy:
            return
        mode = self.mode.currentData()
        if mode == "offline":
            self.offline_requested.emit()
            return
        if mode in {"readonly", "simulation"} and (not self.settings_config or not self.settings_config.td_front):
            if not self.edit_account():
                return
            mode = self.mode.currentData()
        try:
            if not self.front.text().strip():
                raise ValueError("请先点击上方一行行情服务，或填写行情地址；在线模拟不需要交易账号")
            if mode in {"market", "paper"}:
                if self.md_anonymous.isChecked():
                    config = ConnectionConfig(md_front=self.front.text().strip())
                else:
                    if not self.settings_config:
                        raise ValueError("请先点击“账号 / 接口”填写本次行情账户的 BrokerID、账号和密码")
                    config = replace(self.settings_config, md_front=self.front.text().strip(), td_front="", password=self.password.text(), md_anonymous=False, allow_orders=False, environment="simulation", app_id="", auth_code="", probe_mode=False)
                    if config.md_separate_credentials:
                        config = replace(config, md_password=self.password.text())
                config = replace(config, paper_trading=mode == "paper")
            else:
                if not self.settings_config or not self.settings_config.td_front:
                    raise ValueError("请先点击“账号 / 接口”填写本次交易账户与前置")
                config = replace(self.settings_config, md_front=self.front.text().strip(), password=self.password.text(), environment=mode, md_anonymous=self.md_anonymous.isChecked(), allow_orders=self.settings_config.allow_orders if mode == "simulation" else False)
            config.validate()
        except ValueError as error:
            QMessageBox.warning(self, "登录配置不完整", str(error))
            return
        self.set_busy(True)
        self.log("正在连接，等待接口登录响应；不会将连接成功视为行情或交易已就绪。")
        self.login_requested.emit(config)

    def set_busy(self, busy):
        self.busy = busy
        for widget in (self.services, self.front, self.mode, self.account_button, self.manage_button, self.login_button, self.md_anonymous):
            widget.setEnabled(not busy)
        self.market_login_changed()
        self.cancel_button.setVisible(busy)
        if not busy:
            self.mode_changed()

    def clear_credentials(self):
        self.password.clear()
        self.settings_config = None
        self.accounts.setRowCount(0)
        self.trade_fronts.setRowCount(0)

    def update_trade_status(self, status):
        if self.trade_fronts.rowCount():
            self.trade_fronts.setItem(0, 1, QTableWidgetItem(status))

    def closeEvent(self, event):
        self.exit_requested.emit()
        event.ignore()
