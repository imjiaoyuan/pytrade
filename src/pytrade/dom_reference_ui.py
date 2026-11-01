import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QLabel, QRadioButton, QWidget


def restore_reference_layout(window):
    side = window.sidebar.layout()
    window.original_modes = [button for button in window.sidebar.findChildren(QRadioButton) if button.text() in {"A", "B"}]
    window.original_modes[0].setChecked(True)
    window.contract_tick = QLabel(f"最小变动价格 {window.instrument.tick}")
    window.contract_tick.setAlignment(Qt.AlignmentFlag.AlignCenter)
    window.contract_tick.setFixedHeight(20)
    window.contract_tick.setStyleSheet("font-size:11px;font-weight:bold")
    window.contract_tick.setToolTip("合约元数据的最小变动价格；与价梯显示步长独立，每手每 tick 金额另乘合约乘数")
    side.insertWidget(side.indexOf(window.market_stats) + 1, window.contract_tick)
    window.cancel_usage = QLabel("手动撤单：0 / —")
    window.cancel_usage.setToolTip("本合约当日本地成功手动撤单次数 / 本地上限；不同于底部含自动撤单的会话统计")
    side.insertWidget(side.indexOf(window.counters) + 1, window.cancel_usage)
    window.net_position_display = QLabel("净持仓 0")
    window.net_position_display.setAlignment(Qt.AlignmentFlag.AlignCenter)
    side.insertWidget(side.indexOf(window.position) + 1, window.net_position_display)
    window.original_marker.setToolTip("P 表示允许开平，O 表示仅开仓；本地模拟规则")
    window.local_controls["only_open"].toggled.connect(lambda checked: window.original_marker.setText("O" if checked else "P"))
    window.table.setToolTip("左红买量、右绿卖量、中央价格列，黄色标记最新价。盘口缺失不补造数量。单击选价；鼠标报单须明确选择本地模拟开/平模式。")
    footer = QWidget()
    footer.setStyleSheet("background:#e6e9ed;font-size:11px")
    grid = QGridLayout(footer)
    grid.setContentsMargins(3, 2, 3, 2)
    grid.setSpacing(2)
    window.contract_total_pnl = QLabel("总盈亏：—")
    window.contract_floating_pnl = QLabel("持仓浮盈亏：—")
    window.contract_cancels = QLabel("撤单：—")
    for label, row, column in ((window.contract_total_pnl, 0, 0), (window.contract_floating_pnl, 0, 1)):
        label.setMinimumWidth(0)
        label.setWordWrap(True)
        grid.addWidget(label, row, column)
    window.contract_total_pnl.setToolTip("本合约所有分组：已实现平仓盈亏 + 当前持仓浮盈亏，单位元；本地模拟仅本次会话，不含手续费。外部账户合约级核算尚未接入，显示 —")
    window.contract_floating_pnl.setToolTip("本合约所有分组的多空持仓，按最新价相对开仓成本计算，乘合约乘数，单位元；无持仓为 0。行情缺失不估算；外部账户暂显示 —")
    grid.addWidget(window.contract_cancels, 1, 0, 1, 2)
    window.contract_cancels.setToolTip("合约 / 品种：已确认撤单的委托笔数，含部分成交后撤余量及自动撤单，不计失败请求；跨所有分组。品种按交易所和代码字母前缀汇总各月份（含同前缀期权）。本地为本次会话，外部为已查询/回报记录，不是全市场撤单数")
    window.layout().addWidget(footer)


def change_step(window, direction):
    value = max(1, min(1000, window.model.price_step + direction))
    window.ladder_step.setValue(float(window.instrument.tick * value))
    window.set_ladder_step()


def refresh_reference_labels(window, snapshot):
    text = window.position.text()
    numbers = re.findall(r"\d+", text)
    net = str(int(numbers[0]) - int(numbers[-1])) if len(numbers) >= 2 and "—" not in text and "待核对" not in text else "—"
    window.net_position_display.setText(f"净持仓 {net}")
    window.net_position_display.setToolTip(text + "\n净持仓为多减空")
    local = window.controller.paper or (not window.controller.external and window.controller.offline_mode)
    used = window.controller.ledger.cancel_counts.get(window.instrument.key, 0) if local else "—"
    cap = window.cancel_policy.currentData()
    window.cancel_usage.setText(f"手动撤单：{used} / {cap if cap is not None else '—'}")
    refresh_contract_footer(window, snapshot)


def product_key(key):
    exchange, symbol = key.split(".", 1)
    match = re.match(r"[A-Za-z]+", symbol)
    return exchange, match.group().lower() if match else symbol


def refresh_contract_footer(window, snapshot):
    controller = window.controller
    key = window.instrument.key
    local = bool(controller.paper or (not controller.external and controller.offline_mode))
    total = floating = None
    canceled = None
    if local:
        total, floating = controller.ledger.contract_pnl(key, snapshot.last if snapshot else None)
        canceled = [order.key for order in controller.ledger.orders.values() if order.status.value == "已撤单"]
    elif controller.external and controller.external.config.td_front:
        canceled = [f"{row.get('ExchangeID')}.{row.get('InstrumentID')}" for row in controller.external.orders.values() if row.get("OrderStatus") == "5"]
    for label, title, value in ((window.contract_total_pnl, "总盈亏", total), (window.contract_floating_pnl, "持仓浮盈亏", floating)):
        color = "#ba182b" if value is not None and value > 0 else "#174fbb" if value is not None and value < 0 else "#333333"
        amount = f"{value:+.2f} 元" if value else "0.00 元" if value is not None else "—"
        label.setText(f'{title}：<span style="color:{color}">{amount}</span>')
    window.contract_tick.setText(f"最小变动价格 {window.instrument.tick}")
    counts = f"{canceled.count(key)} / {sum(product_key(item) == product_key(key) for item in canceled)}" if canceled is not None else "— / —"
    window.contract_cancels.setText(f"撤单 合约/品种：{counts}")
