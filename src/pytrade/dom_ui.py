import re
import time

from PySide6.QtCore import QAbstractTableModel, QLocale, QModelIndex, QRect, Qt
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton,
    QScrollArea, QSpinBox, QTableView, QVBoxLayout, QWidget,
)

from .theme import LEGACY_PALETTE
from .domain import ACTIVE
from .dom_controls import DomControls
from .order_conversion import OffsetLeg, local_net_plan


def legacy_style(zoom=100):
    palette = LEGACY_PALETTE
    return f"""
        QWidget {{ background:{palette['sidebar']}; color:#181b22; font-size:{11 * zoom / 100:g}px; }}
        QLabel {{ background:transparent; }}
        QPushButton, QSpinBox, QDoubleSpinBox, QComboBox, QLineEdit {{
            min-height:{18 * zoom / 100:g}px; padding:0px; border:1px solid #8f95a3;
            background:#fdfdfe;
        }}
        QPushButton {{ background:#dde3ee; font-weight:bold; }}
        QPushButton:pressed {{ background:#b9c4d8; }}
        QCheckBox, QRadioButton {{ spacing:3px; min-height:{17 * zoom / 100:g}px; background:transparent; }}
        QCheckBox::indicator, QRadioButton::indicator {{ width:{12 * zoom / 100:g}px; height:{12 * zoom / 100:g}px; }}
        QRadioButton::indicator {{ border:1px solid #8796ae; border-radius:6px; background:#edf1fa; }}
        QRadioButton::indicator:checked {{ background:#53b97b; }}
        QCheckBox::indicator {{ border:1px solid #8796ae; background:#edf1fa; }}
        QCheckBox::indicator:checked {{ background:#53b97b; }}
        QCheckBox:disabled, QRadioButton:disabled {{ color:#757b88; }}
        QTableView {{ background:{palette['price_gap']}; gridline-color:#c9cdd6; border:1px solid #777; }}
        QScrollArea {{ border:0px; }}
        QScrollBar:vertical {{ width:8px; }}
    """


