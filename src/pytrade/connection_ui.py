import json
from pathlib import Path

from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget

from .external import ConnectionConfig
from .connection_presets import SIMNOW_PRESETS, simnow_parameters


class ConnectionDialog(QDialog):
    def __init__(self, state_dir, parent=None, market_only=False):
        super().__init__(parent)
        self.setWindowTitle("CTP 行情与交易连接")
        self.profile = Path(state_dir) / "connection.json"
        self.market_only = market_only
        self.paper_only = False
        self.resize(570, 430 if market_only else 700)
        root = QVBoxLayout(self)
        self.paper_button = QPushButton("只接行情，改用在线模拟（无需交易账号）")
        self.paper_button.clicked.connect(self.use_online_simulation)
        self.paper_button.setVisible(not market_only)
        root.addWidget(self.paper_button)
        fields_container = QWidget()
        form = QFormLayout(fields_container)
        fields_scroll = QScrollArea()
        fields_scroll.setWidgetResizable(True)
        fields_scroll.setWidget(fields_container)
        root.addWidget(fields_scroll, 1)
        presets = QHBoxLayout()
        self.preset = QComboBox()
        self.preset.addItem("选择公开参数预设（可编辑）", "")
        for name, values in SIMNOW_PRESETS.items():
            self.preset.addItem(values["label"], name)
        presets.addWidget(self.preset, 1)
        apply_preset = QPushButton("填入预设")
        apply_preset.clicked.connect(self.apply_preset)
        presets.addWidget(apply_preset)
        form.addRow("SimNow 参数", presets)
        self.preset_hint = QLabel("预设只填写公开地址和认证参数，不填个人账号密码，不自动连接。地址以 SimNow 控制台最新公布为准。")
        self.preset_hint.setWordWrap(True)
        form.addRow(self.preset_hint)
        self.environment = QComboBox()
        self.environment.addItem("外部模拟（真实接口）", "simulation")
        self.environment.addItem("实盘只读（登录与查询）", "readonly")
        form.addRow("交易环境", self.environment)
        self.fields = {}
        for key, label in (("md_front", "行情前置"), ("td_front", "交易前置（可留空）"), ("broker", "BrokerID"), ("user", "行情账号" if market_only else "期货账号"), ("password", "密码"), ("app_id", "AppID"), ("auth_code", "授权码"), ("md_broker", "独立行情 BrokerID"), ("md_user", "独立行情账号"), ("md_password", "独立行情密码")):
            edit = QLineEdit()
            if key in {"password", "auth_code", "md_password"}:
                edit.setEchoMode(QLineEdit.EchoMode.Password)
            if key.endswith("front"):
                edit.setPlaceholderText("tcp://主机:端口")
            self.fields[key] = edit
            form.addRow(label, edit)
        self.max_order = QSpinBox()
        self.max_order.setRange(1, 1000)
        self.max_order.setValue(1)
        form.addRow("本地单笔上限（手）", self.max_order)
        self.max_position = QSpinBox()
        self.max_position.setRange(1, 100000)
        self.max_position.setValue(10)
        form.addRow("单合约单边上限（手）", self.max_position)
        self.risk_fields = {}
        for name, label in (("max_total", "账户总持仓上限（手）"), ("max_group", "每组持仓上限（手）"), ("max_cancels", "日撤单请求上限"), ("max_daily_loss", "日亏损上限（人民币）")):
            field = QLineEdit()
            field.setPlaceholderText("启用外部模拟交易前必填")
            self.risk_fields[name] = field
            form.addRow(label, field)
        try:
            saved_risk = json.loads((Path(state_dir) / "risk.json").read_text(encoding="utf-8"))
            for key, field in self.risk_fields.items():
                field.setText(str(saved_risk.get(key, "")))
        except (OSError, ValueError, AttributeError):
            pass
        self.stale = QSpinBox()
        self.stale.setRange(2, 300)
        self.stale.setValue(10)
        form.addRow("行情过期阈值（秒）", self.stale)
        self.allow_orders = QCheckBox("我已核对这是模拟前置，启用外部模拟报撤单")
        root.addWidget(self.allow_orders)
        self.md_anonymous = QCheckBox("行情使用空凭据登录（仅服务允许时可用）")
        self.md_anonymous.setChecked(True)
        self.md_anonymous.toggled.connect(self.update_credentials)
        root.addWidget(self.md_anonymous)
        self.md_separate = QCheckBox("行情使用独立账户（不同于交易账户）")
        self.md_separate.toggled.connect(self.update_credentials)
        root.addWidget(self.md_separate)
        self.form = form
        self.environment.currentIndexChanged.connect(self.mode_changed)
        self.connection_hint = QLabel("只连接行情，不创建交易接口。\n空凭据登录无需账号；账户登录使用服务方提供的 BrokerID、账号和密码。\n凭据仅本次使用，不保存。" if market_only else "外部模拟需要登录交易账户；行情空凭据不会免除交易登录。\nSimNow 填 InvestorID 和账户密码，不是要求另设一套密码。\n账号、密码和授权码仅本次使用，不保存；实盘仅登录与查询。")
        self.connection_hint.setWordWrap(True)
        root.addWidget(self.connection_hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存本次配置")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept_config)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        try:
            saved = json.loads(self.profile.read_text(encoding="utf-8"))
            for key in ("md_front", "td_front", "broker", "app_id"):
                self.fields[key].setText(str(saved.get(key, "")))
        except (OSError, ValueError, AttributeError):
            pass
        self.config = None
        if self.market_only:
            self.setWindowTitle("行情连接与账户（不连接交易接口）")
            form.setRowVisible(self.environment, False)
            for key in ("td_front", "app_id", "auth_code"):
                self.fields[key].clear()
                form.setRowVisible(self.fields[key], False)
            for field in (self.max_order, self.max_position, *self.risk_fields.values()):
                form.setRowVisible(field, False)
            self.allow_orders.setChecked(False)
            self.allow_orders.hide()
            self.md_separate.hide()
        self.update_credentials()

    def apply_preset(self):
        name = self.preset.currentData()
        if not name:
            return
        values = simnow_parameters(name, self.market_only)
        self.environment.setCurrentIndex(self.environment.findData("simulation"))
        self.allow_orders.setChecked(False)
        self.md_separate.setChecked(False)
        self.md_anonymous.setChecked(self.market_only)
        for key in ("user", "password", "md_broker", "md_user", "md_password"):
            self.fields[key].clear()
        for key, value in values.items():
            self.fields[key].setText(value)
        self.fields["user"].setPlaceholderText("填写自己的 SimNow InvestorID（期货账号）")
        self.fields["password"].setPlaceholderText("填写自己的 SimNow 账户密码")
        self.preset_hint.setText("已填入公开参数。连接交易时填写 SimNow InvestorID 和账户密码，不是要求另设一套密码；请按服务方要求核对或修改密码。只看行情或本地练习可使用上方在线模拟入口。新账号的环境同步以服务方为准。报单尚未开启。")
        self.update_credentials()

    def use_online_simulation(self):
        self.market_only = True
        self.paper_only = True
        self.environment.setCurrentIndex(self.environment.findData("simulation"))
        self.form.setRowVisible(self.environment, False)
        for key in ("td_front", "broker", "user", "password", "app_id", "auth_code", "md_broker", "md_user", "md_password"):
            self.fields[key].clear()
        for key in ("td_front", "app_id", "auth_code"):
            self.form.setRowVisible(self.fields[key], False)
        for field in (self.max_order, self.max_position, *self.risk_fields.values()):
            self.form.setRowVisible(field, False)
        self.allow_orders.setChecked(False)
        self.allow_orders.hide()
        self.md_separate.setChecked(False)
        self.md_separate.hide()
        self.md_anonymous.setChecked(True)
        self.paper_button.hide()
        self.setWindowTitle("在线模拟：只连接行情，不连接交易账户")
        self.form.labelForField(self.fields["user"]).setText("行情账号")
        self.resize(570, 430)
        self.preset_hint.setText("已移除本表单的交易配置。保存后切换在线模拟，再点击登录；行情源允许空凭据时无需账号密码。本地撮合不发送外部委托。取消则保留首页原配置。")
        self.connection_hint.setText("在线模拟只连接行情，不创建交易接口、不发送外部委托。\n允许空凭据的行情源无需密码；需要行情账户时取消空凭据勾选。")
        self.update_credentials()

    def update_credentials(self):
        enabled = not (self.market_only and self.md_anonymous.isChecked())
        for key in ("broker", "user", "password"):
            self.fields[key].setEnabled(enabled)
            self.form.setRowVisible(self.fields[key], enabled)
        independent = not self.market_only and self.md_separate.isChecked()
        self.md_separate.setEnabled(not self.md_anonymous.isChecked())
        for key in ("md_broker", "md_user", "md_password"):
            self.form.setRowVisible(self.fields[key], independent)
            self.fields[key].setEnabled(independent and not self.md_anonymous.isChecked())

    def mode_changed(self):
        allowed = self.environment.currentData() == "simulation"
        self.allow_orders.setEnabled(allowed)
        if not allowed:
            self.allow_orders.setChecked(False)
        if not self.market_only:
            for field in (self.max_order, self.max_position, *self.risk_fields.values()):
                self.form.setRowVisible(field, allowed)
            self.allow_orders.setVisible(allowed)
            self.setWindowTitle("CTP 行情与交易连接" if allowed else "实盘账户登录与查询")

    def accept_config(self):
        values = {key: edit.text() if key in {"password", "auth_code", "md_password"} else edit.text().strip() for key, edit in self.fields.items()}
        if self.market_only:
            values.update(td_front="", app_id="", auth_code="")
            if self.md_anonymous.isChecked():
                values.update(broker="", user="", password="")
        if self.market_only or not self.md_separate.isChecked() or self.md_anonymous.isChecked():
            values.update(md_broker="", md_user="", md_password="")
        try:
            risk = {} if self.market_only else {name: (field.text().strip() or "0") if name == "max_daily_loss" else int(field.text() or "0") for name, field in self.risk_fields.items()}
        except ValueError:
            QMessageBox.warning(self, "连接配置", "风险手数和次数须为整数")
            return
        config = ConnectionConfig(**values, **risk, environment=self.environment.currentData(), paper_trading=self.paper_only, allow_orders=not self.market_only and self.allow_orders.isChecked(), max_order=self.max_order.value(), max_position=self.max_position.value(), stale_seconds=self.stale.value(), md_anonymous=self.md_anonymous.isChecked(), md_separate_credentials=not self.market_only and self.md_separate.isChecked() and not self.md_anonymous.isChecked())
        try:
            config.validate()
            if not self.market_only and config.environment == "readonly" and not config.td_front:
                raise ValueError("实盘账户登录需要期货公司提供的交易前置；行情地址不能代替交易前置")
            saved = {key: getattr(config, key) for key in ("md_front", "td_front", "broker", "app_id")}
            self.profile.parent.mkdir(parents=True, exist_ok=True)
            self.profile.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
            if config.allow_orders:
                (self.profile.parent / "risk.json").write_text(json.dumps(risk, ensure_ascii=False, indent=2), encoding="utf-8")
        except (ValueError, OSError) as error:
            message = str(error)
            if bool(config.app_id) != bool(config.auth_code):
                self.fields["auth_code" if config.app_id else "app_id"].setFocus()
                message += "。SimNow 请在顶部选择对应环境并点击“填入预设”，再填写自己的账号密码；只接行情可点击“改用在线模拟”。"
            QMessageBox.warning(self, "连接配置", message)
            return
        self.config = config
        for edit in self.fields.values():
            edit.clear()
        self.accept()
