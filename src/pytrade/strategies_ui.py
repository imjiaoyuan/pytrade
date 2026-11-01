from decimal import Decimal

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout

from .local_algos import calculate_spread


class StrategiesDialog(QDialog):
    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.setWindowTitle("本地模拟 · 策略与双腿价差")
        self.resize(940, 620)
        root = QVBoxLayout(self)
        notice = QLabel("仅本地模拟，不发送外部委托。双腿分别成交，可能留有未对冲持仓；停止策略后须核对挂单和持仓。退出后不恢复。")
        notice.setWordWrap(True)
        root.addWidget(notice)
        form = QFormLayout()
        self.kind = QComboBox()
        for label, value in (("最优价挂单", "best"), ("狙击限价", "sniper"), ("条件触发", "stop"), ("双腿价差 A − 系数 × B", "spread")):
            self.kind.addItem(label, value)
        form.addRow("策略", self.kind)
        self.key, self.second = QComboBox(), QComboBox()
        for key, instrument in controller.catalog.items():
            if instrument.product_class == "1":
                self.key.addItem(f"{key} {instrument.name}", key)
                self.second.addItem(f"{key} {instrument.name}", key)
        self.second.setCurrentIndex(min(1, self.second.count() - 1))
        form.addRow("合约 / 腿 A", self.key)
        form.addRow("腿 B（先打开两腿 DOM 收行情）", self.second)
        self.side = QComboBox()
        self.side.addItems(("买", "卖"))
        self.offset = QComboBox()
        self.offset.addItems(("开仓", "平仓"))
        form.addRow("方向", self.side)
        form.addRow("开平（价差平仓须两腿均有可平仓）", self.offset)
        self.quantity = QSpinBox()
        self.quantity.setRange(1, controller.ledger.max_order)
        form.addRow("总手数 / 价差份数", self.quantity)
        self.group = QSpinBox()
        self.group.setRange(1, 20)
        self.group.setValue(controller.group.value())
        form.addRow("分组", self.group)
        self.limit = QLineEdit()
        form.addRow("限价 / 条件触发价 / 价差阈值", self.limit)
        self.factor = QLineEdit("1")
        form.addRow("腿 B 价格系数", self.factor)
        self.ratio_a, self.ratio_b = QSpinBox(), QSpinBox()
        for widget in (self.ratio_a, self.ratio_b):
            widget.setRange(1, controller.ledger.max_order)
        form.addRow("每份腿 A 手数", self.ratio_a)
        form.addRow("每份腿 B 手数（方向与 A 相反）", self.ratio_b)
        root.addLayout(form)
        self.description = QLabel()
        self.description.setWordWrap(True)
        root.addWidget(self.description)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        root.addWidget(self.preview)
        actions = QHBoxLayout()
        start = QPushButton("启动本地模拟策略")
        start.clicked.connect(self.start_task)
        actions.addWidget(start)
        stop = QPushButton("停止所选策略并撤其挂单")
        stop.clicked.connect(self.stop_task)
        actions.addWidget(stop)
        open_legs = QPushButton("打开所选腿的 DOM")
        open_legs.clicked.connect(self.open_legs)
        actions.addWidget(open_legs)
        root.addLayout(actions)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(("编号", "策略", "合约", "方向/组", "状态", "进度 / 残腿"))
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self.table, 1)
        self.kind.currentIndexChanged.connect(self.update_kind)
        self.key.currentIndexChanged.connect(self.update_limit)
        self.update_kind()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)

    def update_kind(self):
        spread = self.kind.currentData() == "spread"
        for widget in (self.second, self.factor, self.ratio_a, self.ratio_b):
            widget.setEnabled(spread)
        self.offset.setEnabled(True)
        descriptions = {
            "best": "跟随同侧最优价，在策略限价以内挂单；价格变化先撤，取得本地撤单回报后等待新报价重挂。",
            "sniper": "对手价进入策略限价时，按可见一档数量尝试交易；未成挂单在后续新报价撤销。",
            "stop": "买入在最新价达到或高于触发价、卖出在达到或低于触发价时，按对手价挂普通限价单；仅触发一次，不自动追价。",
            "spread": "买价差：买 A、卖 B；卖价差相反。开仓或平仓须明确选择，平仓先核对两腿可平数量。达到阈值后先挂 A，按 A 已成交整份数量补 B，限价未成交不自动追价。价格系数与交易手数比例分别设置。",
        }
        self.description.setText(descriptions[self.kind.currentData()])
        self.update_limit()
        self.refresh()

    def update_limit(self):
        if self.kind.currentData() == "spread":
            self.limit.setText("0")
            return
        key = self.key.currentData()
        if key:
            try:
                quote = self.controller.local_algos.quote(key)
                self.limit.setText(self.controller.catalog[key].price(quote.last))
            except ValueError:
                self.limit.setText("")

    def open_legs(self):
        keys = (self.key.currentData(), self.second.currentData()) if self.kind.currentData() == "spread" else (self.key.currentData(),)
        for key in set(keys):
            if key and not any(window.instrument.key == key for window in self.controller.windows):
                self.controller.open_dom(key, self.group.value())

    def start_task(self):
        try:
            task = self.controller.local_algos.start(self.kind.currentData(), self.key.currentData(), self.side.currentText(), self.offset.currentText(), self.quantity.value(), Decimal(self.limit.text()), self.group.value(), self.second.currentData(), Decimal(self.factor.text()), self.ratio_a.value(), self.ratio_b.value())
            self.controller.log(f"{task.id} 本地策略已启动，不发送外部委托")
        except (ValueError, ArithmeticError) as error:
            QMessageBox.warning(self, "策略未启动", str(error))
        self.refresh()

    def stop_task(self):
        row = self.table.currentRow()
        if row >= 0:
            self.controller.local_algos.stop(self.table.item(row, 0).text())
            self.controller.refresh_records()
            self.refresh()

    def refresh(self):
        if self.kind.currentData() == "spread":
            try:
                first, second = (self.controller.local_algos.quote(key) for key in (self.key.currentData(), self.second.currentData()))
                quote = calculate_spread(first, second, self.controller.catalog[first.key].tick, self.controller.catalog[second.key].tick, Decimal(self.factor.text()), self.ratio_a.value(), self.ratio_b.value())
                self.preview.setText(f"合成盘口：买 {quote.bid} × {quote.bid_volume}份 / 卖 {quote.ask} × {quote.ask_volume}份")
            except (ValueError, ArithmeticError) as error:
                self.preview.setText(str(error))
        else:
            self.preview.setText("策略按新的有效行情推进，休市或无新报价时不会自行成交。")
        tasks = list(self.controller.local_algos.tasks.values())
        selected = self.table.currentRow()
        self.table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            filled = sum(task.ledger.orders[identifier].filled for identifier in task.orders)
            hedged = sum(task.ledger.orders[identifier].filled for identifier in task.hedge_orders)
            progress = f"主单成交 {filled} / 补腿成交 {hedged}；{task.message}"
            name = self.kind.itemText(self.kind.findData(task.kind))
            for column, value in enumerate((task.id, name, " / ".join(task.keys), f"{task.side}{task.offset} / {task.group}", task.status, progress)):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        if selected >= 0:
            self.table.selectRow(selected)

    def showEvent(self, event):
        self.timer.start(500)
        self.refresh()
        super().showEvent(event)

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)
