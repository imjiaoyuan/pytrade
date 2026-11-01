from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QCompleter, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSpinBox


class DomAlgoBinding:
    def __init__(self, window):
        self.window = window
        self.controller = window.controller
        self.mode = "best"
        self.tasks = []
        self.enabled = QCheckBox("使用 A/B 算法挂单〔本地〕")
        self.enabled.setToolTip("选中后买开/卖开/买平/卖平及价梯下单创建算法任务；取消后恢复普通限价。已有任务须手动停止。")
        window.order_panel.layout().insertWidget(0, self.enabled)
        self.status = QLabel("普通限价")
        self.status.setWordWrap(True)
        self.status.setToolTip("本页采用 vn.py 算法：A=最优价，B=狙击，M-OrderX=条件触发，CntrbySprd=双腿价差。不是已验证的原EXE算法映射。")
        side = window.sidebar.layout()
        row = side.indexOf(window.spread_checkbox)
        side.insertWidget(row, self.status)
        self.stop_button = QPushButton("停止本窗策略")
        self.stop_button.clicked.connect(self.stop)
        side.insertWidget(row + 1, self.stop_button)
        for button, kind, name in zip(window.original_modes, ("best", "sniper"), ("最优价", "狙击")):
            button.setText("A 最优价" if kind == "best" else "B 狙击")
            button.setToolTip("本地模拟：跟随同侧最优价，变价先撤后挂，受选定限价约束" if kind == "best" else "本地模拟：对手价满足选定限价时按一档可见数量提交，未成挂单在后续报价撤销")
            button.clicked.connect(lambda checked=False, value=kind: self.choose(value))
        window.spread_checkbox.setToolTip("vn.py 双腿价差本地实现：本合约 A − Fctr × 腿 B；Pt 为触发阈值。买价差买A卖B，卖价差相反。先成交A再补B，非原版算法等价声明。")
        for field in window.spread_fields:
            field.setReadOnly(False)
        second, point, factor = window.spread_fields
        second.clear()
        second.setPlaceholderText("腿B：交易所.合约")
        keys = [key for key, instrument in self.controller.catalog.items() if instrument.product_class == "1" and key != window.instrument.key]
        completer = QCompleter(keys, second)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        second.setCompleter(completer)
        second.setToolTip("不同于当前合约的腿B，填写完整代码，例如 SHFE.ag2612；请先打开该腿DOM接收行情")
        point.setToolTip("Pt：A − Fctr × B 的触发阈值，可为负数；买价差在卖价≤Pt时触发，卖价差在买价≥Pt时触发")
        factor.setToolTip("Fctr：腿B价格系数，必须为正数；交易手数比例在下方下单区独立设置")
        self.ratio_a, self.ratio_b = QSpinBox(), QSpinBox()
        ratios = QHBoxLayout()
        for label, widget in (("腿A手数", self.ratio_a), ("腿B手数", self.ratio_b)):
            widget.setRange(1, self.controller.ledger.max_order)
            ratios.addWidget(QLabel(label))
            ratios.addWidget(widget)
        window.order_panel.layout().insertLayout(1, ratios)
        window.conditional_checkbox.setText("M-OrderX ·条件单")
        window.conditional_checkbox.setToolTip("vn.py Stop 本地实现：买入最新价≥选定价、卖出最新价≤选定价时，按对手价提交限价单。每个任务只触发一次；退出失效，非原版提前挂单算法。")
        window.spread_checkbox.toggled.connect(lambda checked: self.exclusive("spread", checked))
        window.conditional_checkbox.toggled.connect(lambda checked: self.exclusive("stop", checked))
        self.enabled.toggled.connect(lambda checked: self.exclusive("single", checked))
        self.refresh(bool(self.controller.paper or not self.controller.external and self.controller.offline_mode))

    def active(self):
        return self.enabled.isChecked() or self.window.spread_checkbox.isChecked() or self.window.conditional_checkbox.isChecked()

    def kind(self):
        return "spread" if self.window.spread_checkbox.isChecked() else "stop" if self.window.conditional_checkbox.isChecked() else self.mode

    def choose(self, kind):
        self.mode = kind
        self.enabled.setChecked(True)
        self.exclusive("single", True)

    def exclusive(self, source, checked):
        if checked:
            for name, widget in (("single", self.enabled), ("spread", self.window.spread_checkbox), ("stop", self.window.conditional_checkbox)):
                if name != source:
                    widget.setChecked(False)
            self.window.net_mode.setChecked(False)
            self.window.order_type.setCurrentIndex(self.window.order_type.findData("GFD"))
        self.update_status()

    def update_status(self):
        names = {"best": "A 最优价", "sniper": "B 狙击", "stop": "条件触发", "spread": "双腿价差"}
        running = sum(self.controller.local_algos.tasks[identifier].status == "运行" for identifier in self.tasks)
        self.status.setText(f"{names[self.kind()] if self.active() else '普通限价'} · 运行{running}")
        self.stop_button.setEnabled(bool(self.tasks))

    def refresh(self, local):
        for widget in (*self.window.original_modes, self.enabled, self.window.spread_checkbox, self.window.conditional_checkbox, *self.window.spread_fields, self.ratio_a, self.ratio_b):
            widget.setEnabled(local)
        if local:
            self.window.net_mode.setEnabled(not self.active())
            self.window.order_type.setEnabled(not self.active())
            self.window.fok_checkbox.setEnabled(not self.active())
        self.update_status()

    def submit(self, side, offset, quantity):
        window = self.window
        if not (self.controller.paper or not self.controller.external and self.controller.offline_mode):
            raise ValueError("DOM 算法仅用于本地模拟，不发送外部委托")
        if window.net_mode.isChecked() or window.order_type.currentData() != "GFD":
            raise ValueError("算法任务使用普通限价，不能同时选择先平后开或 FAK/FOK")
        if window.controls.only_open and offset != "开仓":
            raise ValueError("OnlyOpen 已开启，本窗仅允许开仓")
        kind = self.kind()
        second, point, factor = window.spread_fields
        limit = Decimal(point.text()) if kind == "spread" else Decimal(window.price.cleanText())
        controls = window.controls
        ledger = self.controller.ledger
        def guard(snapshot, direction, action, ticks):
            controls.validate(snapshot, direction, action, ticks, ledger.orders.values())
        task = self.controller.local_algos.start(kind, window.instrument.key, side, offset, quantity, limit, window.group.value(), second.text().strip() if kind == "spread" else "", Decimal(factor.text()) if kind == "spread" else Decimal(1), self.ratio_a.value(), self.ratio_b.value(), guard=guard)
        self.tasks.append(task.id)
        self.controller.log(f"{task.id} DOM {kind} 本地算法已启动；按新行情推进，不发送外部委托")
        self.update_status()

    def stop(self):
        for identifier in self.tasks:
            task = self.controller.local_algos.tasks[identifier]
            if task.status != "已完成":
                self.controller.local_algos.stop(identifier)
        self.controller.refresh_records()
        self.update_status()
        failures = [self.controller.local_algos.tasks[identifier].message for identifier in self.tasks if self.controller.local_algos.tasks[identifier].status == "停止失败"]
        if failures:
            QMessageBox.warning(self.window, "策略停止，部分撤单失败", "\n".join(failures))
