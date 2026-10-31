import json
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout,
)

from .configuration import parse_legacy
from .records import export_csv
from .risk import RiskLimits, position_id
from .resources import reference_path
from .availability import availability


def guarded(parent, action):
    try:
        return action()
    except (ValueError, OSError, KeyError, ArithmeticError) as error:
        QMessageBox.warning(parent, "操作未完成", str(error))


def import_configuration(controller):
    path, _ = QFileDialog.getOpenFileName(controller, "选择原配置副本", "", "配置 (*.ini *.xml);;所有文件 (*)")
    if not path:
        return
    def perform():
        parsed = parse_legacy(path)
        dialog = QDialog(controller)
        dialog.setWindowTitle("原配置导入预览")
        dialog.resize(650, 500)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("只应用已识别的显示参数；未知字段保留，交易及远程服务字段不启用。用户保存值优先。"))
        preview = QTextEdit()
        preview.setReadOnly(True)
        text = [f"编码：{parsed['encoding']}", f"显示候选：{parsed['candidates']}", *parsed['diagnostics'], "", "保留的键值："]
        text.extend(f"{entry['line']}: [{entry['section']}] {entry['key']}={entry['value']}" for entry in parsed["entries"])
        if parsed["candidates"].get("font") not in QFontDatabase.families():
            text.append("候选字体未安装，运行时继续使用当前字体")
        preview.setPlainText("\n".join(text))
        layout.addWidget(preview)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用导入")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            controller.config_store.apply_import(parsed)
            controller.apply_display_config()
            controller.log("已显式导入显示配置；未知字段保留，敏感字段不导入")
    guarded(controller, perform)


def display_settings(controller):
    dialog = QDialog(controller)
    dialog.setWindowTitle("DOM 显示设置")
    layout = QFormLayout(dialog)
    config = controller.config_store.effective()
    font = QComboBox()
    font.setEditable(True)
    font.addItems(QFontDatabase.families())
    font.setCurrentText(config.font)
    layout.addRow("字体", font)
    fields = {}
    for name, label, low, high in (("row_height", "价梯行高", 10, 30), ("dom_height", "新窗口高度", 540, 1800), ("wheel_steps", "滚轮步数", 1, 100)):
        field = QSpinBox()
        field.setRange(low, high)
        field.setValue(getattr(config, name))
        fields[name] = field
        layout.addRow(label, field)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addRow(buttons)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        def save():
            controller.config_store.set_overrides(dict(font=font.currentText(), **{key: value.value() for key, value in fields.items()}))
            controller.apply_display_config()
        guarded(controller, save)


def export_records(controller):
    dialog = QDialog(controller)
    dialog.setWindowTitle("筛选并导出记录")
    layout = QFormLayout(dialog)
    kind = QComboBox()
    tables = {"委托": controller.orders, "成交": controller.trades, "持仓": controller.positions}
    kind.addItems(tables)
    query = QLineEdit()
    query.setPlaceholderText("合约、委托号或其他字段，留空导出全部")
    layout.addRow("记录", kind)
    layout.addRow("包含文字", query)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addRow(buttons)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return
    path, _ = QFileDialog.getSaveFileName(controller, "导出 CSV", "records.csv", "CSV (*.csv)")
    if not path:
        return
    table = tables[kind.currentText()]
    headers = [table.horizontalHeaderItem(column).text() for column in range(table.columnCount())]
    rows = [[table.item(row, column).text() if table.item(row, column) else "" for column in range(table.columnCount())] for row in range(table.rowCount())]
    rows = [row for row in rows if query.text().casefold() in " ".join(row).casefold()]
    guarded(controller, lambda: export_csv(path, headers, rows))


def export_acceptance(controller):
    if controller.paper:
        QMessageBox.information(controller, "联调报告", "在线模拟只有本地成交，不能作为外部账户验收；可使用CSV导出本地记录。")
        return
    if not controller.external:
        QMessageBox.information(controller, "联调报告", "请先连接接口；离线模拟不作为外部账户验收。")
        return
    path, _ = QFileDialog.getSaveFileName(controller, "导出本地联调报告", "account_verification.json", "JSON (*.json)")
    if path:
        guarded(controller, lambda: Path(path).write_text(json.dumps(controller.external.acceptance_report(), ensure_ascii=False, indent=2), encoding="utf-8"))


def show_original_spec(controller):
    def show():
        path = reference_path("original_dom_spec.json")
        specification = json.loads(path.read_text(encoding="utf-8"))
        dialog = QDialog(controller)
        dialog.setWindowTitle("原版规格与待验证行为")
        dialog.resize(780, 580)
        layout = QVBoxLayout(dialog)
        text = QTextEdit()
        text.setReadOnly(True)
        rows = [f"来源：{specification['source_kind']}", "截图中的控件不代表其业务行为已验证。", ""]
        rows.extend(f"{item['control']}：{item['observed']}；{item['semantics'] or '业务含义待原版验证'}；状态 {item['status']}" for item in specification['observations'])
        rows.extend(["", "本项目当前规则：", *specification['application_rules']])
        text.setPlainText("\n\n".join(rows))
        layout.addWidget(text)
        dialog.exec()
    guarded(controller, show)


