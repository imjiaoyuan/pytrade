from decimal import Decimal

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .configuration import AccountStore, copy_instrument_table
from .domain import ACTIVE


def position_rows(controller):
    rows = []
    if controller.external and not controller.paper and controller.external.config.td_front:
        session = controller.external
        for row in session.positions:
            quantity = row.get("Position", 0)
            if not quantity:
                continue
            key = f"{row.get('ExchangeID')}.{row.get('InstrumentID')}"
            instrument = session.catalog.get(key)
            average = Decimal(str(row.get("OpenCost", 0))) / quantity / instrument.multiplier if instrument else Decimal("0")
            frozen = row.get("LongFrozen", 0) + row.get("ShortFrozen", 0)
            allocation = ", ".join(f"组{group}:{value}" for group, value in session.risk.allocations.get("|".join((row.get("ExchangeID", ""), row.get("InstrumentID", ""), row.get("PosiDirection", ""), row.get("PositionDate", ""), row.get("HedgeFlag", ""))), {}).items()) or "未分配"
            rows.append((key, {"2": "多", "3": "空"}.get(row.get("PosiDirection"), "净"), "今" if row.get("PositionDate") == "1" else "昨", quantity, frozen, f"{average:.4f}" if instrument else "—", str(row.get("PositionProfit", "—")), row.get("HedgeFlag", ""), allocation))
        return rows
    ledger = controller.ledger
    for (key, side), position in ledger.positions.items():
        if not position.quantity:
            continue
        instrument = ledger.catalog[key]
        average = Decimal(position.cost_ticks) / position.quantity
        last = ledger.marks.get(key, instrument.initial_ticks)
        pnl = (last * position.quantity - position.cost_ticks) * instrument.tick * instrument.multiplier * (1 if side == "买" else -1)
        allocation = ", ".join(f"组{group}:{value}" for identifier, groups in ledger.risk.allocations.items() for group, value in groups.items() if identifier.startswith(key + "|") and identifier.split("|")[2] == ("2" if side == "买" else "3")) or "—"
        rows.append((key, "多" if side == "买" else "空", "—", position.quantity, 0, f"{average * instrument.tick:.4f}", f"{pnl:.2f}", "1", allocation))
    return rows


class PositionWindow(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.setWindowTitle("持仓")
        self.resize(980, 480)
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        self.title = QLabel("模拟持仓 · 仅本次会话")
        top.addWidget(self.title)
        top.addStretch(1)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh)
        top.addWidget(self.refresh_button)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        top.addWidget(self.close_button)
        layout.addLayout(top)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(("合约", "方向", "今/昨", "持仓", "冻结", "均价", "盈亏", "套保", "分组"))
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)
        self.hint = QLabel("参考原版 PositionWin 外观；盈亏口径见列头。外部账户持仓由接口查询驱动。")
        self.hint.setStyleSheet("color:#555")
        layout.addWidget(self.hint)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    def refresh(self):
        if self.controller.shutting_down:
            return
        rows = position_rows(self.controller)
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self.table.setUpdatesEnabled(True)
        if self.controller.paper:
            self.title.setText(f"在线模拟持仓 · {len(rows)} 条 · 不含手续费/保证金")
        elif self.controller.external and self.controller.external.config.td_front:
            self.title.setText(f"接口持仓 · {len(rows)} 条 · {self.controller.external.td_status}")
        elif self.controller.external:
            self.title.setText("仅看行情 · 未连接交易账户，无持仓查询")
        elif self.controller.offline_mode:
            self.title.setText(f"离线模拟持仓 · {len(rows)} 条 · 仅本次会话")
        else:
            self.title.setText("已断开 · 显示断开前记录")


