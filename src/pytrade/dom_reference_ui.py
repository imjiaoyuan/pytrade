import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QButtonGroup, QCheckBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QRadioButton, QScrollArea, QVBoxLayout, QWidget


class ReferenceTools(QWidget):
    def closeEvent(self, event):
        self.parentWidget().tools_toggle.setChecked(False)
        event.accept()


def restore_reference_layout(window):
    old_sidebar = window.sidebar_scroll.takeWidget()
    old_sidebar.setParent(window)
    window.side_column.layout().removeWidget(window.order_panel)
    window.topbar.layout().removeWidget(window.mode_label)
    window.topbar.layout().removeWidget(window.time_label)
    window.mode_label.setParent(old_sidebar)
    old_sidebar.layout().insertWidget(0, window.mode_label)
    window.time_label.setParent(window)
    for button in window.topbar.findChildren(QPushButton):
        button.hide()
    window.reference_title_buttons = []
    for text, tip, action in (
        ("−", "缩小价梯显示间隔，不改变交易最小价位", lambda: change_step(window, -1)),
        ("+", "增大价梯显示间隔，不改变交易最小价位", lambda: change_step(window, 1)),
        ("□", "最大化 / 还原", window.toggle_maximized),
        ("×", "关闭本合约，不撤销外部委托", window.close),
    ):
        button = QPushButton(text)
        button.setFixedWidth(21)
        button.setToolTip(tip)
        button.clicked.connect(action)
        window.topbar.layout().addWidget(button)
        window.reference_title_buttons.append(button)
    sidebar = QWidget()
    side = QVBoxLayout(sidebar)
    side.setContentsMargins(2, 1, 2, 1)
    side.setSpacing(3)
    window.sidebar = sidebar
    window.sidebar_scroll.setWidget(sidebar)
    window.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    window.time_label.setFixedHeight(26)
    window.time_label.setStyleSheet("background:#a4a4a4;color:#ff3434;font-size:22px;font-weight:bold")
    side.addWidget(window.time_label)
    window.summary.setFixedHeight(34)
    window.summary.setStyleSheet("background:#ff5156;color:black;font-size:26px;font-weight:bold")
    side.addWidget(window.summary)
    window.volume_stat = stat_label(side, "—", "当日总成交量；不补造缺失数据")
    window.interest_stat = stat_label(side, "—", "当前市场总持仓量 OpenInterest")
    window.interest_delta_stat = stat_label(side, "—", "当日增减仓：OpenInterest − PreOpenInterest")
    for label, color in ((window.short_position, "#ff4c50"), (window.long_position, "#4a45ff")):
        label.setStyleSheet(f"background:{color};color:#000000;font-size:25px;font-weight:bold")
        label.setFixedHeight(37)
        side.addWidget(label)
    for field in (window.left_quantity, window.right_quantity):
        field.setPrefix("")
        field.setFixedHeight(40)
        field.setStyleSheet("background:white;color:black;font-size:25px")
        side.addWidget(field)
    window.original_modes = []
    for radio in old_sidebar.findChildren(QRadioButton):
        if radio.text() in {"A", "B"}:
            radio.setText(f"ChgOrder({radio.text()})")
            window.original_modes.append(radio)
            side.addWidget(radio)
    if window.original_modes:
        window.original_modes[0].setChecked(True)
    window.local_controls["nearby"].setText("Chg Nearby")
    side.addWidget(window.local_controls["nearby"])
    side.addWidget(window.spread_checkbox)
    parameters = QGridLayout()
    parameters.setContentsMargins(0, 0, 0, 0)
    for row, (name, field) in enumerate(zip(("", "Pt", "Fctr"), window.spread_fields)):
        parameters.addWidget(QLabel(name), row, 0)
        parameters.addWidget(field, row, 1)
    side.addLayout(parameters)
    window.cancel_radios = QButtonGroup(window)
    for name, cap in (("Cancel 485", 485), ("Cancel 4K", 4000), ("NoCancel", 0)):
        radio = QRadioButton(name)
        radio.setStyleSheet("color:#800030")
        radio.setToolTip(window.cancel_policy.toolTip())
        window.cancel_radios.addButton(radio, cap)
        radio.clicked.connect(lambda checked=False, value=cap: window.cancel_policy.setCurrentIndex(window.cancel_policy.findData(value)))
        side.addWidget(radio)
    window.cancel_policy.currentIndexChanged.connect(lambda: sync_cancel_radios(window))
    window.local_controls["only_open"].setText("OnlyOpen")
    side.addWidget(window.local_controls["only_open"])
    for box in old_sidebar.findChildren(QCheckBox):
        if box.text() == "M-OrderX":
            box.setStyleSheet("color:#d225d2")
            side.addWidget(box)
    for name in ("limit_lock", "white_lock"):
        side.addWidget(window.local_controls[name])
    side.addWidget(window.fills_toggle)
    group = QHBoxLayout()
    label = QLabel("合约分组:")
    label.setStyleSheet("color:#148f8d;font-size:15px")
    group.addWidget(label)
    window.group.setPrefix("")
    window.group.setStyleSheet("background:#f27aea;font-size:17px;font-weight:bold")
    group.addWidget(window.group)
    side.addLayout(group)
    label = QLabel("价格精度")
    label.setStyleSheet("color:#943c8d;font-size:15px")
    side.addWidget(label)
    precision = QHBoxLayout()
    window.ladder_step.setPrefix("")
    window.ladder_step.setMinimumWidth(0)
    precision.addWidget(window.ladder_step, 1)
    apply_step = QPushButton("改")
    apply_step.setFixedWidth(30)
    apply_step.clicked.connect(window.set_ladder_step)
    precision.addWidget(apply_step)
    side.addLayout(precision)
    side.addSpacing(14)
    window.cancel_usage = QLabel("—")
    window.cancel_usage.setStyleSheet("color:#903268;font-size:15px")
    window.cancel_usage.setToolTip("本合约当日本地手动撤单次数 / 本地上限；沿用总风控时上限显示 —。下方 C 另计本会话已撤委托")
    side.addWidget(window.cancel_usage)
    window.counter_display = QLabel("C:0　　 T:0")
    window.counter_display.setStyleSheet("font-size:14px")
    window.counter_display.setToolTip(window.counters.toolTip())
    side.addWidget(window.counter_display)
    side.addSpacing(22)
    window.net_position_display = QLabel("0")
    window.net_position_display.setAlignment(Qt.AlignmentFlag.AlignCenter)
    window.net_position_display.setStyleSheet("color:#8983bd;font-size:39px")
    side.addWidget(window.net_position_display)
    window.original_marker.setFixedHeight(72)
    window.original_marker.setStyleSheet("color:#800040;font-family:serif;font-size:65px")
    window.original_marker.setToolTip("开平模式标识：P 表示允许开平，O 表示仅开仓；本地模拟规则，原版切换规则尚未验证")
    window.local_controls["only_open"].toggled.connect(lambda checked: window.original_marker.setText("O" if checked else "P"))
    side.addWidget(window.original_marker)
    side.addStretch(1)
    window.reference_tools = ReferenceTools(window, Qt.WindowType.Tool)
    window.reference_tools.setWindowTitle(f"{window.instrument.key} · 下单与更多工具")
    window.reference_tools.resize(340, 650)
    tools_layout = QVBoxLayout(window.reference_tools)
    tools_layout.setContentsMargins(0, 0, 0, 0)
    tools_scroll = QScrollArea()
    tools_scroll.setWidgetResizable(True)
    content = QWidget()
    content_layout = QHBoxLayout(content)
    content_layout.addWidget(old_sidebar)
    content_layout.addWidget(window.order_panel)
    tools_scroll.setWidget(content)
    tools_layout.addWidget(tools_scroll)
    for widget in (window.position, window.counters, window.market_stats):
        widget.hide()
    for label in old_sidebar.findChildren(QLabel):
        if label.text() in {"Pt", "Fct"}:
            label.hide()
    window.tools_toggle = QPushButton("下单 / 更多工具 ▸")
    window.tools_toggle.setCheckable(True)
    window.tools_toggle.clicked.connect(lambda checked: toggle_tools(window, checked))
    window.side_column.layout().addWidget(window.tools_toggle)
    window.reference_tools.hide()
    window.sidebar.setFixedWidth(148)
    window.sidebar_scroll.setFixedWidth(156)
    window.side_column.setFixedWidth(156)
    window.topbar.layout().setContentsMargins(2, 0, 2, 0)
    window.topbar.layout().setSpacing(1)
    window.table.setColumnWidth(1, 65)
    window.table.setToolTip("红色：卖一及以上；蓝色：买一及以下；灰色：中间无报价区。黑线标记最新成交价。行情缺失不补档；点击下单须在更多工具中明确选择模式。红蓝区域按用户标注实现，原版黑线定位规则仍待核验。")
    window.click_mode.setToolTip("原图区域模式仅本地模拟：红区卖、蓝区买、灰区只选价；开/平明确选择。鼠标左/右键分别使用各自数量，右键默认禁用。外部账户不启用鼠标报单。")
    window.resize(348, window.height())
    window.setMinimumWidth(320)