def show_availability(controller):
    dialog = QDialog(controller)
    dialog.setWindowTitle("版本与可用范围")
    dialog.resize(840, 460)
    layout = QVBoxLayout(dialog)
    table = QTableWidget(0, 3)
    table.setHorizontalHeaderLabels(("功能", "当前状态", "使用条件"))
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    rows = availability(controller)
    table.setRowCount(len(rows))
    for row, values in enumerate(rows):
        for column, value in enumerate(values):
            table.setItem(row, column, QTableWidgetItem(value))
    table.resizeColumnsToContents()
    layout.addWidget(table)
    close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    close.rejected.connect(dialog.reject)
    layout.addWidget(close)
    dialog.exec()


class RiskDialog(QDialog):
    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.connection = controller.external
        self.local_ledger = controller.ledger
        self.session = None if controller.paper else controller.external
        self.setWindowTitle("分组归属与风险限额")
        self.resize(740, 540)
        root = QVBoxLayout(self)
        root.addWidget(QLabel("外部未分配持仓计入账户总风险，明确分配后才能参与分组止损。\n日亏损包括浮动盈亏及费用，出入金调整后计算；达到限额仍允许合规减仓。" if self.session else "本地模拟按委托组别自动分配持仓；日盈亏包含按最后报价估算的浮动盈亏。\n不包含手续费或保证金，不是外部账户资金。"))
        form = QFormLayout()
        root.addLayout(form)
        limits = self.session.config.risk_limits() if self.session else controller.ledger.limits
        self.fields = {}
        for name, label in (("total", "账户总持仓上限（手）"), ("group", "每组持仓上限（手）"), ("cancels", "每日撤单请求上限"), ("loss", "日亏损上限（人民币）")):
            field = QLineEdit(str(getattr(limits, name)))
            self.fields[name] = field
            form.addRow(label, field)
        save = QPushButton("保存限额")
        save.clicked.connect(lambda: guarded(self, self.save))
        root.addWidget(save)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("持仓标识", "手数", "分组分配", "未分配"))
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        root.addWidget(self.table)
        row = QHBoxLayout()
        self.group = QSpinBox()
        self.group.setRange(1, 20)
        self.group.setValue(controller.group.value())
        self.quantity = QSpinBox()
        self.quantity.setRange(0, 100000)
        row.addWidget(QLabel("组"))
        row.addWidget(self.group)
        row.addWidget(QLabel("该组分配手数（替换原值）"))
        row.addWidget(self.quantity)
        assign = QPushButton("分配选中持仓")
        assign.setEnabled(bool(self.session))
        assign.clicked.connect(lambda: guarded(self, self.assign))
        row.addWidget(assign)
        root.addLayout(row)
        self.status = QLabel()
        root.addWidget(self.status)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    def save(self):
        limits = RiskLimits(*(int(self.fields[key].text()) for key in ("total", "group", "cancels")), self.fields["loss"].text())
        limits.validate()
        if self.controller.external is not self.connection or self.controller.ledger is not self.local_ledger:
            raise ValueError("连接已改变，请重新打开")
        self.controller.save_risk_limits(limits)
        if self.session:
            config = self.session.config
            config.max_total, config.max_group, config.max_cancels, config.max_daily_loss = limits.total, limits.group, limits.cancels, limits.loss
            self.session.risk.equity(self.session.accounts, self.session.trading_day, limits)
            self.session.persist()
        else:
            self.controller.ledger.limits = limits
        self.status.setText("限额已保存；实盘仍只读")

    def refresh(self):
        if self.controller.external is not self.connection or self.controller.ledger is not self.local_ledger:
            self.status.setText("连接已改变，请关闭后重新打开")
            return
        self.rows = list(self.session.positions) if self.session else []
        book = self.session.risk if self.session else self.controller.ledger.risk
        if not self.session:
            for identifier in book.allocations:
                exchange, symbol, direction, day, hedge = identifier.split("|")
                position = self.controller.ledger.positions.get((f"{exchange}.{symbol}", "买" if direction == "2" else "卖"))
                self.rows.append(dict(ExchangeID=exchange, InstrumentID=symbol, PosiDirection=direction, PositionDate=day, HedgeFlag=hedge, Position=position.quantity if position else 0))
        selected = self.table.currentRow()
        self.table.setRowCount(len(self.rows))
        for index, row in enumerate(self.rows):
            allocated = book.allocations.get(position_id(row), {})
            values = (position_id(row), row.get("Position", 0), ", ".join(f"组{key}:{value}" for key, value in allocated.items()), row.get("Position", 0) - sum(allocated.values()))
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(str(value)))
        if selected >= 0:
            self.table.selectRow(selected)
        self.table.resizeColumnsToContents()
        self.status.setText(f"日盈亏：{book.pnl if book.pnl is not None else '尚未核验'}；撤单占用：{book.cancel_count}" + ("；日亏损限额已触发" if book.loss_locked else ""))

    def assign(self):
        if self.controller.external is not self.connection or not self.session or self.table.currentRow() < 0:
            raise ValueError("请选择当前连接的持仓")
        self.session.assign_position(self.rows[self.table.currentRow()], self.group.value(), self.quantity.value())
        self.refresh()


