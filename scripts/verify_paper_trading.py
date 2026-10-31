import argparse
import json
import os
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("请使用新文件名保留历史证据")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtCore import QObject, QTimer, Qt, Signal, qVersion
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from pytrade.domain import Status
    from pytrade.flow import ApplicationFlow

    class SyntheticGateway(QObject):
        event = Signal(dict)
        stopped = Signal()

        def __init__(self, config, state_dir, parent=None):
            super().__init__(parent)
            self.config = config
            self.closing = False
            self.commands = []

        def send(self, command):
            self.commands.append(command)

        def close(self):
            self.closing = True
            QTimer.singleShot(0, self.stopped.emit)

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    checks = {}
    with TemporaryDirectory(prefix="pytrade-paper-acceptance-") as directory, patch("pytrade.app.CtpGateway", SyntheticGateway):
        flow = ApplicationFlow(Path(directory))
        flow.show()
        startup = flow.startup
        startup.mode.setCurrentIndex(startup.mode.findData("paper"))
        checks["requires_service_but_not_trade_account"] = not startup.login_button.isEnabled()
        startup.services.selectRow(1)
        if args.screenshots:
            args.screenshots.mkdir(parents=True, exist_ok=True)
            startup.grab().save(str(args.screenshots / "startup.png"))
        startup.login_button.click()
        controller = flow.controller
        controller.timer.stop()
        gateway = controller.gateway
        sequence = controller.simulator.sequence
        gateway.event.emit(dict(kind="md_state", epoch=1, status="登录成功（验证用合成网关，无网络）"))
        window = controller.open_dom("SHFE.au2612")
        gateway.event.emit(dict(kind="subscription", epoch=1, key=window.instrument.key, error=0))

        def quote():
            gateway.event.emit(dict(kind="quote", epoch=1, key=window.instrument.key, received=time.monotonic(), row=dict(LastPrice="916.66", BidPrice1="916.64", BidVolume1=3, AskPrice1="916.68", AskVolume1=1, AskPrice2="916.70", AskVolume2=3, UpperLimitPrice="1000", LowerLimitPrice="800", TradingDay="20261030", UpdateTime="10:00:02", Volume=100)))

        quote()
        controller.advance()
        window.price.setValue(916.70)
        window.quantity.setValue(3)
        window.submit("买", "开仓")
        opening = next(iter(controller.ledger.orders.values()))
        checks["submit_does_not_fill"] = opening.status == Status.PENDING and opening.filled == 0
        quote()
        checks["first_new_quote_acknowledges_only"] = opening.status == Status.WORKING and opening.filled == 0
        quote()
        controller.advance()
        checks["new_quotes_fill_local_orders"] = opening.status == Status.FILLED and controller.ledger.positions[(window.instrument.key, "买")].quantity == 3
        own = controller.paper.submit(window.instrument.key, "买", "开仓", 45000, 1, 1)
        other = controller.paper.submit(window.instrument.key, "买", "开仓", 45000, 1, 2)
        window.activateWindow()
        app.processEvents()
        QTest.keyClick(window, Qt.Key.Key_Space)
        checks["space_only_cancels_current_group"] = own.status == Status.CANCELED and other.status == Status.PENDING
        second = controller.open_dom(window.instrument.key, group=2)
        QTest.keyClick(window, Qt.Key.Key_F2)
        checks["f2_switches_group"] = controller.group.value() == 2 and second.isVisible() and not window.isVisible()
        QTest.keyClick(second, Qt.Key.Key_F1)
        checks["f1_restores_group"] = controller.group.value() == 1 and window.isVisible()
        window.price.setValue(916.64)
        window.submit("卖", "平仓")
        closing = list(controller.ledger.orders.values())[-1]
        quote()
        quote()
        controller.advance()
        checks["closing_and_precise_pnl"] = closing.status == Status.FILLED and controller.ledger.realized == -160
        if args.screenshots:
            window.setWindowTitle("合成回调验证 · 在线模拟 · 未联网")
            window.banner.setText("合成回调验证\n10:00:02")
            controller.state.setText("合成行情验证 · 在线模拟 · 未联网、无外部委托")
            app.processEvents()
            window.grab().save(str(args.screenshots / "paper_dom.png"))
            controller.grab().save(str(args.screenshots / "paper_workspace.png"))
        controller.disconnect_ctp()
        for _ in range(5):
            app.processEvents()
        checks["disconnect_cancels_local_working_orders"] = other.status == Status.CANCELED and controller.external is None
        checks["no_generated_market"] = controller.simulator.sequence == sequence and not controller.simulator.snapshots
        checks["no_trader_interface_or_commands"] = not gateway.config.td_front and not gateway.config.allow_orders and all(command["command"] == "subscriptions" for command in gateway.commands)
        checks["no_trade_credentials"] = not any((gateway.config.user, gateway.config.password, gateway.config.auth_code, gateway.config.md_user, gateway.config.md_password))
        report = dict(generated_at_utc=datetime.now(timezone.utc).isoformat(), source="synthetic callbacks through the application flow; no network, credentials, native API or external orders", environment=dict(system=platform.platform(), python=platform.python_version(), qt=qVersion(), platform=app.platformName(), dpi=app.primaryScreen().logicalDotsPerInch()), local_orders=len(controller.ledger.orders), local_fills=len(controller.ledger.fills), realized_pnl=str(controller.ledger.realized), outgoing_command_kinds=sorted({command["command"] for command in gateway.commands}), checks=checks, ok=all(checks.values()), limitations=["合成回调验证不代替交易时段真实行情接入", "本地成交不模拟交易所排队、手续费或保证金", "不是外部模拟账户交易验收"])
        flow.quit()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
