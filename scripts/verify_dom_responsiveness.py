import argparse
import json
import os
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class MarketOnlyGateway:
    closing = False

    def __init__(self):
        self.commands = []

    def send(self, command):
        self.commands.append(command)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--offscreen", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("请使用新文件名保留历史证据")
    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer, qVersion
    from PySide6.QtWidgets import QApplication
    from pytrade.app import Controller
    from pytrade.external import ConnectionConfig, ExternalSession, load_instruments

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    loop = QEventLoop()
    callback_errors = []
    previous_hook = sys.excepthook

    def record_error(kind, error, traceback):
        callback_errors.append(kind.__name__)
        previous_hook(kind, error, traceback)

    sys.excepthook = record_error
    samples, gaps = [], []
    keys = ("CZCE.MA701", "CZCE.FG701", "SHFE.ag2612", "SHFE.au2612", "SHFE.cu2612", "SHFE.rb2701", "SHFE.al2612", "DCE.m2701", "CZCE.SR701", "CFFEX.IF2612")
    root = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(prefix="pytrade-dom-response-") as directory:
        controller = Controller(Path(directory))
        controller.timer.stop()
        controller.catalog = load_instruments(root / "data/Instruments.snapshot.xml")
        gateway = MarketOnlyGateway()
        session = ExternalSession(ConnectionConfig(md_front="tcp://127.0.0.1:1"), gateway, controller.catalog)
        controller.gateway, controller.external, controller.offline_mode = gateway, session, False
        controller.show()
        controller.filter_contracts()
        session.handle(dict(kind="md_state", epoch=1, status="本地合成行情，不使用网络"))
        for key in keys:
            window = controller.open_dom(key)
            instrument = controller.catalog[key]
            ticks = 3480 if key == "CZCE.MA701" else 15000
            price = instrument.tick * ticks
            session.handle(dict(kind="subscription", epoch=1, key=key, error=0))
            session.handle(dict(kind="quote", epoch=1, key=key, received=time.monotonic() - 100, row=dict(LastPrice=str(price), BidPrice1=str(price - instrument.tick), BidVolume1=16, AskPrice1=str(price + instrument.tick), AskVolume1=211, Volume=3293327, TradingDay="20261030", UpdateTime="15:00:00")))
        controller.advance()
        app.processEvents()
        workspace_geometry = controller.geometry().getRect()
        shown = controller.windows[0]
        shown.set_zoom(150)
        app.processEvents()
        zoom_verified = shown.model.font_size > 11 and shown.model.row_height > 13
        shown.toggle_maximized()
        app.processEvents()
        maximized = shown.isMaximized()
        shown.toggle_maximized()
        app.processEvents()
        restored = not shown.isMaximized()
        if args.screenshots:
            args.screenshots.mkdir(parents=True, exist_ok=True)
            shown.grab().save(str(args.screenshots / "market_only_zoom150.png"))
            controller.grab().save(str(args.screenshots / "workspace.png"))
        pending = list(controller.windows)
        previous = time.perf_counter()

        def heartbeat():
            nonlocal previous
            now = time.perf_counter()
            gaps.append((now - previous) * 1000)
            previous = now

        def close_next():
            if not pending:
                loop.quit()
                return
            window = pending.pop(0)
            started = time.perf_counter()
            accepted = window.close()
            samples.append(dict(key=window.instrument.key, milliseconds=(time.perf_counter() - started) * 1000, accepted=accepted))
            QTimer.singleShot(50, close_next)

        timer = QTimer()
        timer.timeout.connect(heartbeat)
        timer.start(10)
        QTimer.singleShot(50, close_next)
        QTimer.singleShot(10000, loop.quit)
        loop.exec()
        timer.stop()
        checks = dict(all_windows_closed=not controller.windows and len(samples) == len(keys) and all(row["accepted"] for row in samples), quote_subscriptions_released=not session.references, close_calls_under_100ms=all(row["milliseconds"] <= 100 for row in samples), event_loop_gap_under_100ms=bool(gaps) and max(gaps) <= 100, zoom_enlarges_text_and_rows=zoom_verified, maximize_and_restore=maximized and restored, workspace_geometry_unchanged=controller.geometry().getRect() == workspace_geometry, no_trader_commands=all(command["command"] == "subscriptions" for command in gateway.commands), no_callback_errors=not callback_errors)
        screen = app.primaryScreen()
        report = dict(generated_at_utc=datetime.now(timezone.utc).isoformat(), source="local synthetic stale quotes with historical catalog; no network, credentials or trader API", environment=dict(system=platform.platform(), python=platform.python_version(), qt=qVersion(), platform=app.platformName(), dpi=screen.logicalDotsPerInch(), device_pixel_ratio=screen.devicePixelRatio()), catalog_instruments=len(controller.catalog), windows=len(keys), close_samples=samples, maximum_event_loop_gap_ms=max(gaps) if gaps else None, callback_errors=callback_errors, checks=checks, ok=all(checks.values()), limitations=["offscreen事件循环实验不等于桌面窗口管理器验收", "100ms仅为本机实验阈值，不保证其他硬件", "合成过期行情不是真实市场行情"])
        controller.gateway, controller.external = None, None
        controller.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        sys.excepthook = previous_hook
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