def toggle_tools(window, checked):
    window.reference_tools.setVisible(checked)
    window.tools_toggle.setText("下单 / 更多工具 ▾" if checked else "下单 / 更多工具 ▸")


def stat_label(layout, text, tooltip):
    label = QLabel(text)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setFixedHeight(24)
    label.setStyleSheet("background:#a4a4a4;color:black;font-size:21px;font-weight:bold")
    label.setToolTip(tooltip)
    layout.addWidget(label)
    return label


def change_step(window, direction):
    value = max(1, min(1000, window.model.price_step + direction))
    window.ladder_step.setValue(float(window.instrument.tick * value))
    window.set_ladder_step()


def sync_cancel_radios(window):
    cap = window.cancel_policy.currentData()
    window.cancel_radios.setExclusive(False)
    for button in window.cancel_radios.buttons():
        button.setEnabled(window.cancel_policy.isEnabled())
        button.setAutoExclusive(False)
        button.setChecked(window.cancel_radios.id(button) == cap)
        button.setAutoExclusive(True)
    window.cancel_radios.setExclusive(True)


def refresh_reference_labels(window, snapshot):
    if snapshot:
        window.volume_stat.setText(str(snapshot.volume))
        interest = snapshot.open_interest
        delta = interest - snapshot.pre_open_interest if interest is not None and snapshot.pre_open_interest is not None else None
        window.interest_stat.setText(format(interest, 'f') if interest is not None else "—")
        window.interest_delta_stat.setText(format(delta, '+f') if delta is not None else "—")
    else:
        for label in (window.volume_stat, window.interest_stat, window.interest_delta_stat):
            label.setText("—")
    text = window.position.text()
    numbers = re.findall(r"\d+", text)
    window.net_position_display.setText(str(int(numbers[0]) - int(numbers[-1])) if len(numbers) >= 2 and "—" not in text and "待核对" not in text else "—")
    window.net_position_display.setToolTip(text + "\n显示净持仓，多减空；不表示总持仓为零")
    counts = re.findall(r"\d+", window.counters.text())
    window.counter_display.setText(f'<span style="color:#903268">C:{counts[0] if counts else "—"}</span>　 <span style="color:#26934a">T:{counts[1] if len(counts) > 1 else "—"}</span>')
    cap = window.cancel_policy.currentData()
    local = window.controller.paper or (not window.controller.external and window.controller.offline_mode)
    used = window.controller.ledger.cancel_counts.get(window.instrument.key, 0) if local else "—"
    window.cancel_usage.setText(f"{used}/{cap if cap is not None else '—'}")
    sync_cancel_radios(window)