class StopsDialog(QDialog):
    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.connection = controller.external
        self.local_ledger = controller.ledger
        self.session = None if controller.paper else controller.external
        self.manager = self.session.stops if self.session else controller.offline_stops.stops
        self.setWindowTitle("本地 / 服务端止损")
        self.resize(940, 620)
        root = QVBoxLayout(self)
        root.addWidget(QLabel("本地止损：最新价触发，先撤冲突平仓单并对账，再按对手价提交限价平仓，不自动追价。\n退出后本地止损失效；重启后须手动启用。服务端止损须实际托管服务支持并返回确认。"))
        form = QFormLayout()
        root.addLayout(form)
        self.key = QComboBox()
        self.key.setEditable(True)
        self.key.addItems(sorted(controller.catalog))
        self.side = QComboBox()
        self.side.addItems(("多仓", "空仓"))
        self.offset = QComboBox()
        self.offset.addItems(("平仓", "平今", "平昨") if self.session else ("平仓",))
        self.price = QLineEdit()
        self.quantity = QSpinBox()
        self.quantity.setRange(1, 100000)
        self.group = QSpinBox()
        self.group.setRange(1, 20)
        self.group.setValue(controller.group.value())
        self.mode = QComboBox()
        self.mode.addItem("本地止损", "local")
        self.mode.addItem("服务端托管", "server")
        for label, field in (("合约", self.key), ("持仓方向", self.side), ("平仓类型", self.offset), ("触发价格", self.price), ("数量", self.quantity), ("组别", self.group), ("止损模式", self.mode)):
            form.addRow(label, field)
        provider = self.manager.provider.capabilities()
        root.addWidget(QLabel("托管能力：" + ("服务已配置，仍须接口确认" if provider.get("supported") else provider.get("reason", "不支持"))))
        create = QPushButton("创建止损（初始暂停）")
        create.clicked.connect(lambda: guarded(self, self.create))
        root.addWidget(create)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(("标识", "合约", "持仓", "触发价", "数量", "组别", "模式", "状态", "说明"))
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        root.addWidget(self.table)
        actions = QHBoxLayout()
        for label, method in (("启用选中", "arm"), ("暂停选中", "pause"), ("撤销选中", "cancel")):
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, name=method: guarded(self, lambda: self.apply(name)))
            actions.addWidget(button)
        root.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    def current(self):
        if self.controller.external is not self.connection or self.controller.ledger is not self.local_ledger:
            raise ValueError("连接已改变，请重新打开止损窗口")

    def create(self):
        self.current()
        key = self.key.currentText()
        instrument = self.controller.catalog[key]
        self.manager.add(key, "买" if self.side.currentIndex() == 0 else "卖", self.offset.currentText(), instrument.ticks(self.price.text()), self.quantity.value(), self.group.value(), self.mode.currentData())
        self.refresh()

    def apply(self, method):
        self.current()
        row = self.table.currentRow()
        if row < 0:
            raise ValueError("请选择止损")
        identifier = self.table.item(row, 0).text()
        if self.session and method == "arm":
            stop = self.manager.stops[identifier]
            self.session.ensure_stop_subscription(stop)
            if not self.session.quote_fresh(stop.key):
                raise ValueError("已请求独立行情订阅；收到有效报价后再次启用")
        getattr(self.manager, method)(identifier)
        self.refresh()

    def refresh(self):
        selected = self.table.currentRow()
        self.table.setRowCount(len(self.manager.stops))
        for index, stop in enumerate(self.manager.stops.values()):
            instrument = self.controller.catalog.get(stop.key)
            price = instrument.price(stop.trigger) if instrument else str(stop.trigger)
            values = (stop.id, stop.key, "多" if stop.position_side == "买" else "空", price, stop.quantity, stop.group, "本地" if stop.mode == "local" else "服务端", stop.state, stop.message)
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(str(value)))
        if selected >= 0:
            self.table.selectRow(selected)
        self.table.resizeColumnsToContents()
