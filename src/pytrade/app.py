import argparse
import json
import logging
import os
import sys
import time
from itertools import islice
from decimal import Decimal
from datetime import date, datetime
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QSignalBlocker, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QSizePolicy, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget

from .dom_ui import DomWindow
from .domain import ACTIVE, CATALOG, INSTRUMENTS, Ledger
from .simulation import Simulator
from .sounds import SoundTracker, TradeSounds
from .connection_ui import ConnectionDialog
from .external import ExternalSession, OFFSET_NAMES, ORDER_STATES, load_instruments, quote_ticks
from .paper import PaperSimulator
from .gateway import CtpGateway
from .resources import application_root, reference_path
from .version import VERSION
from .configuration import ConfigStore
from .trial import TrialState, current_trial_state, enforce_trial_at_startup
from .offline_stops import OfflineStops
from .records import configure_logging, transaction_paths
from .tools_ui import RiskDialog, StopsDialog, display_settings, export_acceptance, export_records, import_configuration, show_availability, show_original_spec
from .windows_ui import CloseRecordWindow, GroupControlBar, PositionWindow


class Controller(QMainWindow):
    log_message = Signal(str)
    connection_requested = Signal()

    def __init__(self, state_dir, restore_windows=False):
        super().__init__()
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config_store = ConfigStore(state_dir)
        self.layout_path = state_dir / "layout.json"
        self.ledger = Ledger()
        self.sound_tracker = SoundTracker()
        self.sound_tracker.observe(self.ledger, {}, set(), audible=False)
        self.sounds = TradeSounds(self, self.log)
        self.simulator = Simulator(self.ledger)
        self.offline_stops = OfflineStops(self)
        self.ledger.stop_manager = self.offline_stops.stops
        self.catalog = dict(CATALOG)
        self.external = None
        self.paper = None
        self.offline_mode = True
        self.pending_offline = False
        self.gateway = None
        self.last_error = ""
        self.persistence_error = ""
        self.exit_pending = False
        self.windows = []
        self.contract_selector = None
        self.position_window = None
        self.close_record_window = None
        self.group_bar = None
        self.shutting_down = False
        self.paused = False
        from .local_algos import LocalAlgoEngine
        self.local_algos = LocalAlgoEngine(self)
        self.strategies_dialog = None
        self.checked_contracts = set()
        self.details_visible = True
        self.expanded_size = None
        self.use_startup_login = False
        self.setWindowTitle("意意交易 · 合约与交易控制")
        self.resize(840, 430)
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(5, 5, 5, 5)
        root.setSpacing(4)
        self.state = QLabel("离线模拟 · 行情、委托和成交均为本地模拟")
        self.state.setStyleSheet("background:#263238;color:#ffe18a;padding:4px")
        self.state.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.state.setFixedHeight(self.state.fontMetrics().height() + 8)
        root.addWidget(self.state)
        connection_menu = self.menuBar().addMenu("连接 / 账户")
        choose_contracts = connection_menu.addAction("选择合约")
        choose_contracts.triggered.connect(lambda: self.select_contracts())
        positions_menu = connection_menu.addAction("独立持仓窗口")
        positions_menu.triggered.connect(self.show_positions_window)
        records_menu = connection_menu.addAction("平仓记录窗口")
        records_menu.triggered.connect(self.show_close_records_window)
        accounts_menu = connection_menu.addAction("账号管理…")
        accounts_menu.triggered.connect(self.open_account_manager)
        group_bar_menu = connection_menu.addAction("分组控制条")
        group_bar_menu.setCheckable(True)
        group_bar_menu.toggled.connect(lambda checked: self.show_group_bar(checked))
        self.group_bar_menu = group_bar_menu
        for title, action in (("返回首页 / 切换模式", self.connect_ctp), ("断开 CTP", self.disconnect_ctp), ("切换到离线模拟", self.enter_offline), ("查询对账", self.query_external), ("结算单 / 确认", self.settlement_dialog), ("退出", self.close)):
            item = QAction(title, self)
            item.triggered.connect(action)
            connection_menu.addAction(item)
        settings_menu = self.menuBar().addMenu("配置 / 工具")
        strategies_action = settings_menu.addAction("本地策略 / 双腿价差")
        strategies_action.triggered.connect(lambda: self.show_strategies())
        sound_menu = settings_menu.addMenu("交易音效")
        enabled = sound_menu.addAction("启用音效")
        enabled.setCheckable(True)
        enabled.setChecked(True)
        enabled.toggled.connect(lambda value: setattr(self.sounds, "enabled", value))
        same_sound = sound_menu.addAction("挂单与撤单使用同一音效")
        same_sound.setCheckable(True)
        same_sound.toggled.connect(lambda value: setattr(self.sounds, "same_order_cancel", value))
        for cue, label in (("accepted", "试听挂单"), ("fill", "试听成交（开仓 / 平仓相同）"), ("canceled", "试听撤单")):
            item = sound_menu.addAction(label)
            item.triggered.connect(lambda checked=False, cue=cue: self.sounds.play(cue))
        for title, action in (("显示设置", lambda: display_settings(self)), ("导入原配置", lambda: import_configuration(self)), ("刷新合约元数据", self.refresh_metadata), ("分组 / 风控", lambda: RiskDialog(self).exec()), ("本地 / 服务端止损", lambda: StopsDialog(self).exec()), ("筛选 / 导出记录", lambda: export_records(self)), ("导出联调报告", lambda: export_acceptance(self)), ("原版规格 / 待验证", lambda: show_original_spec(self)), ("版本 / 可用范围", lambda: show_availability(self))):
            item = QAction(title, self)
            item.triggered.connect(action)
            settings_menu.addAction(item)
        tools = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索合约 / 名称")
        self.search.textChanged.connect(self.filter_contracts)
        tools.addWidget(self.search)
        self.exchange = QComboBox()
        self.exchange.addItem("全部交易所", "")
        for exchange in ("SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX"):
            self.exchange.addItem(exchange, exchange)
        self.exchange.currentIndexChanged.connect(self.filter_contracts)
        tools.addWidget(self.exchange)
        self.category = QComboBox()
        self.category.addItems(("全部品种", "期货", "股指期货", "期权", "其他 / 未确认"))
        self.category.currentIndexChanged.connect(self.filter_contracts)
        tools.addWidget(self.category)
        self.group = QSpinBox()
        self.group.setRange(1, 20)
        self.group.setPrefix("分组 ")
        self.group.setMaximumWidth(100)
        self.open_button = QPushButton("打开所选 (0)")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_selected)
        tools.addWidget(self.open_button)
        root.addLayout(tools)
        actions = QHBoxLayout()
        self.home_button = QPushButton("返回首页")
        self.home_button.clicked.connect(self.connect_ctp)
        actions.addWidget(self.home_button)
        self.details_button = QPushButton("收起合约 / 记录")
        self.details_button.clicked.connect(lambda: self.set_details_visible(not self.details_visible))
        actions.addWidget(self.details_button)
        actions.addWidget(self.group)
        for title, action in (("排列窗口", self.align_windows), ("切组", self.switch_group), ("保存布局", self.save_layout), ("恢复布局", self.restore_and_arrange), ("暂停模拟", self.toggle_pause)):
            button = QPushButton(title)
            button.clicked.connect(action)
            actions.addWidget(button)
            if title == "暂停模拟":
                self.pause_button = button
        root.addLayout(actions)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)
        self.contracts = self.make_table(("选择", "合约", "名称", "最小价位", "乘数", "状态"))
        self.tabs.addTab(self.contracts, "合约")
        self.contracts.cellDoubleClicked.connect(self.open_contract_row)
        self.contracts.itemChanged.connect(self.contract_checked)
        self.filter_contracts()
        self.orders = self.make_table(("委托号", "合约", "方向", "开平", "限价", "手数", "已成", "状态", "说明", "组别"))
        orders_page = QWidget()
        orders_layout = QVBoxLayout(orders_page)
        orders_layout.setContentsMargins(0, 0, 0, 0)
        orders_layout.addWidget(self.orders)
        cancel = QPushButton("撤销选中委托")
        cancel.clicked.connect(self.cancel_selected)
        orders_layout.addWidget(cancel)
        self.tabs.addTab(orders_page, "委托")
        self.positions = self.make_table(("合约", "方向", "持仓", "均价", "浮动盈亏"))
        self.tabs.addTab(self.positions, "持仓")
        self.trades = self.make_table(("成交号", "委托号", "合约", "方向", "开平", "价格", "数量"))
        self.tabs.addTab(self.trades, "成交")
        self.funds = QTextEdit()
        self.funds.setReadOnly(True)
        self.tabs.addTab(self.funds, "资金 / 连接")
        self.logs = QTextEdit()
        self.logs.setReadOnly(True)
        self.logs.document().setMaximumBlockCount(500)
        self.tabs.addTab(self.logs, "运行记录")
        self.group_shortcuts = []
        for group in range(1, 11):
            shortcut = QShortcut(QKeySequence(f"F{group}"), self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(lambda target=group: self.activate_group(target))
            self.group_shortcuts.append(shortcut)
        self.selection_hint = QLabel("勾选合约后点击“打开所选”，或双击合约直接打开")
        root.addWidget(self.selection_hint)
        self.statusBar().showMessage("所有报价、成交与盈亏均为本地模拟；原版未确认选项已禁用")
        if restore_windows:
            self.restore_layout()
        self.timer = QTimer(self)
        self.trial_state = current_trial_state(self.state_dir)
        self.update_trial_title()
        self.timer.timeout.connect(self.advance)
        self.timer.start(500)
        self.advance()
        self.log("离线原型已启动；合约与价格参数仅用于演示，不代表当前有效合约")
        if self.config_store.error:
            self.log(self.config_store.error)

    def make_table(self, headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(22)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        widths = {"选择": 48, "合约": 135, "名称": 120, "最小价位": 80, "乘数": 55, "状态": 180, "委托号": 220, "成交号": 220, "说明": 250}
        for column, title in enumerate(headers):
            table.setColumnWidth(column, widths.get(title, 95))
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def populate(self, table, rows):
        values = [tuple(str(value) for value in row) for row in rows]
        if getattr(table, "_displayed_rows", None) == values:
            return
        selected = table.currentRow()
        identity = tuple(table.item(selected, column).text() for column in range(min(2, table.columnCount()))) if 0 <= selected < table.rowCount() and table.item(selected, 0) else None
        blocker = QSignalBlocker(table)
        table.setUpdatesEnabled(False)
        try:
            table.setRowCount(len(values))
            for row, cells in enumerate(values):
                for column, value in enumerate(cells):
                    item = table.item(row, column)
                    if item is None:
                        table.setItem(row, column, QTableWidgetItem(value))
                    elif item.text() != value:
                        item.setText(value)
            if identity is not None:
                matched = next((row for row, cells in enumerate(values) if cells[:len(identity)] == identity), None)
                if matched is None:
                    table.clearSelection()
                    table.setCurrentCell(-1, -1)
                else:
                    table.selectRow(matched)
            table._displayed_rows = values
        finally:
            table.setUpdatesEnabled(True)
            blocker.unblock()

    def filter_contracts(self):
        query = self.search.text().strip().lower()
        exchange = self.exchange.currentData()
        items = self.catalog.values()
        if self.external:
            today = date.today().strftime("%Y%m%d")
            items = (item for item in items if not item.expire_date or item.expire_date >= today)
        def kind(item):
            if item.product_class in {"2", "6"}:
                return "期权"
            if item.product_class == "1":
                return "股指期货" if item.key.startswith(("CFFEX.IF", "CFFEX.IH", "CFFEX.IC", "CFFEX.IM")) else "期货"
            return "其他 / 未确认"
        category = self.category.currentText()
        items = list(islice((i for i in items if query in (i.key + i.name).lower() and (not exchange or i.key.startswith(exchange + ".")) and (category == "全部品种" or kind(i) == category)), 300))
        self.contracts.blockSignals(True)
        self.contracts.setRowCount(len(items))
        opened = {window.instrument.key for window in self.windows}
        for row, instrument in enumerate(items):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            check.setData(Qt.ItemDataRole.UserRole, instrument.key)
            check.setCheckState(Qt.CheckState.Checked if instrument.key in self.checked_contracts else Qt.CheckState.Unchecked)
            self.contracts.setItem(row, 0, check)
            status = "接口已核验期货" if self.external and instrument.key in self.external.verified else "接口元数据 · 交易未启用" if instrument.source == "ctp-query" else "历史参考，仅行情" if self.external else "离线模拟"
            if self.paper:
                status = "历史参数 · 在线模拟（本地）" if instrument.product_class == "1" else "仅行情 · 在线模拟暂不支持此品种"
            if not self.external and not self.offline_mode:
                status = "已断开 · 仅元数据"
            status += f" · {kind(instrument)}"
            check.setToolTip(f"来源：{instrument.source}\n{'快照文件时间' if instrument.source == 'historical' else '获取时间'}：{instrument.fetched_at or '离线内置'}\n交易日：{instrument.trading_day or '未核验'}\n到期：{instrument.expire_date or '未提供'}")
            if instrument.key in opened:
                status += " · 已开窗"
            for column, value in enumerate((instrument.key, instrument.name, instrument.tick, instrument.multiplier, status), 1):
                self.contracts.setItem(row, column, QTableWidgetItem(str(value)))
        self.contracts.blockSignals(False)
        self.update_selection()

    def contract_checked(self, item):
        if item.column() != 0:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.checked_contracts.add(key)
        else:
            self.checked_contracts.discard(key)
        self.update_selection()

    def update_selection(self):
        count = len(self.checked_contracts)
        self.open_button.setText(f"打开所选 ({count})")
        self.open_button.setEnabled(count > 0)
        if hasattr(self, "selection_hint"):
            self.selection_hint.setText(f"已选 {count} 个 · 已开 {len(self.windows)} 个窗口 · 勾选后打开，双击可直接打开")

    def open_selected(self):
        if not self.checked_contracts:
            return
        if len(self.checked_contracts) > 40:
            self.selection_hint.setText("单次最多打开 40 个合约")
            return
        for key in sorted(self.checked_contracts):
            self.open_or_activate(key)
        self.update_selection()

    def open_or_activate(self, key):
        existing = next((w for w in self.windows if w.instrument.key == key and w.group.value() == self.group.value()), None)
        if existing:
            existing.show()
            existing.raise_()
            return existing
        return self.open_dom(key, self.group.value())

    def open_contract_row(self, row, column=1):
        if column == 0:
            return
        item = self.contracts.item(row, 1)
        if item:
            key = item.text()
            self.checked_contracts.add(key)
            self.open_or_activate(key)
            self.update_contract_window_state(key)

    def select_contracts(self, reset=False):
        self.set_details_visible(True)
        self.tabs.setCurrentIndex(0)
        if self.contract_selector is None:
            from .contract_selector import ContractSelector
            self.contract_selector = ContractSelector(self)
        self.contract_selector.prepare(reset=reset)

    def set_details_visible(self, visible):
        if visible == self.details_visible:
            return
        if not visible:
            self.expanded_size = self.size()
        self.details_visible = visible
        self.tabs.setVisible(visible)
        self.selection_hint.setVisible(visible)
        self.search.setVisible(visible)
        self.exchange.setVisible(visible)
        self.category.setVisible(visible)
        self.open_button.setVisible(visible)
        self.details_button.setText("收起合约 / 记录" if visible else "展开合约 / 记录")
        self.centralWidget().layout().activate()
        if visible:
            if self.expanded_size:
                self.resize(self.expanded_size)
        else:
            self.centralWidget().layout().invalidate()
            self.centralWidget().layout().activate()
            self.layout().invalidate()
            self.layout().activate()
            self.resize(self.width(), 1)

    def restore_and_arrange(self):
        self.restore_layout()
        if self.windows:
            self.align_windows()

    def dom_closed(self, key):
        if not any(w.instrument.key == key for w in self.windows):
            self.checked_contracts.discard(key)
        self.update_contract_window_state(key)

    def update_contract_window_state(self, key):
        opened = any(window.instrument.key == key for window in self.windows)
        blocker = QSignalBlocker(self.contracts)
        for row in range(self.contracts.rowCount()):
            item = self.contracts.item(row, 1)
            if item is None or item.text() != key:
                continue
            check = self.contracts.item(row, 0)
            check.setCheckState(Qt.CheckState.Checked if key in self.checked_contracts else Qt.CheckState.Unchecked)
            status = self.contracts.item(row, 5)
            text = status.text().removesuffix(" · 已开窗")
            status.setText(text + " · 已开窗" if opened else text)
        blocker.unblock()
        self.update_selection()

    def open_dom(self, key, group=1, geometry=None):
        window = DomWindow(self, key, group)
        self.windows.append(window)
        if geometry:
            self.apply_geometry(window, geometry)
        else:
            screen = self.screen().availableGeometry()
            window.resize(min(window.width(), screen.width()), min(window.height(), max(window.minimumHeight(), screen.height() - 80)))
            x = min(screen.x() + 20 + (len(self.windows) - 1) * 38, screen.right() - window.width() + 1)
            y = min(screen.y() + 40 + (len(self.windows) - 1) * 24, screen.bottom() - window.height() - 40)
            window.move(max(screen.left(), x), max(screen.top(), y))
        if self.external:
            self.external.retain(key)
            window.refresh_external(self.external.snapshots.get(key))
        elif not self.offline_mode:
            window.refresh_disconnected()
        elif key in self.simulator.snapshots:
            window.refresh(self.simulator.snapshots[key])
        self.apply_dom_config(window)
        window.show()
        self.update_contract_window_state(key)
        self.refresh_records()
        return window

    def align_windows(self):
        screen = self.screen().availableGeometry()
        visible = [w for w in self.windows if w.isVisible()]
        if not visible:
            return
        top = screen.top() + 6
        below = self.frameGeometry().bottom() + 7
        if screen.bottom() - below >= visible[0].minimumHeight() + 40:
            top = below
        columns = max(1, (screen.width() - 12) // (visible[0].frameGeometry().width() + 4))
        for index, window in enumerate(visible):
            row, column = divmod(index, columns)
            frame_extra = window.frameGeometry().height() - window.height()
            height = min(self.config_store.effective().dom_height, max(window.minimumHeight(), screen.bottom() - top - frame_extra - 8))
            window.resize(window.width(), height)
            x = screen.x() + 6 + column * (window.frameGeometry().width() + 4)
            y = top + min(row * 24, max(0, screen.bottom() - top - window.frameGeometry().height()))
            window.move(x, y)
            window.raise_()

    def switch_group(self):
        for window in self.windows:
            window.setVisible(window.group.value() == self.group.value())

    def activate_group(self, group):
        if not self.isVisible() or QApplication.activeModalWidget():
            return
        self.group.setValue(group)
        self.switch_group()
        if self.group_bar:
            self.group_bar.adjust_buttons()
        visible = next((window for window in self.windows if window.isVisible()), None)
        if visible:
            visible.raise_()
            visible.activateWindow()

    def show_positions_window(self):
        if self.position_window is None:
            self.position_window = PositionWindow(self)
        self.position_window.show()
        self.position_window.raise_()

    def show_close_records_window(self):
        if self.close_record_window is None:
            self.close_record_window = CloseRecordWindow(self)
        self.close_record_window.show()
        self.close_record_window.raise_()

    def show_group_bar(self, visible=True):
        if visible and self.group_bar is None:
            self.group_bar = GroupControlBar(self)
            self.group_bar.show()
        elif self.group_bar:
            self.group_bar.setVisible(visible)
            if visible:
                self.group_bar.raise_()

    def close_all_doms(self):
        for window in list(self.windows):
            window.close()
        self.log("已关闭全部 DOM 窗口；未撤销外部委托")

    def close_group_doms(self):
        group = self.group.value()
        closed = [window for window in self.windows if window.group.value() == group]
        for window in closed:
            window.close()
        self.log(f"已关闭第 {group} 组的 {len(closed)} 个 DOM 窗口；未撤销外部委托")

    def cancel_group_orders(self):
        group = self.group.value()
        if self.external and not self.paper:
            self.log("外部交易不绑定批量撤单；请在委托列表逐笔选择撤单")
            return
        if not self.offline_mode and not self.paper:
            self.log("连接已断开，请重连后查询外部委托")
            return
        count = 0
        for order in list(self.ledger.orders.values()):
            if order.group == group and order.status in ACTIVE:
                self.cancel_one(order.id)
                count += 1
        self.log(f"已撤销第 {group} 组本地模拟挂单 {count} 笔")

    def open_account_manager(self):
        from .accounts_ui import AccountManagerDialog
        dialog = AccountManagerDialog(self, self.state_dir)
        dialog.exec()

    def show_strategies(self, key=None):
        from .strategies_ui import StrategiesDialog
        if self.strategies_dialog is None:
            self.strategies_dialog = StrategiesDialog(self)
        if key:
            self.strategies_dialog.key.setCurrentIndex(self.strategies_dialog.key.findData(key))
        self.strategies_dialog.show()
        self.strategies_dialog.raise_()

    def toggle_pause(self):
        if self.paper:
            self.paused = not self.paused
            self.paper.paused = self.paused
            self.pause_button.setText("继续撮合" if self.paused else "暂停撮合")
            return
        if self.external:
            self.log("真实行情模式持续接收；请使用断开 CTP 结束连接")
            return
        self.paused = not self.paused
        self.pause_button.setText("继续行情" if self.paused else "暂停行情")
        self.state.setText("离线模拟 · 已暂停行情与撮合" if self.paused else "离线模拟 · 行情、委托和成交均为本地模拟")

    def advance(self):
        self.check_trial_period()
        if self.external:
            self.external.periodic()
            if self.paper:
                self.offline_stops.poll()
            for window in self.windows:
                window.refresh_external(self.external.snapshots.get(window.instrument.key))
            self.refresh_records()
            return
        if self.paused or not self.offline_mode:
            return
        snapshots = self.simulator.advance()
        self.offline_stops.poll()
        for snapshot in snapshots.values():
            self.local_algos.update(snapshot)
        for window in self.windows:
            window.refresh(snapshots[window.instrument.key])
        self.refresh_records()

    def refresh_records(self):
        if not self.external and not self.offline_mode and not self.paper:
            return
        from .dom_reference_ui import refresh_contract_footer
        for window in self.windows:
            refresh_contract_footer(window, window.model.snapshot)
        if self.external and not self.paper:
            self.refresh_external_records()
            return
        ledger = self.ledger
        cues = self.sound_tracker.observe(ledger, {key: str(order.status) for key, order in ledger.orders.items()}, {fill.event_id for fill in ledger.fills})
        for cue in dict.fromkeys(cues):
            self.sounds.play(cue)
        self.populate(self.orders, [(o.id, o.key, o.side, o.offset, ledger.catalog[o.key].price(o.ticks), o.quantity, o.filled, o.status, f"[{o.order_type}] {o.message}", o.group) for o in ledger.orders.values()])
        rows = []
        for (key, side), position in ledger.positions.items():
            if not position.quantity:
                continue
            instrument = ledger.catalog[key]
            average = Decimal(position.cost_ticks) / position.quantity
            last = self.ledger.marks.get(key, instrument.initial_ticks) if self.paper else self.simulator.prices[key]
            pnl = (last * position.quantity - position.cost_ticks) * instrument.tick * instrument.multiplier * (1 if side == "买" else -1)
            rows.append((key, "多" if side == "买" else "空", position.quantity, f"{average * instrument.tick:.4f}", f"{pnl:.2f}"))
        self.populate(self.positions, rows)
        self.populate(self.trades, [(f.event_id, f.order_id, f.key, f.side, f.offset, ledger.catalog[f.key].price(f.ticks), f.quantity) for f in ledger.fills])
        self.statusBar().showMessage(f"{self.mode_name()} · 已实现盈亏 {ledger.realized:.2f} 元 · 不含手续费/保证金 · {len(self.windows)} 个 DOM 窗口")
        if self.paper:
            status = self.external.md_status if self.external else "已断开"
            if self.paper.paused:
                status += " · 已暂停撮合"
            self.state.setText(f"在线模拟 · 真实行情 / 本地委托成交 · 行情：{status} · 不发送外部委托")
            funds = f"在线模拟：真实行情驱动本地撮合，不是外部交易账户。\n已实现盈亏：{ledger.realized:.2f}\n按最后报价计算浮动盈亏：{self.paper.unrealized():.2f}\n不模拟手续费、保证金、交易所排队优先级或真实成交保证。\n本地持仓和记录仅本次应用会话有效。"
            if self.funds.toPlainText() != funds:
                self.funds.setPlainText(funds)

    def cancel_selected(self):
        row = self.orders.currentRow()
        if row >= 0:
            self.cancel_one(self.orders.item(row, 0).text())

    def cancel_one(self, order_id):
        if self.external and not self.paper:
            try:
                self.external.cancel(order_id)
            except (ValueError, KeyError) as error:
                QMessageBox.warning(self, "撤单未提交", str(error))
            return
        if not self.offline_mode and not self.paper:
            self.log("连接已断开，请重连查询并在接口委托列表处理")
            return
        try:
            accepted = self.ledger.cancel_report(order_id)
            if self.paper:
                self.paper.sync_references()
        except ValueError as error:
            self.log(str(error))
            return
        self.log(f"{order_id} {'模拟撤单成功' if accepted else '模拟撤单失败'}")
        self.refresh_records()

    def cancel_orders(self, key, group=None, shortcut=False):
        if self.external and not self.paper:
            if shortcut:
                self.log("外部交易未绑定空格撤单；请在委托列表逐笔选择撤单")
                return
            self.log("外部交易请在委托列表逐笔选择撤单")
            self.set_details_visible(True)
            self.tabs.setCurrentIndex(1)
            self.raise_()
            return
        if not self.offline_mode and not self.paper:
            self.log("连接已断开，请重连后查询外部委托")
            return
        for order in self.ledger.orders.values():
            if order.key == key and order.status in ACTIVE and (group is None or order.group == group):
                self.cancel_one(order.id)

    def check_trial_period(self):
        state = current_trial_state(self.state_dir)
        if state is None:
            return
        now = datetime.now()
        if state.expired_reason or state.remaining(now).total_seconds() <= 0:
            if not state.expired_reason:
                state = TrialState(state.first_run, state.last_run, "试用已到期")
            self.trial_state = state
            self.on_trial_expired(state)
            return
        self.trial_state = state
        self.update_trial_title()

    def update_trial_title(self, state=None):
        state = state or self.trial_state
        if state is None:
            return
        title = self.windowTitle()
        marker = title.find(" · 试用版")
        if marker >= 0:
            title = title[:marker]
        new_title = title + f" · {state.label()}"
        if new_title != self.windowTitle():
            self.setWindowTitle(new_title)

    def on_trial_expired(self, state):
        self.shutting_down = True
        self.timer.stop()
        for window in list(self.windows):
            window.close()
        for window in (self.position_window, self.close_record_window, self.group_bar):
            if window is not None:
                window.close()
        QMessageBox.warning(self, "试用期已结束", f"{state.label()}\n\n如需继续使用，请联系开发者获取正式版本。")
        QApplication.instance().quit()

    def log(self, text):
        if self.external:
            from .external import redact
            text = redact(text, self.external.config)
        text = str(text)
        self.logs.append(text)
        logging.info(json.dumps(dict(event="operation", mode=self.mode_name(), message=text), ensure_ascii=False))
        self.log_message.emit(text)

    def mode_name(self):
        if self.paper:
            return "在线模拟" if self.external else "在线模拟 / 行情已断开"
        if not self.external:
            return "离线模拟" if self.offline_mode else "已断开"
        if not self.external.config.td_front:
            return "仅看行情"
        return "外部模拟" if self.external.config.environment == "simulation" else "实盘只读"

    def connect_ctp(self):
        if self.use_startup_login:
            self.connection_requested.emit()
            return
        if self.gateway:
            self.log("请先断开当前 CTP 连接，再切换服务")
            return
        dialog = ConnectionDialog(self.state_dir, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.start_external(dialog.config)

    def enable_online_paper(self):
        session = self.external
        if not session or not self.gateway or session.config.td_front or session.config.allow_orders or self.paper or self.gateway.closing or session.faulted:
            self.log("当前连接不能直接切换在线模拟，请从登录页选择在线模拟")
            return
        config = replace(session.config, paper_trading=True, environment="simulation")
        config.validate()
        self.local_algos.suspend("切换模拟会话")
        self.offline_stops.close()
        session.config = config
        self.ledger = Ledger(catalog=dict(self.catalog), limits=self.ledger.limits)
        self.paper = PaperSimulator(self.ledger, session)
        self.offline_stops = OfflineStops(self, online=True)
        self.ledger.stop_manager = self.offline_stops.stops
        self.paused = False
        self.pause_button.setEnabled(True)
        self.pause_button.setText("暂停模拟")
        for snapshot in session.snapshots.values():
            self.paper.accept(snapshot, session.md_epoch)
        self.setWindowTitle("意意交易 · 在线模拟")
        self.update_trial_title()
        self.log("已切换在线模拟：复用当前行情，不连接交易账户；收到新鲜真实报价后可本地下单，持仓仅本次会话有效")
        self.advance()

    def start_external(self, config):
        if self.gateway:
            raise ValueError("已有 CTP 连接")
        if self.persistence_error:
            self.log(self.persistence_error)
            return
        self.local_algos.suspend("开始新的接口会话")
        gateway = None
        try:
            config.validate()
            self.offline_stops.close()
            catalog = load_instruments(reference_path("Instruments.snapshot.xml"))
            for key, instrument in CATALOG.items():
                catalog.setdefault(key, instrument)
            gateway = CtpGateway(config, self.state_dir, self)
            journal, legacy = transaction_paths(self.state_dir, config) if config.td_front else (None, None)
            session = ExternalSession(config, gateway, catalog, journal, legacy_journal_path=legacy)
        except Exception as error:
            if gateway:
                gateway.close()
            self.log(f"CTP 初始化失败：{type(error).__name__}")
            return
        self.gateway, self.external, self.catalog = gateway, session, session.catalog
        self.offline_mode = False
        if config.paper_trading:
            if not self.paper:
                self.ledger = Ledger(catalog=dict(self.catalog), limits=self.ledger.limits)
            self.paper = PaperSimulator(self.ledger, session)
            self.offline_stops = OfflineStops(self, online=True)
            self.ledger.stop_manager = self.offline_stops.stops
        else:
            self.paper = None
        gateway.event.connect(lambda event, source=gateway: self.on_external_event(event, source))
        gateway.stopped.connect(self.on_gateway_stopped)
        self.paused = False
        self.pause_button.setEnabled(bool(self.paper))
        self.pause_button.setText("暂停撮合" if self.paper else "暂停模拟")
        self.simulator.snapshots.clear()
        for window in self.windows:
            window.model.snapshot = None
            session.retain(window.instrument.key)
            window.refresh_external(None)
        self.filter_contracts()
        self.setWindowTitle(f"意意交易 · CTP · {self.mode_name()}")
        self.update_trial_title()
        self.log(f"行情连接进程已启动 · {self.mode_name()}；正在检查 TCP 网络，随后等待 CTP 登录回调")

    def on_external_event(self, event, source=None):
        if (source is not None and source is not self.gateway) or not self.external or self.gateway.closing:
            return
        try:
            self.external.handle(event)
        except Exception as error:
            self.external.ready = False
            self.external.faulted = True
            self.log(f"接口事件处理失败：{type(error).__name__}，禁止交易，需重连对账")
        if self.external.error and self.external.error != self.last_error:
            self.last_error = self.external.error
            self.log(self.last_error)
        if event["kind"] == "md_state" and event.get("epoch") != self.external.md_epoch:
            return
        if not self.paper:
            cues = self.sound_tracker.observe(self.external, {key: ORDER_STATES.get(row.get("OrderStatus"), row.get("OrderStatus")) for key, row in self.external.orders.items()}, set(self.external.trades), audible=event["kind"] in {"order", "trade"} and self.external.logged_in and not self.external.syncing and not self.external.faulted)
            for cue in dict.fromkeys(cues):
                self.sounds.play(cue)
        if event["kind"] == "sync_done":
            self.filter_contracts()
        if event["kind"] in {"md_state", "fatal"}:
            self.local_algos.suspend("行情连接改变")
            if self.paper:
                self.paper.suspend("行情连接改变，本地模拟挂单已撤销")
            for window in self.windows:
                window.model.snapshot = None
        if self.paper and event["kind"] == "quote":
            snapshot = self.external.snapshots.get(event.get("key"))
            if snapshot and snapshot.received_monotonic == event.get("received") and self.paper.accept(snapshot, event["epoch"]) and not self.paused:
                self.local_algos.update(snapshot)
                prices = [snapshot.last]
                for name in ("range_low", "range_high"):
                    try:
                        prices.append(quote_ticks(event[name], self.catalog[snapshot.key].tick))
                    except (KeyError, ValueError, ArithmeticError):
                        continue
                self.offline_stops.stops.observe(snapshot.key, min(prices), max(prices))

    def disconnect_ctp(self):
        self.local_algos.suspend("连接正在断开")
        if self.gateway:
            if self.paper:
                self.paper.suspend("行情已断开，本地模拟挂单已撤销")
            self.external.ready = False
            self.external.logged_in = False
            self.external.td_status = "断开中"
            self.external.md_status = "断开中"
            self.external.snapshots.clear()
            self.external.td_generation += 1
            for stop in self.external.stops.stops.values():
                if stop.mode == "local" and stop.state not in {"filled", "canceled", "failed"}:
                    stop.state, stop.message = "paused", "连接已断开；须重新对账并手动启用"
            self.gateway.close()
            self.log("正在断开 CTP；不撤单，不重发委托")

    def on_gateway_stopped(self):
        old_gateway = self.gateway
        if self.paper:
            self.paper.suspend("行情进程已结束，本地模拟挂单已撤销")
        if self.external:
            try:
                self.external.close()
            except ValueError as error:
                self.persistence_error = "上次交易日志未能安全关闭，须修复并重启核对后再连接"
                self.log(str(error))
        self.external = None
        self.gateway = None
        self.offline_mode = False
        self.last_error = ""
        self.pause_button.setEnabled(False)
        for window in self.windows:
            window.setWindowTitle(f"{window.instrument.key} [已断开]")
            window.refresh_disconnected()
            if self.paper:
                window.setWindowTitle(f"{window.instrument.key} [在线模拟 / 行情已断开]")
        self.state.setText("行情与交易已断开 · 外部委托可能仍有效 · 重连后先查询对账")
        self.setWindowTitle("意意交易 · 已断开")
        self.update_trial_title()
        self.funds.setPlainText("CTP 已断开，列表为断开前记录。外部委托可能仍有效，重新连接后先查询对账。")
        if self.paper:
            self.refresh_records()
        self.filter_contracts()
        if old_gateway:
            old_gateway.deleteLater()
        if self.exit_pending:
            self.close()
        elif self.pending_offline:
            self.pending_offline = False
            self.enter_offline()

    def enter_offline(self):
        if self.gateway:
            self.pending_offline = True
            self.disconnect_ctp()
            return
        self.local_algos.suspend("切换到离线会话")
        self.offline_mode = True
        self.offline_stops.close()
        self.paper = None
        self.offline_stops = OfflineStops(self)
        self.catalog = dict(CATALOG)
        self.checked_contracts.intersection_update(self.catalog)
        self.ledger = Ledger()
        self.ledger.stop_manager = self.offline_stops.stops
        self.simulator = Simulator(self.ledger)
        self.paused = False
        self.pause_button.setEnabled(True)
        self.pause_button.setText("暂停模拟")
        for window in list(self.windows):
            if window.instrument.key not in self.catalog:
                window.close()
                continue
            window.instrument = self.catalog[window.instrument.key]
            window.model.instrument = window.instrument
            window.quantity.setMaximum(self.ledger.max_order)
            window.setWindowTitle(f"{window.instrument.key} [离线模拟]")
            window.notice.setText("离线持仓仅本次会话\n不含手续费")
            window.footer.setText("离线模拟")
        self.state.setText("离线模拟 · 行情与成交均为本地生成")
        self.setWindowTitle("意意交易 · 离线模拟")
        self.update_trial_title()
        self.filter_contracts()
        self.advance()

    def query_external(self):
        if self.external:
            self.external.synchronize()

    def settlement_dialog(self):
        if not self.external or not self.external.logged_in:
            self.log("交易未登录，无法查询结算单")
            return
        session = self.external
        dialog = QDialog(self)
        dialog.setWindowTitle("接口结算单 · 查看后确认")
        dialog.resize(760, 570)
        layout = QVBoxLayout(dialog)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(session.settlement or "当前没有结算单文本；请等待初始查询完成，或重连后重新查询。")
        layout.addWidget(text)
        confirmed = QCheckBox("我已阅读本交易日结算信息并确认")
        layout.addWidget(confirmed)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        confirmed.toggled.connect(lambda checked: buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(checked and bool(session.settlement) and not session.syncing))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted and self.external is session and session.logged_in:
            session.send("confirm_settlement")
            self.log("结算确认请求已发送，等待接口回报")

    def refresh_external_records(self):
        session = self.external
        if not session:
            return
        if not session.config.td_front:
            self.state.setText(f"仅看行情 · 行情: {session.md_status} · 未连接交易账户，不能下单")
            for table in (self.orders, self.positions, self.trades):
                self.populate(table, [])
            text = "当前仅连接行情服务，没有查询交易账户、持仓或资金。需要交易时请在登录页配置自己的交易账户。"
            if self.funds.toPlainText() != text:
                self.funds.setPlainText(text)
            self.statusBar().showMessage(f"仅看行情 · {len(session.snapshots)} 个合约有报价 · {session.error}")
            return
        self.state.setText(f"{self.mode_name()} · 行情: {session.md_status} · 交易: {session.td_status}")
        rows = []
        for key, row in session.orders.items():
            status = ORDER_STATES.get(row.get('OrderStatus'), {"pending": "待回报", "rejected": "已拒绝"}.get(row.get('OrderStatus'), "未知"))
            if row.get("OrderSubmitStatus") == "4":
                status = "已拒绝"
            if key in session.canceling:
                status += " / 撤单中"
            rows.append((key, f"{row.get('ExchangeID')}.{row.get('InstrumentID')}", "买" if row.get("Direction") == "0" else "卖", OFFSET_NAMES.get(row.get("CombOffsetFlag", "")[0:1], "未知"), row.get("LimitPrice"), row.get("VolumeTotalOriginal"), row.get("VolumeTraded"), status, row.get("StatusMsg", ""), row.get("Group", "未分配")))
        self.populate(self.orders, rows)
        positions = []
        for row in session.positions:
            if not row.get("Position"):
                continue
            key = f"{row.get('ExchangeID')}.{row.get('InstrumentID')}"
            instrument = session.catalog.get(key)
            average = Decimal(str(row.get("OpenCost", 0))) / row["Position"] / instrument.multiplier if instrument else Decimal("0")
            direction = {"2": "多", "3": "空"}.get(row.get("PosiDirection"), "净")
            date = "今" if row.get("PositionDate") == "1" else "昨/合并"
            positions.append((key, f"{direction} {date} 套保:{row.get('HedgeFlag')}", f"{row['Position']} (冻{row.get('LongFrozen', 0) + row.get('ShortFrozen', 0)})", f"{average:.4f}", row.get("PositionProfit")))
        self.populate(self.positions, positions)
        self.populate(self.trades, [(key, row.get("OrderRef"), f"{row.get('ExchangeID')}.{row.get('InstrumentID')}", "买" if row.get("Direction") == "0" else "卖", OFFSET_NAMES.get(row.get("OffsetFlag"), "未知"), row.get("Price"), row.get("Volume")) for key, row in session.trades.items()])
        funds = "\n".join(f"{name}: {row.get(name, '')}" for row in session.accounts for name in ("CurrencyID", "TradingDay", "Balance", "Available", "CurrMargin", "FrozenMargin", "Commission", "CloseProfit", "PositionProfit"))
        if self.funds.toPlainText() != funds:
            self.funds.setPlainText(funds)
        self.statusBar().showMessage(f"{self.mode_name()} · 结算{'已确认' if session.settled else '未确认'} · {'对账完成' if session.ready else '禁止发单，待对账'} · 合约核验 {len(session.verified)} · {session.error}")

    def apply_dom_config(self, window):
        config = self.config_store.effective()
        if config.font in QFontDatabase.families():
            window.setFont(QFont(config.font, 9))
            window.model.font_family = config.font
        window.table.wheel_steps = config.wheel_steps
        window.set_zoom(window.zoom_percent, resize=False)

    def apply_display_config(self):
        for window in self.windows:
            self.apply_dom_config(window)

    def save_risk_limits(self, limits):
        values = dict(max_total=limits.total, max_group=limits.group, max_cancels=limits.cancels, max_daily_loss=limits.loss)
        path = self.state_dir / "risk.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def refresh_metadata(self):
        if not self.external or not self.external.logged_in:
            self.log("刷新合约需要交易接口登录；历史快照不作为报单依据")
            return
        self.external.synchronize(instruments=True)
        self.log("已请求刷新合约元数据；等待接口查询完成")

    def geometry_values(self, window):
        return [window.x(), window.y(), window.width(), window.height()]

    def apply_geometry(self, window, values):
        if not isinstance(values, list) or len(values) != 4 or any(type(v) is not int for v in values):
            raise ValueError("窗口位置格式无效")
        screen = QApplication.primaryScreen().availableGeometry()
        x, y, width, height = values
        width = min(max(width, window.minimumWidth()), screen.width())
        height = min(max(height, window.minimumHeight(), 300), screen.height())
        x = min(max(x, screen.left()), screen.right() - width + 1)
        y = min(max(y, screen.top()), screen.bottom() - height + 1)
        window.resize(width, height)
        window.move(x, y)

    def save_layout(self):
        geometry = self.geometry_values(self)
        if not self.details_visible and self.expanded_size:
            geometry[2:] = [self.expanded_size.width(), self.expanded_size.height()]
        state = {"schema_version": 1, "controller": geometry, "controller_group": self.group.value(), "details_visible": self.details_visible, "windows": [{"key": w.instrument.key, "group": w.group.value(), "geometry": self.geometry_values(w), "follow": w.model.follow, "center": w.model.center, "zoom": w.zoom_percent} for w in self.windows]}
        try:
            temporary = self.layout_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.layout_path)
            self.log("布局已保存")
        except OSError as error:
            self.log(f"布局保存失败：{error}")

    def restore_layout(self):
        if not self.layout_path.exists():
            return
        try:
            state = json.loads(self.layout_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("schema_version") != 1:
                raise ValueError("不支持的布局版本")
            items = state.get("windows", [])
            if not isinstance(items, list) or len(items) > 40:
                raise ValueError("布局窗口列表无效或超过40个")
            group = state.get("controller_group", 1)
            details = state.get("details_visible", True)
            if type(group) is not int or not 1 <= group <= 20 or type(details) is not bool:
                raise ValueError("控制台组别或显示状态无效")
            self.set_details_visible(True)
            self.apply_geometry(self, state["controller"])
            self.group.setValue(group)
            for item in items:
                if item["key"] not in self.catalog or type(item["group"]) is not int or not 1 <= item["group"] <= 20:
                    raise ValueError("布局合约或分组无效")
                window = next((existing for existing in self.windows if existing.instrument.key == item["key"] and existing.group.value() == item["group"]), None)
                if window is None:
                    window = self.open_dom(item["key"], item["group"], item["geometry"])
                else:
                    self.apply_geometry(window, item["geometry"])
                zoom = item.get("zoom", 100)
                if type(zoom) is not int or not 75 <= zoom <= 200:
                    raise ValueError("布局显示比例无效")
                window.set_zoom(zoom, resize=False)
                window.follow.setChecked(item.get("follow", True) is True)
                center = item.get("center", window.model.center)
                if type(center) is not int or center <= 0:
                    raise ValueError("价梯中心无效")
                window.model.center = center
                window.model.dataChanged.emit(window.model.index(0, 0), window.model.index(window.model.rows - 1, 2))
            self.set_details_visible(details)
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.log(f"布局恢复失败：{error}；请重新排列后保存")

    def closeEvent(self, event):
        self.local_algos.suspend("应用退出")
        if self.strategies_dialog is not None:
            self.strategies_dialog.close()
        if self.gateway:
            self.exit_pending = True
            self.disconnect_ctp()
            event.ignore()
            return
        if self.windows:
            self.save_layout()
        self.shutting_down = True
        self.timer.stop()
        for window in list(self.windows):
            window.close()
        for window in (self.position_window, self.close_record_window, self.group_bar):
            if window is not None:
                window.close()
        for window in (self.position_window, self.close_record_window, self.group_bar):
            if window is not None:
                window.deleteLater()
        try:
            self.offline_stops.close()
        except ValueError as error:
            self.log(str(error))
        event.accept()
        QApplication.instance().quit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--diagnostics", type=Path)
    parser.add_argument("--native-check", action="store_true")
    args = parser.parse_args()
    if args.native_check and not args.diagnostics:
        parser.error("--native-check 须与 --diagnostics 输出路径一起使用")
    if args.diagnostics:
        from .diagnostics import write_diagnostics
        raise SystemExit(0 if write_diagnostics(args.diagnostics, args.native_check) else 1)
    app = QApplication([sys.argv[0]])
    app.setOrganizationName("PyTrade")
    app.setApplicationVersion(VERSION)
    app.setApplicationName("OfflineTerminal")
    app.setStyle("Fusion")
    app.setFont(QFont("新宋体" if sys.platform == "win32" else "Noto Sans CJK SC", 9))
    from .theme import APPLICATION_STYLE
    app.setStyleSheet(APPLICATION_STYLE)
    state_dir = args.state_dir or Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))
    state_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(state_dir)
    app.setQuitOnLastWindowClosed(False)
    trial_state = enforce_trial_at_startup(state_dir, application_root())
    if args.smoke_test:
        controller = Controller(state_dir)
        controller.show()
        def verify():
            try:
                if not controller.windows:
                    controller.open_dom(INSTRUMENTS[0].key)
                window = controller.windows[0]
                snapshot = controller.simulator.snapshots[window.instrument.key]
                order = controller.ledger.submit(window.instrument.key, "买", "开仓", snapshot.asks[-1][0] + 10, 3)
                controller.advance()
                controller.advance()
                controller.advance()
                assert controller.ledger.orders[order.id].filled == 3
                if args.screenshot:
                    args.screenshot.mkdir(parents=True, exist_ok=True)
                    assert controller.grab().save(str(args.screenshot / "controller.png"))
                    assert window.grab().save(str(args.screenshot / "dom.png"))
                controller.close()
            except Exception:
                logging.exception("GUI smoke test failed")
                app.exit(1)
        QTimer.singleShot(800, verify)
    else:
        from .flow import ApplicationFlow
        flow = ApplicationFlow(state_dir, trial_state)
        flow.show()
    result = app.exec()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    sys.exit(result)