class CloseRecordWindow(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.setWindowTitle("平仓记录")
        self.resize(900, 460)
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        self.title = QLabel("平仓记录（本地模拟）")
        top.addWidget(self.title)
        top.addStretch(1)
        export = QPushButton("导出 CSV")
        export.clicked.connect(self.export)
        top.addWidget(export)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        top.addWidget(self.close_button)
        layout.addLayout(top)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(("合约", "方向", "平仓类型", "数量", "开仓均价", "平仓价", "盈亏", "交易日", "委托号"))
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)
        self.hint = QLabel("参考原版 TCloseRecordWin 外观；外部账户成交以接口查询为准。")
        self.hint.setStyleSheet("color:#555")
        layout.addWidget(self.hint)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    def records(self):
        rows = []
        ledger = self.controller.ledger
        opened = {}
        for fill in ledger.fills:
            instrument = ledger.catalog.get(fill.key)
            if not instrument:
                continue
            if fill.offset == "开仓":
                opened.setdefault(fill.key, []).append((fill.ticks, fill.quantity))
                continue
            remaining = fill.quantity
            profits = Decimal("0")
            cost_ticks = 0
            while remaining > 0 and opened.get(fill.key):
                ticks, quantity = opened[fill.key][0]
                used = min(remaining, quantity)
                direction = 1 if fill.side == "卖" else -1
                profits += (Decimal(fill.ticks) - Decimal(ticks)) * instrument.tick * instrument.multiplier * used * direction
                cost_ticks = ticks
                remaining -= used
                if used == quantity:
                    opened[fill.key].pop(0)
                else:
                    opened[fill.key][0] = (ticks, quantity - used)
            if remaining:
                continue
            rows.append((fill.key, "买" if fill.side == "买" else "卖", fill.offset, fill.quantity, instrument.price(cost_ticks), instrument.price(fill.ticks), f"{profits:.2f}", ledger.risk_day or "—", fill.order_id))
        return rows

    def refresh(self):
        if self.controller.shutting_down:
            return
        rows = self.records()
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self.table.setUpdatesEnabled(True)
        total = sum(Decimal(row[6]) for row in rows) if rows else Decimal("0")
        mode = "在线模拟" if self.controller.paper else ("外部账户" if self.controller.external and self.controller.external.config.td_front else "离线模拟")
        self.title.setText(f"平仓记录（{mode}）· {len(rows)} 笔 · 合计盈亏 {total:.2f}")

    def export(self):
        from .records import export_csv
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(self, "导出平仓记录", "close_records.csv", "CSV (*.csv)")
        if not path:
            return
        headers = [self.table.horizontalHeaderItem(column).text() for column in range(self.table.columnCount())]
        rows = [[self.table.item(row, column).text() if self.table.item(row, column) else "" for column in range(self.table.columnCount())] for row in range(self.table.rowCount())]
        try:
            export_csv(path, headers, rows)
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "导出失败", str(error))


class GroupControlBar(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.setWindowTitle("分组控制")
        self.cancel_shortcut = QShortcut(QKeySequence("Space"), self)
        self.cancel_shortcut.setAutoRepeat(False)
        self.cancel_shortcut.activated.connect(controller.cancel_all_orders)
        self.setFixedHeight(96)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)
        self.buttons = []
        rows = (QHBoxLayout(), QHBoxLayout())
        for row in rows:
            row.setSpacing(1)
            layout.addLayout(row)
        for group in range(1, 21):
            button = QPushButton(str(group))
            button.setCheckable(True)
            button.setFixedSize(30, 24)
            shortcut = f"F{group}" if group <= 10 else f"Shift+F{group - 10}"
            button.setToolTip(f"显示第 {group} 组窗口（{shortcut}）")
            button.clicked.connect(lambda checked=False, target=group: controller.activate_group(target))
            rows[0 if group <= 10 else 1].addWidget(button)
            self.buttons.append(button)
        actions = QHBoxLayout()
        actions.setSpacing(2)
        for label, tip, action in (("全关", "关闭全部 DOM 窗口（不撤销外部委托）", controller.close_all_doms), ("关组", "关闭当前组的 DOM 窗口", controller.close_group_doms), ("撤组", "撤销当前组的本地模拟挂单", controller.cancel_group_orders), ("持仓", "打开独立持仓窗口", controller.show_positions_window), ("平仓", "打开平仓记录窗口", controller.show_close_records_window), ("排列", "排列当前可见窗口", controller.align_windows), ("隐藏", "隐藏控制条（可从工作台再次打开）", self.hide)):
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(action)
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.adjust_buttons()

    def adjust_buttons(self):
        current = self.controller.group.value()
        for index, button in enumerate(self.buttons, start=1):
            count = sum(1 for window in self.controller.windows if window.group.value() == index)
            button.setText(str(index) if not count else f"{index}·{count}")
            button.setChecked(index == current)

    def showEvent(self, event):
        super().showEvent(event)
        self.adjust_buttons()
