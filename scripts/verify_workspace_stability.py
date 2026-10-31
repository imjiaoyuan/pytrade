import argparse
import json
import os
import platform
import signal
from dataclasses import replace
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QTimer, qVersion
from PySide6.QtWidgets import QApplication

from pytrade.app import Controller
from pytrade.domain import CATALOG
from pytrade.simulation import Simulator
from pytrade.market_fault_proxy import MarketFaultProxy
from pytrade.external import ConnectionConfig, load_instruments


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--front")
    parser.add_argument("--switch-front")
    parser.add_argument("--switch-after", type=int, default=60)
    parser.add_argument("--reconnect-after", type=int, default=0)
    parser.add_argument("--duration", type=int, default=1800)
    parser.add_argument("--instruments", nargs="+")
    parser.add_argument("--windows", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--offscreen", action="store_true")
    parser.add_argument("--fault-proxy", action="store_true")
    parser.add_argument("--drop-transport", action="store_true")
    parser.add_argument("--drop-after", type=int, default=15)
    args = parser.parse_args()
    if not 5 <= args.duration <= 86400 or not 1 <= args.windows <= 40:
        parser.error("duration 须为 5–86400 秒，windows 须为 1–40")
    if args.switch_front and not args.front:
        parser.error("切源验证需要 --front")
    if args.drop_transport and (not args.front or args.fault_proxy or args.switch_front or args.reconnect_after or sys.platform != "linux"):
        parser.error("直接断开验证进程行情连接仅支持Linux、单一行情源且无主动切源")
    if args.drop_transport and not 1 <= args.drop_after < args.duration - 5:
        parser.error("断线时间须早于结束至少5秒")
    if args.fault_proxy and (not args.front or args.switch_front or args.reconnect_after):
        parser.error("代理断线验证只使用一个行情源，不同时主动重连或切源")
    if args.fault_proxy and not 1 <= args.drop_after < args.duration - 5:
        parser.error("断线时间须早于结束至少5秒")
    if args.output.exists():
        parser.error("输出文件已存在，请使用新文件名保留历史结果")
    for front in (args.front, args.switch_front):
        if front:
            ConnectionConfig(md_front=front).validate()
    root = Path(__file__).resolve().parents[1]
    catalog = CATALOG if args.offline else load_instruments(root / "data/Instruments.snapshot.xml")
    if args.offline:
        historical = load_instruments(root / "data/Instruments.snapshot.xml")
        catalog = dict(CATALOG)
        for key in ("SHFE.rb2701", "SHFE.al2612", "DCE.m2701", "CZCE.SR701", "CFFEX.IF2612"):
            if key in historical:
                catalog[key] = replace(historical[key], source="offline-stress", initial_ticks=1000, depth=1)
    keys = args.instruments or (list(catalog) if args.offline else ["SHFE.ag2612", "SHFE.au2612", "SHFE.cu2612"])
    if len(set(keys)) != len(keys) or any(key not in catalog for key in keys):
        parser.error("合约必须唯一且具有元数据；十合约验收须显式提供十个有效合约")
    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    temporary = TemporaryDirectory(prefix="pytrade-stability-")
    controller = Controller(Path(temporary.name))
    controller.timer.stop()
    if args.offline:
        controller.catalog = dict(catalog)
        controller.ledger.catalog = dict(catalog)
        controller.simulator = Simulator(controller.ledger)
        controller.advance()
        controller.filter_contracts()
    controller.show()
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    samples, changes, quotes = [], Counter(), Counter()
    signatures, events, errors, transitions = {}, Counter(), [], []
    cpu_start = os.times()
    queue_peak = 0
    process_exit = 1
    proxy = MarketFaultProxy(args.front) if args.fault_proxy else None
    active_front = proxy.front if proxy else args.front
    pending_front = None
    fault_epoch = None
    after_fault = Counter()
    forced_transport_closes = 0
    switching = False
    switched = False
    reconnected = False
    finished = False

    def event(message):
        nonlocal forced_transport_closes
        if message["kind"] == "probe_fault":
            forced_transport_closes += message.get("connections_closed", 0)
        events[message["kind"]] += 1
        if message["kind"] == "quote":
            quotes[message["key"]] += 1
            if fault_epoch is not None and message.get("epoch", 0) > fault_epoch:
                after_fault[message["key"]] += 1
        elif message["kind"] in {"fatal", "error"}:
            errors.append(message.get("message", "unknown"))

    def connect(front):
        nonlocal active_front
        active_front = front
        controller.start_external(ConnectionConfig(md_front=front, probe_mode=args.drop_transport))
        if not controller.gateway:
            errors.append("行情网关启动失败")
            return
        controller.gateway.event.connect(event)

    if args.front:
        connect(active_front)
    for index in range(args.windows):
        controller.open_dom(keys[index % len(keys)], group=index // len(keys) + 1)
    controller.align_windows()

    def request_switch(front, reason):
        nonlocal pending_front, switching
        pending_front, switching = front, True
        transitions.append(dict(seconds=round(time.monotonic() - started, 3), reason=reason, front=front))
        controller.disconnect_ctp()

    def sample():
        nonlocal queue_peak, switching, switched, reconnected, process_exit, fault_epoch
        elapsed = time.monotonic() - started
        if elapsed >= args.duration:
            finish()
            return
        if args.drop_transport and fault_epoch is None and elapsed >= args.drop_after:
            fault_epoch = controller.external.md_epoch if controller.external else 0
            controller.gateway.send(dict(command="probe_market_disconnect"))
            transitions.append(dict(seconds=round(elapsed, 3), reason="强制断开验证进程自身的行情TCP连接，等待SDK自行重连"))
        if proxy and fault_epoch is None and elapsed >= args.drop_after:
            fault_epoch = controller.external.md_epoch if controller.external else 0
            closed = proxy.drop()
            transitions.append(dict(seconds=round(elapsed, 3), reason="强制关闭只读行情TCP代理连接，等待SDK自行重连", connections_closed=closed))
        if switching:
            if controller.gateway is None:
                connect(pending_front)
                switching = False
            return
        if args.switch_front and not switched and elapsed >= args.switch_after:
            switched = True
            request_switch(args.switch_front, "主动断开并切源，不是断网实验")
            return
        if args.reconnect_after and args.front and not reconnected and elapsed >= args.reconnect_after:
            reconnected = True
            request_switch(active_front, "主动断开并重新连接，不是断网实验")
            return
        before = time.perf_counter()
        if args.offline or controller.external:
            controller.advance()
        snapshots = controller.external.snapshots if controller.external else controller.simulator.snapshots if args.offline else {}
        for key, snapshot in snapshots.items():
            signature = (snapshot.last, snapshot.volume, snapshot.bids, snapshot.asks)
            if signature != signatures.get(key):
                changes[key] += 1
                signatures[key] = signature
        for index, window in enumerate(controller.windows):
            if len(samples) % 100 == 0:
                window.model.follow = False
                window.model.center += 1 if index % 2 else -1
            if len(samples) % 200 == 0:
                window.resize(window.width(), 900 if window.height() > 900 else 960)
            window.table.viewport().repaint()
        samples.append((time.perf_counter() - before) * 1000)
        if controller.gateway:
            try:
                queue_peak = max(queue_peak, controller.gateway.outgoing.qsize())
            except (NotImplementedError, OSError):
                pass
        if len(samples) % 100 == 0:
            print(json.dumps(dict(seconds=round(elapsed), updates=dict(changes), queue_peak=queue_peak)), flush=True)

    def finish():
        nonlocal finished, process_exit
        if finished:
            return
        finished = True
        timer.stop()
        elapsed = time.monotonic() - started
        if args.screenshots:
            args.screenshots.mkdir(parents=True, exist_ok=True)
            controller.grab().save(str(args.screenshots / "workspace.png"))
            for index, window in enumerate(controller.windows[:3]):
                window.grab().save(str(args.screenshots / f"dom_{index}.png"))
        ordered = sorted(samples)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * .95))] if ordered else None
        cpu = os.times()
        cpu_seconds = cpu.user + cpu.system - cpu_start.user - cpu_start.system
        rss_kib = None
        if sys.platform == "linux":
            import resource
            rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        screen = app.primaryScreen()
        checks = dict(requested_duration_completed=elapsed >= args.duration - .1, all_contracts_changed=all(changes[key] >= 2 for key in keys), p95_under_100ms=p95 is not None and p95 <= 100, no_interface_errors=not errors, full_30_minutes=elapsed >= 1800, ten_unique_contracts=len(keys) >= 10, ten_windows=len(controller.windows) >= 10)
        if args.drop_transport:
            checks["sdk_recovered_after_forced_disconnect"] = forced_transport_closes > 0 and all(after_fault[key] >= 2 for key in keys)
        if proxy:
            checks["sdk_recovered_after_forced_disconnect"] = proxy.forced_closes > 0 and proxy.accepted >= 2 and all(after_fault[key] >= 2 for key in keys)
        report = dict(started_at_utc=started_at, finished_at_utc=datetime.now(timezone.utc).isoformat(), scope="offline generated market" if args.offline else "market only; empty credentials; no trader API or orders", environment=dict(system=platform.platform(), python=sys.version.split()[0], qt=qVersion(), platform=app.platformName(), dpi=screen.logicalDotsPerInch(), device_pixel_ratio=screen.devicePixelRatio()), duration_seconds=elapsed, fronts=[front for front in (args.front, args.switch_front) if front], instruments=keys, windows=len(controller.windows), events=dict(events), quotes=dict(quotes), changes=dict(changes), transitions=transitions, errors=errors, presentation_ms=dict(samples=len(samples), p95=p95, maximum=max(samples) if samples else None), cpu_seconds=cpu_seconds, maximum_rss_kib=rss_kib, observed_queue_peak=queue_peak, checks=checks, limitations=["受控主动重连不能替代真实断网恢复验收", "本地绘制耗时不代表网络行情延迟", "行情来源使用历史合约元数据，不能据此交易"])
        if args.drop_transport:
            report["forced_transport_connections_closed"] = forced_transport_closes
            report["quotes_after_fault"] = dict(after_fault)
        if proxy:
            report["fault_proxy"] = proxy.report()
            report["quotes_after_fault"] = dict(after_fault)
        if controller.external:
            report["final_capabilities"] = {key: vars(value) for key, value in controller.external.capabilities.items()}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        process_exit = 0 if checks["requested_duration_completed"] and checks["all_contracts_changed"] and checks["no_interface_errors"] and checks["p95_under_100ms"] and (not (proxy or args.drop_transport) or checks["sdk_recovered_after_forced_disconnect"]) else 1
        print(json.dumps(dict(output=str(args.output), checks=checks), ensure_ascii=False), flush=True)
        controller.close()
        QTimer.singleShot(6000, app.quit)

    signal.signal(signal.SIGINT, lambda *unused: finish())
    signal.signal(signal.SIGTERM, lambda *unused: finish())
    timer = QTimer()
    timer.timeout.connect(sample)
    timer.start(100)
    app.aboutToQuit.connect(finish)
    app.exec()
    if proxy:
        proxy.close()
    temporary.cleanup()
    return process_exit


if __name__ == "__main__":
    raise SystemExit(main())
