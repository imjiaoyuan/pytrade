from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from .configuration import AccountStore, copy_instrument_table
from .external import ConnectionConfig


class AccountDialog(QDialog):
    def __init__(self, parent, store, name=""):
        super().__init__(parent)
        self.store = store
        self.name = name
        self.setWindowTitle("编辑账号" if name else "添加账号")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.fields = {}
        profile = store.read().get(name, {}) if name else {}
        for key, label, placeholder in (("name", "账号名称", "例如：模拟A"), ("md_front", "行情前置", "tcp://主机:端口"), ("td_front", "交易前置（可留空）", "tcp://主机:端口"), ("broker", "BrokerID", ""), ("user", "账号", ""), ("app_id", "AppID（可选）", ""), ("auth_code", "授权码（可选）", "")):
            edit = QLineEdit(str(profile.get(key, "")) if profile.get(key) else "")
            edit.setPlaceholderText(placeholder)
            self.fields[key] = edit
            form.addRow(label, edit)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("留空则连接时再输入；密码不保存")
        form.addRow("密码", self.password)
        self.max_order = QSpinBox()
        self.max_order.setRange(1, 1000)
        self.max_order.setValue(int(profile.get("max_order", 1)))
        form.addRow("本地单笔上限（手）", self.max_order)
        self.max_position = QSpinBox()
        self.max_position.setRange(1, 100000)
        self.max_position.setValue(int(profile.get("max_position", 10)))
        form.addRow("单合约单边上限（手）", self.max_position)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save(self):
        name = self.fields["name"].text().strip()
        if not name:
            QMessageBox.warning(self, "账号管理", "账号名称不能为空")
            return
        values = {key: edit.text().strip() for key, edit in self.fields.items()}
        values.update(max_order=self.max_order.value(), max_position=self.max_position.value())
        if values["md_front"]:
            try:
                ConnectionConfig(md_front=values["md_front"], td_front=values["td_front"], broker=values["broker"], app_id=values["app_id"]).validate()
            except ValueError as error:
                QMessageBox.warning(self, "账号管理", f"接口参数无效：{error}")
                return
        try:
            self.store.save_profile(name, values, previous=self.name)
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "账号管理", str(error))
            return
        self.accept()


class AccountManagerDialog(QDialog):
    def __init__(self, parent, state_dir):
        super().__init__(parent)
        self.store = AccountStore(Path(state_dir) / "accounts.json")
        self.setWindowTitle("账号管理")
        self.resize(760, 460)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("账号、接口与风控参数的管理入口。密码与授权码不落盘，每次连接时输入。\n“复制合约表”把一个账号保存的合约元数据复制给另一账号；实时行情、持仓与委托不复制。"))
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(("账号名称", "行情前置", "交易前置", "BrokerID", "AppID", "上次使用"))
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        for label, action in (("添加", self.add), ("修改", self.edit), ("删除", self.remove), ("上移", lambda: self.move(-1)), ("下移", lambda: self.move(1)), ("复制合约表…", self.copy_table)):
            button = QPushButton(label)
            button.clicked.connect(action)
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.hint = QLabel()
        layout.addWidget(self.hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.table.cellDoubleClicked.connect(lambda row, column: self.edit())
        self.refresh()

    def current_name(self):
        row = self.table.currentRow()
        return self.table.item(row, 0).text() if row >= 0 else ""

    def refresh(self):
        data = self.store.read()
        self.table.setRowCount(len(data))
        for row, (name, profile) in enumerate(data.items()):
            for column, value in enumerate((name, profile.get("md_front", ""), profile.get("td_front", ""), profile.get("broker", ""), profile.get("app_id", ""), profile.get("used_at", "未使用"))):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self.hint.setText(f"共 {len(data)} 个账号档案；凭据不保存。")

    def add(self):
        dialog = AccountDialog(self, self.store)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def edit(self):
        name = self.current_name()
        if not name:
            self.hint.setText("请先选择账号")
            return
        dialog = AccountDialog(self, self.store, name)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh()

    def remove(self):
        name = self.current_name()
        if not name:
            self.hint.setText("请先选择账号")
            return
        confirm = QMessageBox.question(self, "删除账号", f"删除账号档案“{name}”？不会删除交易日志。")
        if confirm == QMessageBox.StandardButton.Yes:
            try:
                self.store.delete(name)
            except (ValueError, OSError) as error:
                QMessageBox.warning(self, "账号管理", str(error))
            self.refresh()

    def move(self, offset):
        name = self.current_name()
        if not name:
            return
        try:
            self.store.reorder(name, offset)
        except ValueError:
            return
        self.refresh()
        names = list(self.store.read())
        if name in names:
            self.table.selectRow(names.index(name))

    def copy_table(self):
        names = list(self.store.read())
        if not names:
            self.hint.setText("没有可复制的账号档案")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("复制合约表")
        form = QFormLayout(dialog)
        source = QComboBox()
        source.addItems(names)
        target = QComboBox()
        target.addItems(names)
        form.addRow("源账号（已保存合约）", source)
        form.addRow("目标账号", target)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if source.currentText() == target.currentText():
            self.hint.setText("源账号与目标账号相同，未复制")
            return
        try:
            count = copy_instrument_table(self.store, source.currentText(), target.currentText())
            self.hint.setText(f"已把 {count} 条合约元数据从“{source.currentText()}”复制到“{target.currentText()}”；实时行情与持仓不复制。")
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "复制合约表", str(error))
