from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout


class VolumeItem(QTableWidgetItem):
    def __init__(self, value):
        super().__init__(str(value) if value else "—")
        self.value = value
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if value:
            self.setToolTip("实时成交量：由当前行情回报更新；离线为模拟值")
        else:
            self.setToolTip("尚无实时报价，不显示静态目录数据")

    def __lt__(self, other):
        if isinstance(other, VolumeItem):
            return self.value < other.value
        return super().__lt__(other)


class ContractSelector(QDialog):
    def __init__(self, controller):
        super().__init__(controller)
        self.controller = controller
        self.selected = set()
        self.sort_column = 0
        self.sort_descending = False
        self.setWindowTitle("选择合约")
        self.resize(840, 520)
        self.setModal(False)
        layout = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索合约代码或名称，例如 MA、白银")
        filters.addWidget(self.search, 1)
        self.exchange = QComboBox()
        self.exchange.addItem("全部交易所", "")
        for exchange in ("SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX"):
            self.exchange.addItem(exchange, exchange)
        filters.addWidget(self.exchange)
        self.category = QComboBox()
        self.category.addItems(("全部品种", "期货", "期权", "其他 / 未确认"))
        filters.addWidget(self.category)
        self.group = QSpinBox()
        self.group.setRange(1, 20)
        self.group.setPrefix("分组 ")
        filters.addWidget(self.group)
        layout.addLayout(filters)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(("选择", "合约", "名称", "成交量", "价位", "乘数", "品种"))
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        for column, width in enumerate((48, 180, 150, 80, 90, 70, 100)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().sectionClicked.connect(self.sort_requested)
        layout.addWidget(self.table, 1)
        self.hint = QLabel()
        layout.addWidget(self.hint)
        actions = QHBoxLayout()
        self.home_button = QPushButton("返回首页")
        self.home_button.clicked.connect(controller.connect_ctp)
        actions.addWidget(self.home_button)
        self.open_button = QPushButton("打开所选 (0)")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_selected)
        actions.addWidget(self.open_button)
        close = QPushButton("关闭列表")
        close.clicked.connect(self.reject)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.search.textChanged.connect(self.refresh)
        self.exchange.currentIndexChanged.connect(self.refresh)
        self.category.currentIndexChanged.connect(self.refresh)
        self.table.itemChanged.connect(self.selection_changed)
        self.table.cellDoubleClicked.connect(self.open_row)

    def volumes(self):
        controller = self.controller
        if controller.external:
            return {key: snapshot.volume for key, snapshot in controller.external.snapshots.items()}
        if controller.offline_mode:
            return {key: snapshot.volume for key, snapshot in controller.simulator.snapshots.items()}
        return {}

    def sort_requested(self, column):
        if column == 0:
            return
        if self.sort_column == column:
            self.sort_descending = not self.sort_descending
        else:
            self.sort_column, self.sort_descending = column, column in {3, 5}
        self.refresh()

    def prepare(self, reset=False):
        if reset:
            self.selected.clear()
            self.search.clear()
            self.exchange.setCurrentIndex(0)
        self.group.setValue(self.controller.group.value())
        self.refresh()
        self.show()
        self.raise_()
        self.search.setFocus()

    def refresh(self):
        query = self.search.text().strip().lower()
        exchange = self.exchange.currentData()
        category = self.category.currentText()
        volumes = self.volumes()
        today = date.today().strftime("%Y%m%d")
        def kind(item):
            return "期权" if item.product_class in {"2", "6"} else "期货" if item.product_class == "1" else "其他"
        items = [item for item in self.controller.catalog.values() if query in (item.key + item.name).lower() and (not exchange or item.key.startswith(exchange + ".")) and (category == "全部品种" or kind(item) == category) and (not self.controller.external or not item.expire_date or item.expire_date >= today)]
        if self.sort_column:
            def sort_key(item):
                if self.sort_column == 3:
                    return (volumes.get(item.key, 0), item.key)
                values = {2: item.name, 4: item.tick, 5: item.multiplier, 6: kind(item), 1: item.key}
                return (values.get(self.sort_column),)
            items.sort(key=sort_key, reverse=self.sort_descending)
        rows = items[:300]
        self.truncated = len(items) > 300
        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(rows))
            for row, instrument in enumerate(rows):
                check = QTableWidgetItem()
                check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
                check.setData(Qt.ItemDataRole.UserRole, instrument.key)
                check.setCheckState(Qt.CheckState.Checked if instrument.key in self.selected else Qt.CheckState.Unchecked)
                self.table.setItem(row, 0, check)
                volume = volumes.get(instrument.key, 0)
                for column, value in enumerate((instrument.key, instrument.name, VolumeItem(volume), instrument.tick, instrument.multiplier, kind(instrument)), 1):
                    self.table.setItem(row, column, value if isinstance(value, QTableWidgetItem) else QTableWidgetItem(str(value)))
        finally:
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)
        self.update_selection()

    def selection_changed(self, item):
        if item.column() != 0:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.selected.add(key)
        else:
            self.selected.discard(key)
        self.update_selection()

    def update_selection(self):
        self.selected.intersection_update(self.controller.catalog)
        count = len(self.selected)
        self.open_button.setText(f"打开所选 ({count})")
        self.open_button.setEnabled(0 < count <= 40)
        self.hint.setText(f"已选 {count} 个，最多同时打开40个。" + ("当前仅显示前300项，请搜索缩小范围。" if self.truncated else "双击合约可直接打开；点击表头排序。"))

    def open_selected(self):
        keys = sorted(self.selected.intersection(self.controller.catalog))
        if not 0 < len(keys) <= 40:
            return
        self.controller.group.setValue(self.group.value())
        for key in keys:
            self.controller.checked_contracts.add(key)
            self.controller.open_or_activate(key)
        self.controller.update_selection()
        self.accept()

    def open_row(self, row, column):
        if column == 0:
            return
        item = self.table.item(row, 0)
        if item:
            key = item.data(Qt.ItemDataRole.UserRole)
            if key in self.controller.catalog:
                self.controller.group.setValue(self.group.value())
                self.controller.checked_contracts.add(key)
                self.controller.open_or_activate(key)
                self.controller.update_selection()
                self.accept()