class LadderModel(QAbstractTableModel):
    def __init__(self, instrument, ledger):
        super().__init__()
        self.instrument = instrument
        self.ledger = ledger
        self.snapshot = None
        self.center = instrument.initial_ticks
        self.follow = True
        self.real_mode = False
        self.rows = 71
        self.font_family = ""
        self.row_height = 13
        self.font_size = 11
        self.pending_by_price = {}
        self.filled_by_price = {}
        self.show_fills = True
        self.price_step = 1

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.rows

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 3

    def price_at(self, row):
        return self.center + (self.rows // 2 - row) * self.price_step

    def price_color(self, price):
        snapshot = self.snapshot
        if snapshot:
            if snapshot.asks and price >= min(ticks for ticks, quantity in snapshot.asks):
                return LEGACY_PALETTE["ask_price"]
            if snapshot.bids and price <= max(ticks for ticks, quantity in snapshot.bids):
                return LEGACY_PALETTE["bid_price"]
        return LEGACY_PALETTE["price_gap"]

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return ("买量", "价格", "卖量")[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        price = self.price_at(index.row())
        col = index.column()
        snapshot = self.snapshot
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return Qt.AlignmentFlag.AlignCenter
        if role == Qt.ItemDataRole.DisplayRole:
            if price <= 0 or (self.real_mode and snapshot is None):
                return ""
            if col == 1:
                return self.instrument.price(price)
            levels = dict(snapshot.bids if col == 0 else snapshot.asks) if snapshot else {}
            quantity = levels.get(price)
            return str(quantity) if quantity is not None else ""
        if role == Qt.ItemDataRole.BackgroundRole:
            palette = LEGACY_PALETTE
            if col == 1:
                return QColor(self.price_color(price))
            if col == 0:
                return QColor(palette["buy_volume"])
            if col == 2:
                return QColor(palette["sell_volume"])
            return QColor(palette["price_gap"])
        if role == Qt.ItemDataRole.ForegroundRole:
            palette = LEGACY_PALETTE
            if col == 1:
                return QColor(palette["last_price_text"] if snapshot and price == snapshot.last else palette["price_text"])
            return QColor(palette["buy_volume_text"] if col == 0 else palette["sell_volume_text"])
        if role == Qt.ItemDataRole.FontRole:
            font = QFont(self.font_family)
            font.setPixelSize(min(self.font_size, self.row_height - 2))
            font.setBold(True)
            return font
        if role == Qt.ItemDataRole.ToolTipRole:
            return "单击选择委托价格；通过左侧买开/卖开/买平/卖平按钮提交"

    def update_snapshot(self, snapshot):
        if snapshot is self.snapshot and (not self.follow or self.center == snapshot.last):
            return
        self.snapshot = snapshot
        if self.follow:
            self.center = snapshot.last
        self.dataChanged.emit(self.index(0, 0), self.index(self.rows - 1, 2))


class LadderView(QTableView):
    wheel_steps = 3

    def paintEvent(self, event):
        model = self.model()
        if model is None:
            return
        painter = QPainter(self.viewport())
        palette = LEGACY_PALETTE
        painter.fillRect(self.viewport().rect(), QColor(palette["price_gap"]))
        snapshot = model.snapshot
        height = model.row_height
        font = QFont(model.font_family)
        font.setPixelSize(min(model.font_size, height - 2))
        font.setBold(True)
        painter.setFont(font)
        positions = [self.columnViewportPosition(column) for column in range(3)]
        widths = [self.columnWidth(column) for column in range(3)]
        bids = dict(snapshot.bids) if snapshot else {}
        asks = dict(snapshot.asks) if snapshot else {}
        fills = model.filled_by_price if model.show_fills else {}
        pendings = model.pending_by_price
        fill_color = QColor(palette["mark_fill"])
        open_color = QColor(palette["mark_open"])
        for row in range(model.rows):
            y = row * height
            price = model.price_at(row)
            if price <= 0:
                continue
            painter.fillRect(positions[0], y, widths[0], height, QColor(palette["buy_volume"]))
            painter.fillRect(positions[2], y, widths[2], height, QColor(palette["sell_volume"]))
            painter.fillRect(positions[1], y, widths[1], height, QColor(model.price_color(price)))
            quantity = bids.get(price)
            if quantity is not None and not (model.real_mode and snapshot is None):
                painter.setPen(QColor(palette["buy_volume_text"]))
                painter.drawText(QRect(positions[0], y, widths[0], height), Qt.AlignmentFlag.AlignCenter, str(quantity))
            if not (model.real_mode and snapshot is None):
                painter.setPen(QColor(palette["last_price_text"] if snapshot and price == snapshot.last else palette["price_text"]))
                painter.drawText(QRect(positions[1], y, widths[1], height), Qt.AlignmentFlag.AlignCenter, model.instrument.price(price))
            quantity = asks.get(price)
            if quantity is not None and not (model.real_mode and snapshot is None):
                painter.setPen(QColor(palette["sell_volume_text"]))
                painter.drawText(QRect(positions[2], y, widths[2], height), Qt.AlignmentFlag.AlignCenter, str(quantity))
            marks = (fills.get(price, 0), pendings.get(price, 0))
            if marks != (0, 0):
                width = max(2, widths[0] // 6)
                if marks[1]:
                    painter.fillRect(positions[0], y, width, height, open_color)
                if marks[0]:
                    painter.fillRect(positions[0] + width, y, width, height, fill_color)
                painter.fillRect(positions[2] + widths[2] - 2 * width, y, 2 * width, height, fill_color if marks[0] else open_color)
        painter.setPen(QColor("#c9cdd6"))
        for row in range(model.rows + 1):
            painter.drawLine(0, row * height, self.viewport().width(), row * height)
        for position, width in zip(positions, widths):
            painter.drawLine(position + width - 1, 0, position + width - 1, self.viewport().height())
        if snapshot:
            distance = model.center - snapshot.last
            row = model.rows // 2 + distance // model.price_step
            if 0 <= row < model.rows:
                painter.setPen(QColor("#000000"))
                painter.drawLine(0, row * height, self.viewport().width(), row * height)
        painter.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        model = self.model()
        if model:
            rows = max(15, self.viewport().height() // model.row_height)
            if rows != model.rows:
                model.beginResetModel()
                model.rows = rows
                model.endResetModel()

    def wheelEvent(self, event):
        model = self.model()
        if event.angleDelta().y() == 0:
            event.ignore()
            return
        model.follow = False
        model.center = max(1, model.center + model.price_step * (self.wheel_steps if event.angleDelta().y() > 0 else -self.wheel_steps))
        model.dataChanged.emit(model.index(0, 0), model.index(model.rows - 1, 2))
        event.accept()


class DomWindow(QWidget):
    def __init__(self, controller, key, group=1):
        super().__init__()
        self.controller = controller
        self.controls = DomControls()
        self.zoom_percent = 100
        self.closing = False
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, True)
        self.instrument = controller.catalog[key]
        self.setWindowTitle(f"{key} [离线模拟]")
        self.resize(300, controller.config_store.effective().dom_height)
        self.setMinimumSize(288, 360)
        self.setStyleSheet(legacy_style())
        shell = QVBoxLayout(self)
        shell.setContentsMargins(1, 1, 1, 1)
        shell.setSpacing(1)
        topbar = QWidget()
        self.topbar = topbar
        topbar.setStyleSheet(f"background:{LEGACY_PALETTE['topbar']};color:{LEGACY_PALETTE['topbar_text']};font-weight:bold;font-size:12px")
        topbar.setFixedHeight(22)
        top_layout = QHBoxLayout(topbar)
        top_layout.setContentsMargins(5, 0, 5, 0)
        top_layout.setSpacing(6)
        self.contract_label = QLabel(self.instrument.key)
        self.mode_label = QLabel("离线模拟")
        self.time_label = QLabel("--:--:--")
        for label in (self.contract_label, self.mode_label, self.time_label):
            label.setStyleSheet("background:transparent;color:%s" % LEGACY_PALETTE["topbar_text"])
        top_layout.addWidget(self.contract_label)
        top_layout.addStretch(1)
        top_layout.addWidget(self.mode_label)
        top_layout.addWidget(self.time_label)
        shell.addWidget(topbar)
        root = QHBoxLayout()
        shell.addLayout(root, 1)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)
        sidebar = QWidget()
        self.sidebar = sidebar
        sidebar.setFixedWidth(132)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(1)
        self.banner = QLabel("离线模拟")
        self.banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.banner.setWordWrap(True)
        self.banner.setFixedHeight(19)
        self.banner.setStyleSheet("background:#e2e5ec;color:#a32638;font-size:12px;font-weight:bold")
        side.addWidget(self.banner)
        window_tools = QHBoxLayout()
        window_tools.setSpacing(1)
        for text, tip, action in (("－", "缩小显示", lambda: self.set_zoom(self.zoom_percent - 25)), ("＋", "放大显示", lambda: self.set_zoom(self.zoom_percent + 25)), ("关闭", "关闭本窗（不撤销外部委托）", self.close)):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(action)
            window_tools.addWidget(button)
        side.addLayout(window_tools)
        self.maximize_button = QPushButton("最大化 / 还原")
        self.maximize_button.clicked.connect(self.toggle_maximized)
        side.addWidget(self.maximize_button)
        self.close_shortcut = QShortcut(QKeySequence("Ctrl+W"), self)
        self.close_shortcut.activated.connect(self.close)
        self.cancel_shortcut = QShortcut(QKeySequence("Space"), self)
        self.cancel_shortcut.activated.connect(lambda: controller.cancel_orders(key, group=self.group.value(), shortcut=True))
        self.summary = QLabel("等待行情")
        self.summary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.summary.setFixedHeight(27)
        self.summary.setStyleSheet(f"background:{LEGACY_PALETTE['summary']};color:{LEGACY_PALETTE['summary_text']};font-size:16px;font-weight:bold")
        side.addWidget(self.summary)
        self.quote = QLabel()
        self.quote.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.quote.setMinimumHeight(52)
        self.quote.setStyleSheet(f"background:{LEGACY_PALETTE['quote_panel']};font-weight:bold")
        side.addWidget(self.quote)
        self.market_stats = QLabel("总持仓 —\n增减仓 —")
        self.market_stats.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.market_stats.setToolTip("总持仓来自行情 OpenInterest；增减仓 = OpenInterest − PreOpenInterest，缺失不补零")
        side.addWidget(self.market_stats)
        self.long_position = QLabel("多 0")
        self.short_position = QLabel("空 0")
        for label, color in ((self.long_position, LEGACY_PALETTE["buy_volume"]), (self.short_position, LEGACY_PALETTE["side_button_sell"])):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setFixedHeight(21)
            label.setStyleSheet(f"background:{color};color:#ffffff;font-size:14px;font-weight:bold")
            side.addWidget(label)
        presets = QHBoxLayout()
        presets.setSpacing(1)
        self.left_quantity = QSpinBox()
        self.left_quantity.setRange(1, controller.ledger.max_order)
        self.left_quantity.setPrefix("左 ")
        self.left_quantity.setToolTip("鼠标左键挂单数量；与下方按钮手数独立")
        self.right_quantity = QSpinBox()
        self.right_quantity.setRange(0, controller.ledger.max_order)
        self.right_quantity.setPrefix("右 ")
        self.right_quantity.setToolTip("鼠标右键挂单数量；0 为禁用，默认禁用")
        presets.addWidget(self.left_quantity)
        presets.addWidget(self.right_quantity)
        side.addLayout(presets)
        spread = QCheckBox("CntrbySprd")
        self.spread_checkbox = spread
        spread.setEnabled(False)
        spread.setToolTip("原版价差控制语义待确认")
        side.addWidget(spread)
        parameters = QGridLayout()
        parameters.setContentsMargins(0, 0, 0, 0)
        parameters.setHorizontalSpacing(1)
        parameters.setVerticalSpacing(1)
        self.spread_fields = []
        for row, (name, value) in enumerate((("", "ag_o"), ("Pt", "0"), ("Fct", "1"))):
            parameters.addWidget(QLabel(name), row, 0)
            field = QLineEdit(value if name else self.instrument.key.split(".")[-1])
            field.setMinimumWidth(0)
            field.setReadOnly(True)
            field.setEnabled(False)
            field.setToolTip("原版参数含义待确认，仅保留外观；不参与报单")
            parameters.addWidget(field, row, 1)
            self.spread_fields.append(field)
        side.addLayout(parameters)
        side.addSpacing(20)
        choices = QHBoxLayout()
        choices.setSpacing(2)
        for name in ("A", "B"):
            button = QRadioButton(name)
            button.setEnabled(False)
            button.setToolTip("原版 A/B 语义待确认")
            choices.addWidget(button)
        side.addLayout(choices)
        local_label = QLabel("以下锁定仅本窗模拟")
        side.addWidget(local_label)
        self.local_controls = {}
        tips = {
            "nearby": "本窗模拟规则：委托价距最新价不超过下方 tick 数；非已确认原版算法",
            "only_open": "本窗模拟仅允许开仓，平仓按钮和鼠标平仓均拦截",
            "pending_lock": "本窗模拟禁止重复提交同合约、同方向、同开平、同价位的活动挂单",
            "limit_lock": "本窗模拟禁止在涨跌停价及以外挂单；缺少涨跌停价格时阻止委托",
            "white_lock": "本窗模拟仅允许在有可见买卖盘口数量的价位挂单；非已确认原版算法",
        }
        for name, attribute in (("Chg Nearby ·近价", "nearby"), ("OnlyOpen ·仅开", "only_open"), ("挂单锁定", "pending_lock"), ("涨跌停锁", "limit_lock"), ("白格单锁", "white_lock")):
            box = QCheckBox(name)
            box.setToolTip(tips[attribute] + "；仅本地模拟生效")
            box.toggled.connect(lambda checked, attr=attribute: setattr(self.controls, attr, checked))
            self.local_controls[attribute] = box
            side.addWidget(box)
        self.nearby_ticks = QSpinBox()
        self.nearby_ticks.setRange(1, 1000)
        self.nearby_ticks.setValue(self.controls.nearby_ticks)
        self.nearby_ticks.setPrefix("近价 tick ")
        self.nearby_ticks.valueChanged.connect(lambda value: setattr(self.controls, "nearby_ticks", value))
        side.addWidget(self.nearby_ticks)
        self.cancel_policy = QComboBox()
        for label, cap in (("撤单：沿用总风控", None), ("本地上限 485 次", 485), ("本地上限 4000 次", 4000), ("NoCancel ·禁止手动撤", 0)):
            self.cancel_policy.addItem(label, cap)
        self.cancel_policy.setToolTip("仅本地模拟，本合约所有手动撤单入口共享；按本地交易日成功撤单计数。自动断线/换日保护撤单仍执行。485/4000 是本项目试验阈值，原版计数口径待确认")
        self.cancel_policy.currentIndexChanged.connect(self.set_cancel_policy)
        side.addWidget(self.cancel_policy)
        for name in ("M-OrderX",):
            box = QCheckBox(name)
            self.conditional_checkbox = box
            box.setEnabled(False)
            box.setToolTip("原版行为待确认，未启用")
            side.addWidget(box)
        self.fok_checkbox = QCheckBox("FOK〔本地模拟〕")
        self.fok_checkbox.setToolTip("标准全成或撤，本地模拟使用可见剩余盘口；不发送外部FOK委托")
        self.fok_checkbox.toggled.connect(lambda checked: self.order_type.setCurrentIndex(self.order_type.findData("FOK" if checked else "GFD")))
        side.addWidget(self.fok_checkbox)
        marks = QCheckBox("价梯标记")
        marks.setChecked(True)
        marks.setToolTip("在价梯两侧显示本合约委托与成交位置：蓝条为未成交委托，紫条为成交")
        marks.toggled.connect(lambda checked: self.set_marks_enabled(checked))
        self.marks_toggle = marks
        side.addWidget(marks)
        self.fills_toggle = QCheckBox("成交标识")
        self.fills_toggle.setChecked(True)
        self.fills_toggle.setToolTip("独立显示/隐藏按实际成交价汇总的紫色标记；不影响挂单标记")
        self.fills_toggle.toggled.connect(self.set_fills_enabled)
        side.addWidget(self.fills_toggle)
        self.ladder_step = QDoubleSpinBox()
        self.ladder_step.setLocale(QLocale.c())
        self.ladder_step.setDecimals(max(0, -self.instrument.tick.as_tuple().exponent))
        self.ladder_step.setRange(float(self.instrument.tick), float(self.instrument.tick * 1000))
        self.ladder_step.setSingleStep(float(self.instrument.tick))
        self.ladder_step.setValue(float(self.instrument.tick))
        self.ladder_step.setPrefix("价梯步长 ")
        self.ladder_step.setToolTip("只调整显示间隔，必须为合约最小变动价位的整数倍；不改变交易 tick 或既有委托")
        self.ladder_step.editingFinished.connect(self.set_ladder_step)
        side.addWidget(self.ladder_step)
        self.counters = QLabel("C:0　T:0")
        self.counters.setToolTip("本合约：C 为已撤委托数，T 为有成交的委托数；部分成交多笔只计一单。模拟为本会话，外部为已查询/回报记录")
        side.addWidget(self.counters)
        side.addStretch(1)
        self.position = QLabel("模拟持仓\n多 0　空 0")
        self.position.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.position.setMinimumHeight(46)
        self.position.setStyleSheet("color:#7775ba;font-size:14px")
        side.addWidget(self.position)
        original_marker = QLabel("P")
        self.original_marker = original_marker
        original_marker.setAlignment(Qt.AlignmentFlag.AlignCenter)
        original_marker.setFixedHeight(40)
        original_marker.setStyleSheet("color:#800040;font-family:'新宋体';font-size:28px")
        original_marker.setToolTip("原版静态窗体 LBKP 的显示文字；点击行为待运行验证，当前仅作外观参考")
        side.addWidget(original_marker)
        side.addStretch(2)
        self.order_panel = QWidget()
        side = QVBoxLayout(self.order_panel)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(1)
        self.quantity = QSpinBox()
        self.quantity.setRange(1, controller.ledger.max_order)
        self.quantity.setPrefix("手数 ")
        side.addWidget(self.quantity)
        self.price = QDoubleSpinBox()
        self.price.setDecimals(max(0, -self.instrument.tick.as_tuple().exponent))
        self.price.setRange(float(self.instrument.tick), 9999999)
        self.price.setSingleStep(float(self.instrument.tick))
        self.price.setValue(float(self.instrument.tick * self.instrument.initial_ticks))
        self.price.setLocale(QLocale.c())
        self.price.setToolTip("委托限价：单击价梯选择")
        side.addWidget(self.price)
        self.click_mode = QComboBox()
        for label, value in (("价梯：仅选价", "select"), ("原图区域·开〔模拟〕", "regions_open"), ("原图区域·平〔模拟〕", "regions_close"), ("左买右卖·开", "mouse_open"), ("左买右卖·平", "mouse_close"), ("灰格买卖·开", "columns_open"), ("灰格买卖·平", "columns_close")):
            self.click_mode.addItem(label, value)
        self.click_mode.setToolTip("仅本地模拟：左买右卖按鼠标左右键；灰格买卖按左侧买、右侧卖。开/平须明确选择；不是已确认的原版规则")
        side.addWidget(self.click_mode)
        self.close_offset = QComboBox()
        self.close_offset.addItems(("平仓", "平今", "平昨"))
        side.addWidget(self.close_offset)
        self.order_type = QComboBox()
        for label, value in (("普通限价 GFD", "GFD"), ("FAK ·即成剩撤", "FAK"), ("FOK ·全成或撤", "FOK")):
            self.order_type.addItem(label, value)
        self.order_type.currentIndexChanged.connect(self.sync_fok_checkbox)
        self.order_type.setToolTip("FAK/FOK 仅本地模拟：受理后的首次新报价尝试成交；不代表真实交易所队列。自动剩撤不受手动撤单锁限制")
        side.addWidget(self.order_type)
        self.net_mode = QCheckBox("净仓：先平后开〔模拟〕")
        self.net_mode.setToolTip("勾选后买开/卖开及鼠标开仓先平本组可用反向仓，余量开仓；扣除平仓挂单和止损占用，不等同于原版 A/B。OnlyOpen 开启时全部按开仓")
        side.addWidget(self.net_mode)
        strategies = QPushButton("本地策略 / 双腿价差")
        strategies.clicked.connect(lambda: controller.show_strategies(key))
        side.addWidget(strategies)
        buttons = QGridLayout()
        self.order_buttons = []
        buttons.setSpacing(1)
        for row, offset in enumerate(("开仓", "平仓")):
            for col, direction in enumerate(("买", "卖")):
                button = QPushButton(direction + offset[0])
                button.setStyleSheet(f"background:{LEGACY_PALETTE['side_button_buy'] if direction == '买' else LEGACY_PALETTE['side_button_sell']};color:#ffffff")
                button.clicked.connect(lambda checked=False, s=direction, o=offset: self.submit(s, o))
                buttons.addWidget(button, row, col)
                self.order_buttons.append(button)
        side.addLayout(buttons)
        self.group = QSpinBox()
        self.group.setRange(1, 20)
        self.group.setValue(group)
        self.group.setPrefix("分组: ")
        side.addWidget(self.group)
        self.follow = QCheckBox("跟随最新价")
        self.follow.setChecked(True)
        self.follow.toggled.connect(self.set_follow)
        side.addWidget(self.follow)
        actions = QHBoxLayout()
        actions.setSpacing(1)
        center = QPushButton("居中")
        center.clicked.connect(self.recenter)
        actions.addWidget(center)
        cancel = QPushButton("撤本窗")
        self.cancel_button = cancel
        cancel.setToolTip("撤销本合约委托")
        cancel.clicked.connect(lambda: controller.cancel_orders(key))
        actions.addWidget(cancel)
        side.addLayout(actions)
        self.paper_button = QPushButton("切换在线模拟")
        self.paper_button.setToolTip("使用当前真实行情进行本地模拟，不连接交易账户；需要新鲜有效报价")
        self.paper_button.clicked.connect(controller.enable_online_paper)
        self.paper_button.hide()
        side.addWidget(self.paper_button)
        self.home_button = QPushButton("返回首页")
        self.home_button.clicked.connect(controller.connect_ctp)
        side.addWidget(self.home_button)
        self.notice = QLabel("离线持仓仅本次会话\n不含手续费")
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet("color:#555;font-size:10px")
        side.addWidget(self.notice)
        self.footer = QLabel("离线模拟")
        self.footer.setWordWrap(True)
        self.footer.setStyleSheet("color:#005c68;font-size:10px")
        self.footer.setToolTip("滚轮移动价梯 · 单击选择委托限价 · 双击居中 · 蓝条挂单 / 紫条成交标记")
        side.addWidget(self.footer)
        sidebar_scroll = QScrollArea()
        self.sidebar_scroll = sidebar_scroll
        sidebar_scroll.setWidget(sidebar)
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setFixedWidth(140)
        sidebar_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.side_column = QWidget()
        side_column_layout = QVBoxLayout(self.side_column)
        side_column_layout.setContentsMargins(0, 0, 0, 0)
        side_column_layout.setSpacing(1)
        side_column_layout.addWidget(sidebar_scroll, 1)
        side_column_layout.addWidget(self.order_panel)
        self.side_column.setFixedWidth(140)
        root.addWidget(self.side_column)
        self.model = LadderModel(self.instrument, controller.ledger)
        self.table = LadderView()
        self.table.setModel(self.model)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().hide()
        self.table.verticalHeader().setMinimumSectionSize(13)
        self.table.verticalHeader().setDefaultSectionSize(13)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 58)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setSelectionMode(QTableView.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.clicked.connect(self.price_action)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(lambda position: self.price_action(self.table.indexAt(position), right=True))
        self.table.doubleClicked.connect(lambda index: self.recenter() if self.click_mode.currentData() == "select" else None)
        root.addWidget(self.table, 1)
        from .dom_reference_ui import restore_reference_layout
        restore_reference_layout(self)
        from .dom_algo_binding import DomAlgoBinding
        self.algo_binding = DomAlgoBinding(self)
        self.style_baselines = [(widget, widget.styleSheet()) for widget in (self, *self.findChildren(QWidget)) if widget.styleSheet()]

    def toggle_maximized(self):
        self.showNormal() if self.isMaximized() else self.showMaximized()

    def scaled(self, value):
        return round(value * self.zoom_percent / 100)

    def set_fills_enabled(self, enabled):
        self.model.show_fills = enabled
        self.table.viewport().update()

    def set_cancel_policy(self):
        if self.controller.external and not self.controller.paper:
            return
        cap = self.cancel_policy.currentData()
        if cap is None:
            self.controller.ledger.cancel_caps.pop(self.instrument.key, None)
        else:
            self.controller.ledger.cancel_caps[self.instrument.key] = cap
        for window in self.controller.windows:
            if window is not self and window.instrument.key == self.instrument.key:
                window.cancel_policy.blockSignals(True)
                window.cancel_policy.setCurrentIndex(window.cancel_policy.findData(cap))
                window.cancel_policy.blockSignals(False)

    def set_ladder_step(self):
        try:
            step = self.instrument.ticks(self.ladder_step.cleanText())
        except (ValueError, ArithmeticError):
            self.ladder_step.setValue(float(self.instrument.tick * self.model.price_step))
            self.controller.log("价梯步长必须是最小变动价位的整数倍")
            return
        self.model.price_step = step
        self.model.dataChanged.emit(self.model.index(0, 0), self.model.index(self.model.rows - 1, 2))

    def refresh_controls(self, local):
        for widget in (*self.local_controls.values(), self.left_quantity, self.right_quantity, self.nearby_ticks, self.cancel_policy, self.order_type, self.net_mode, self.fok_checkbox):
            widget.setEnabled(local)
        if local:
            cap = self.controller.ledger.cancel_caps.get(self.instrument.key)
            self.cancel_policy.blockSignals(True)
            self.cancel_policy.setCurrentIndex(self.cancel_policy.findData(cap))
            self.cancel_policy.blockSignals(False)
        else:
            self.order_type.setCurrentIndex(self.order_type.findData("GFD"))
            self.net_mode.setChecked(False)
            self.counters.setText("C:—　T:—")
        self.algo_binding.refresh(local)

    def sync_fok_checkbox(self):
        self.fok_checkbox.blockSignals(True)
        self.fok_checkbox.setChecked(self.order_type.currentData() == "FOK")
        self.fok_checkbox.blockSignals(False)

    def refresh_market_stats(self, snapshot):
        if snapshot is None:
            self.market_stats.setText("总持仓 —\n增减仓 —")
            return
        previous = snapshot.pre_settlement
        if previous is not None and previous > 0:
            change = (self.instrument.tick * snapshot.last - previous) * 100 / previous
            self.summary.setText(f"{change:+.2f}%")
            self.summary.setToolTip(f"涨跌幅按昨结算价 {previous} 计算；最新价 {self.instrument.price(snapshot.last)}。原版分母口径待核对")
        else:
            self.summary.setText("涨跌幅 —")
            self.summary.setToolTip(f"昨结算价缺失；最新价 {self.instrument.price(snapshot.last)}")
        interest = snapshot.open_interest
        delta = interest - snapshot.pre_open_interest if interest is not None and snapshot.pre_open_interest is not None else None
        self.market_stats.setText(f"总持仓 {format(interest, 'f') if interest is not None else '—'}\n增减仓 {format(delta, '+f') if delta is not None else '—'}")

    def set_marks_enabled(self, enabled):
        self.model.filled_by_price = {}
        self.model.pending_by_price = {}
        if enabled:
            self.refresh_marks()
        self.model.dataChanged.emit(self.model.index(0, 0), self.model.index(self.model.rows - 1, 2))

    def refresh_marks(self):
        pending = {}
        filled = {}
        if self.marks_toggle.isChecked():
            key = self.instrument.key
            if self.controller.paper or (not self.controller.external and self.controller.offline_mode):
                for order in self.controller.ledger.orders.values():
                    if order.key != key:
                        continue
                    if order.status in ACTIVE:
                        pending[order.ticks] = pending.get(order.ticks, 0) + order.remaining
                for fill in self.controller.ledger.fills:
                    if fill.key == key:
                        filled[fill.ticks] = filled.get(fill.ticks, 0) + fill.quantity
            elif self.controller.external and self.controller.external.config.td_front:
                for identifier, row in self.controller.external.orders.items():
                    if f"{row.get('ExchangeID')}.{row.get('InstrumentID')}" != key:
                        continue
                    status = row.get("OrderStatus", "")
                    volume = int(row.get("VolumeTotalOriginal", 0) or 0)
                    traded = int(row.get("VolumeTraded", 0) or 0)
                    if status in {"1", "3", "a", "b"} and volume > traded:
                        try:
                            ticks = self.instrument.ticks(str(row.get("LimitPrice", "")))
                        except ValueError:
                            continue
                        pending[ticks] = pending.get(ticks, 0) + volume - traded
                for row in self.controller.external.trades.values():
                    if f"{row.get('ExchangeID')}.{row.get('InstrumentID')}" != key:
                        continue
                    try:
                        ticks = self.instrument.ticks(str(row.get("Price", "")))
                        quantity = int(row.get("Volume", 0))
                    except (ValueError, TypeError, ArithmeticError):
                        continue
                    if quantity > 0:
                        filled[ticks] = filled.get(ticks, 0) + quantity
        key = self.instrument.key
        if self.controller.paper or (not self.controller.external and self.controller.offline_mode):
            orders = [order for order in self.controller.ledger.orders.values() if order.key == key]
            canceled = sum(order.status.value == "已撤单" for order in orders)
            traded = len({fill.order_id for fill in self.controller.ledger.fills if fill.key == key})
            self.counters.setText(f"C:{canceled}　T:{traded}")
        elif self.controller.external and self.controller.external.config.td_front:
            rows = [row for row in self.controller.external.orders.values() if f"{row.get('ExchangeID')}.{row.get('InstrumentID')}" == key]
            self.counters.setText(f"C:{sum(row.get('OrderStatus') == '5' for row in rows)}　T:{sum(int(row.get('VolumeTraded', 0) or 0) > 0 for row in rows)}")
        changed = self.model.pending_by_price != pending or self.model.filled_by_price != filled
        self.model.pending_by_price = pending
        self.model.filled_by_price = filled
        if changed:
            self.model.dataChanged.emit(self.model.index(0, 0), self.model.index(self.model.rows - 1, 2))

    def set_zoom(self, percent, resize=True):
        if type(percent) is not int:
            raise ValueError("显示比例须为整数")
        percent = min(200, max(75, percent))
        previous = self.zoom_percent
        self.zoom_percent = percent
        for widget, style in self.style_baselines:
            widget.setStyleSheet(re.sub(r"(\d+(?:\.\d+)?)px", lambda match: f"{float(match[1]) * percent / 100:g}px", style))
        self.sidebar.setFixedWidth(self.scaled(148))
        self.sidebar_scroll.setFixedWidth(self.scaled(156))
        self.side_column.setFixedWidth(self.scaled(156))
        self.topbar.setFixedHeight(self.scaled(22))
        for button in self.reference_title_buttons:
            button.setFixedWidth(self.scaled(21))
        self.time_label.setFixedHeight(self.scaled(26))
        for label in (self.volume_stat, self.interest_stat, self.interest_delta_stat):
            label.setFixedHeight(self.scaled(24))
        for field in (self.left_quantity, self.right_quantity):
            field.setFixedHeight(self.scaled(40))
        self.banner.setFixedHeight(self.scaled(32 if self.model.real_mode else 19))
        self.summary.setFixedHeight(self.scaled(34))
        self.long_position.setFixedHeight(self.scaled(37))
        self.short_position.setFixedHeight(self.scaled(37))
        self.original_marker.setFixedHeight(self.scaled(72))
        self.quote.setMinimumHeight(self.scaled(52))
        self.position.setMinimumHeight(self.scaled(46))
        self.setMinimumWidth(self.scaled(320))
        height = self.scaled(self.controller.config_store.effective().row_height)
        self.model.font_size = self.scaled(11)
        self.model.row_height = height
        self.table.verticalHeader().setMinimumSectionSize(height)
        self.table.verticalHeader().setDefaultSectionSize(height)
        self.table.setColumnWidth(1, self.scaled(65))
        if resize and percent != previous and not self.isMaximized():
            available = self.screen().availableGeometry()
            self.resize(min(round(self.width() * percent / previous), available.width()), min(round(self.height() * percent / previous), max(self.minimumHeight(), available.height() - 80)))
        self.model.beginResetModel()
        self.model.rows = max(15, self.table.viewport().height() // height)
        self.model.endResetModel()
        self.maximize_button.setToolTip(f"当前显示比例 {percent}%；可拖动窗口边缘改变尺寸")

    def refresh_disconnected(self):
        self.refresh_controls(False)
        self.refresh_market_stats(None)
        self.paper_button.hide()
        self.click_mode.setEnabled(False)
        self.model.real_mode = True
        self.model.snapshot = None
        self.model.dataChanged.emit(self.model.index(0, 0), self.model.index(self.model.rows - 1, 2))
        self.banner.setText("已断开")
        self.summary.setText("—")
        self.quote.setText("等待连接\n行情已清空")
        self.long_position.setText("多 —")
        self.short_position.setText("空 —")
        self.position.setText("持仓待核对")
        self.notice.setText("重连后先查询对账")
        self.footer.setText("已断开 · 外部委托可能仍有效")
        self.mode_label.setText("已断开")
        self.time_label.setText("--:--:--")
        for button in (*self.order_buttons, self.cancel_button):
            button.setEnabled(False)
        from .dom_reference_ui import refresh_reference_labels
        refresh_reference_labels(self, None)

    def select_price(self, index):
        ticks = self.model.price_at(index.row())
        if ticks <= 0:
            return
        self.price.setValue(float(self.instrument.tick * ticks))

    def price_action(self, index, right=False):
        if not index.isValid():
            return
        self.select_price(index)
        mode = self.click_mode.currentData()
        if mode == "select" or not (self.controller.paper or (self.controller.offline_mode and not self.controller.external)):
            return
        if mode.startswith("regions"):
            color = self.model.price_color(self.model.price_at(index.row()))
            if color == LEGACY_PALETTE["ask_price"]:
                side = "卖"
            elif color == LEGACY_PALETTE["bid_price"]:
                side = "买"
            else:
                self.controller.log("灰色无人报价区仅选价，不按红蓝区域提交模拟委托")
                return
        elif mode.startswith("columns"):
            if right or index.column() == 1:
                return
            side = "买" if index.column() == 0 else "卖"
        else:
            side = "卖" if right else "买"
        offset = "开仓" if mode.endswith("open") else "平仓"
        quantity = self.right_quantity.value() if right else self.left_quantity.value()
        if quantity == 0:
            self.controller.log("右键挂单已禁用；请明确设置右键手数后使用")
            return
        button = self.order_buttons[(0 if offset == "开仓" else 2) + (0 if side == "买" else 1)]
        if not button.isEnabled():
            self.controller.log(button.toolTip() or "当前模拟交易未就绪")
            return
        self.submit(side, offset, quantity=quantity)

    def set_follow(self, value):
        self.model.follow = value
        if value and self.model.snapshot:
            self.model.update_snapshot(self.model.snapshot)

    def recenter(self):
        self.follow.setChecked(True)
        self.set_follow(True)

    def submit(self, side, offset, quantity=None):
        created = []
        try:
            if self.algo_binding.active():
                self.algo_binding.submit(side, offset, self.quantity.value() if quantity is None else quantity)
                self.controller.refresh_records()
                return
            ticks = self.instrument.ticks(self.price.cleanText())
            quantity = self.quantity.value() if quantity is None else quantity
            if self.controller.paper or (not self.controller.external and self.controller.offline_mode):
                snapshot = self.controller.paper.check_ready(self.instrument.key) if self.controller.paper else self.controller.simulator.snapshots.get(self.instrument.key)
                plan = local_net_plan(self.controller.ledger, self.instrument.key, side, quantity, self.group.value()) if offset == "开仓" and self.net_mode.isChecked() and not self.controls.only_open else (OffsetLeg(offset, quantity),)
                for leg in plan:
                    self.controls.validate(snapshot, side, leg.offset, ticks, self.controller.ledger.orders.values())
                sender = self.controller.paper or self.controller.ledger
                for leg in plan:
                    created.append(sender.submit(self.instrument.key, side, leg.offset, ticks, leg.quantity, self.group.value(), order_type=self.order_type.currentData(), after_order=created[-1].id if created else ""))
                for order in created:
                    self.controller.log(f"{order.id} 本地模拟 {order.side}{order.offset} {order.quantity}手 {order.order_type}，等待新报价回报")
            elif self.controller.external:
                name = self.close_offset.currentText() if offset == "平仓" else offset
                reference = self.controller.external.submit(self.instrument.key, side, name, ticks, quantity, self.group.value())
                self.controller.log(f"{reference} 外部委托已提交，等待接口回报")
            else:
                raise ValueError("行情与交易已断开；请选择离线模拟或重连接口")
            self.controller.refresh_records()
        except (ValueError, ArithmeticError) as error:
            for order in created:
                self.controller.ledger.orders.pop(order.id, None)
            if self.controller.paper:
                self.controller.paper.sync_references()
            QMessageBox.warning(self, "委托未提交", str(error))

    def refresh(self, snapshot):
        if self.controller.external:
            self.refresh_external(snapshot)
            from .dom_reference_ui import refresh_reference_labels
            refresh_reference_labels(self, snapshot)
            return
        self.model.real_mode = False
        self.refresh_controls(True)
        self.paper_button.hide()
        self.click_mode.setEnabled(True)
        self.banner.setFixedHeight(self.scaled(19))
        for button in (*self.order_buttons, self.cancel_button):
            button.setEnabled(True)
        self.close_offset.setEnabled(True)
        self.model.update_snapshot(snapshot)
        self.refresh_marks()
        self.follow.blockSignals(True)
        self.follow.setChecked(self.model.follow)
        self.follow.blockSignals(False)
        self.banner.setText(snapshot.time)
        self.banner.setToolTip(f"{snapshot.key} · 离线生成行情")
        self.time_label.setText(snapshot.time)
        self.mode_label.setText("离线模拟")
        self.summary.setText(self.instrument.price(snapshot.last))
        self.summary.setToolTip("模拟最新价；原版百分比字段口径待确认")
        self.refresh_market_stats(snapshot)
        self.quote.setText(f"量 {snapshot.volume}\n深度 {self.instrument.depth} 档\n价位 {self.instrument.tick}")
        positions = self.controller.ledger.positions
        long = positions.get((snapshot.key, "买"))
        short = positions.get((snapshot.key, "卖"))
        self.long_position.setText(f"多 {long.quantity if long else 0}")
        self.short_position.setText(f"空 {short.quantity if short else 0}")
        self.position.setText(f"{long.quantity if long else 0}\n模拟持仓\n{short.quantity if short else 0}")
        from .dom_reference_ui import refresh_reference_labels
        refresh_reference_labels(self, snapshot)

    def refresh_external(self, snapshot):
        self.refresh_controls(bool(self.controller.paper))
        was_real = self.model.real_mode
        self.model.real_mode = True
        self.banner.setFixedHeight(self.scaled(32))
        session = self.controller.external
        self.click_mode.setEnabled(bool(self.controller.paper))
        self.paper_button.setVisible(not self.controller.paper and not session.config.td_front)
        self.paper_button.setEnabled(not session.faulted and not self.controller.gateway.closing)
        instrument = session.catalog.get(self.instrument.key, self.instrument)
        if instrument != self.instrument:
            tick_changed = instrument.tick != self.instrument.tick
            self.instrument = instrument
            self.model.instrument = instrument
            self.contract_label.setText(instrument.key)
            self.price.setDecimals(max(0, -instrument.tick.as_tuple().exponent))
            self.price.setSingleStep(float(instrument.tick))
            if tick_changed:
                self.model.price_step = 1
                self.ladder_step.setDecimals(max(0, -instrument.tick.as_tuple().exponent))
                self.ladder_step.setRange(float(instrument.tick), float(instrument.tick * 1000))
                self.ladder_step.setSingleStep(float(instrument.tick))
                self.ladder_step.setValue(float(instrument.tick))
        self.quantity.setMaximum(self.controller.ledger.max_order if self.controller.paper else session.config.max_order)
        self.left_quantity.setMaximum(self.quantity.maximum())
        self.right_quantity.setMaximum(self.quantity.maximum())
        self.setWindowTitle(f"{self.instrument.key} [{self.controller.mode_name()}]")
        self.mode_label.setText(self.controller.mode_name())
        if snapshot:
            if self.model.snapshot is None:
                self.price.setValue(float(instrument.tick * snapshot.last))
            self.model.update_snapshot(snapshot)
            self.refresh_marks()
            self.follow.blockSignals(True)
            self.follow.setChecked(self.model.follow)
            self.follow.blockSignals(False)
            stale = time.monotonic() - snapshot.received_monotonic > session.config.stale_seconds
            self.banner.setText(f"{'行情过期' if stale else '真实行情'}\n{snapshot.time}")
            self.banner.setToolTip(f"{snapshot.key} · 交易日 {snapshot.trading_day}")
            if stale:
                self.banner.setToolTip(f"{snapshot.key} · 超过{session.config.stale_seconds}秒未收到新报价；可能休市或连接异常，请查看连接状态。最后行情时间 {snapshot.time}")
            self.time_label.setText(snapshot.time)
            self.summary.setText(instrument.price(snapshot.last))
            self.summary.setToolTip("真实最新价；原版百分比字段口径待确认")
            self.quote.setText(f"量 {snapshot.volume}\n深度 {len(snapshot.bids)}/{len(snapshot.asks)}\n价位 {instrument.tick}")
        else:
            if self.model.snapshot is not None or not was_real:
                self.model.snapshot = None
                self.model.dataChanged.emit(self.model.index(0, 0), self.model.index(self.model.rows - 1, 2))
            self.banner.setText("等待有效真实行情")
            self.banner.setToolTip(instrument.key)
            self.time_label.setText("--:--:--")
            self.summary.setText(instrument.name)
            self.quote.setText("尚无有效报价")
        self.refresh_market_stats(snapshot)
        if snapshot is None:
            self.refresh_marks()
        self.close_offset.setEnabled(not self.controller.paper)
        if self.controller.paper:
            self.close_offset.setCurrentIndex(0)
            try:
                self.controller.paper.check_ready(instrument.key)
                ready, reason = True, "只提交本地模拟委托，不发送外部委托"
            except ValueError as error:
                ready, reason = False, str(error)
            for button in self.order_buttons:
                button.setEnabled(ready and not self.controller.ledger.trading_error)
                button.setToolTip(reason)
            self.cancel_button.setEnabled(True)
            self.cancel_button.setToolTip("撤销本合约的本地模拟挂单；空格仅撤当前组")
            positions = self.controller.ledger.positions
            long = positions.get((instrument.key, "买"))
            short = positions.get((instrument.key, "卖"))
            self.long_position.setText(f"多 {long.quantity if long else 0}")
            self.short_position.setText(f"空 {short.quantity if short else 0}")
            self.position.setText(f"{long.quantity if long else 0}\n模拟持仓\n{short.quantity if short else 0}")
            self.notice.setText("真实行情 · 本地成交\n不含手续费/保证金\n空格撤本窗当前组\nF1–F10切组")
            self.footer.setText("在线模拟" if ready else f"在线模拟\n{reason}")
            return
        connected = bool(session.config.td_front)
        tradable = connected and session.config.environment == "simulation" and session.config.allow_orders and session.logged_in and session.settled and session.ready and not session.faulted
        for button in self.order_buttons:
            button.setEnabled(tradable and session.quote_fresh(instrument.key))
            button.setToolTip("须启用外部模拟报单并完成结算、对账和有效行情检查" if connected else "仅看行情，未连接交易账户，不能下单")
        self.cancel_button.setEnabled(connected and session.config.allow_orders and session.logged_in and session.settled and not session.faulted)
        if not connected:
            self.long_position.setText("多 —")
            self.short_position.setText("空 —")
            self.position.setText("—\n未查询持仓\n—")
            self.notice.setText("仅看行情\n未连接交易账户\n不能下单")
            self.footer.setText("仅看行情")
            return
        rows = [r for r in session.positions if f"{r.get('ExchangeID')}.{r.get('InstrumentID')}" == instrument.key]
        long = sum(r['Position'] for r in rows if r['PosiDirection'] == '2')
        short = sum(r['Position'] for r in rows if r['PosiDirection'] == '3')
        self.long_position.setText(f"多 {long}")
        self.short_position.setText(f"空 {short}")
        self.position.setText(f"{long}\n接口持仓\n{short}")
        self.notice.setText("接口回报驱动\n仅投机期货限价单\n未核验合约禁止交易")
        self.footer.setText(f"{self.controller.mode_name()} · {session.td_status}")

    def closeEvent(self, event):
        event.accept()
        if self.closing:
            return
        self.closing = True
        if not self.controller.shutting_down:
            if self in self.controller.windows:
                self.controller.windows.remove(self)
            if self.controller.external and self.controller.gateway and not self.controller.gateway.closing:
                try:
                    self.controller.external.release(self.instrument.key)
                except ValueError as error:
                    self.controller.log(f"窗口已关闭；退订失败：{error}")
            self.controller.dom_closed(self.instrument.key)
