import argparse
import json
import sys
import time
from decimal import Decimal
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QCoreApplication, QTimer

from pytrade.external import ConnectionConfig, ExternalSession, load_instruments
from pytrade.gateway import CtpGateway


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--front", required=True)
    parser.add_argument("--instruments", nargs="+", default=["SHFE.ag2612", "SHFE.au2612", "SHFE.rb2701"])
    parser.add_argument("--duration", type=int, default=25)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--paper-smoke", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.duration <= 60:
        parser.error("duration must be between 1 and 60 seconds")
    if args.paper_smoke and (not args.gui or len(args.instruments) < 2):
        parser.error("paper-smoke requires --gui and at least two instruments")
    root = Path(__file__).resolve().parents[1]
    catalog = load_instruments(root / "data" / "Instruments.snapshot.xml")
    if any(key not in catalog for key in args.instruments):
        parser.error("instrument metadata missing")
    config = ConnectionConfig(md_front=args.front, paper_trading=args.paper_smoke)
    config.validate()
    controller = None
    if args.gui:
        from PySide6.QtWidgets import QApplication
        from pytrade.app import Controller
        app = QApplication([sys.argv[0]])
        app.setStyle("Fusion")
        controller = Controller(root / ".runtime" / "application_gui_probe")
        controller.show()
        controller.start_external(config)
        gateway, session = controller.gateway, controller.external
        if session is None:
            raise RuntimeError("GUI gateway failed to start")
    else:
        app = QCoreApplication([sys.argv[0]])
        gateway = CtpGateway(config, root / ".runtime" / "application_probe")
        session = ExternalSession(config, gateway, catalog)
    counts = Counter()
    prices = {}
    errors = []
    local_started = False
    started = datetime.now(timezone.utc).isoformat()

    def on_event(event):
        nonlocal local_started
        counts[event["kind"]] += 1
        if not controller:
            session.handle(event)
        if event["kind"] in {"fatal", "error"}:
            errors.append(event["message"])
        if event["kind"] == "quote":
            snapshot = session.snapshots.get(event["key"])
            if snapshot:
                prices.setdefault(snapshot.key, set()).add((snapshot.last, snapshot.volume, snapshot.time))
        if args.paper_smoke and not local_started and all(key in session.snapshots for key in args.instruments):
            try:
                first, second = args.instruments[:2]
                snapshot = controller.paper.check_ready(first)
                controller.paper.check_ready(second)
                if not snapshot.asks:
                    return
                ticks = snapshot.asks[0][0]
                for kind in ("FAK", "FOK"):
                    controller.paper.submit(first, "买", "开仓", ticks, 1, order_type=kind)
                instrument = controller.catalog[first]
                for kind, trigger in (("best", ticks + 10), ("sniper", ticks + 10), ("stop", snapshot.last)):
                    controller.local_algos.start(kind, first, "买", "开仓", 1, instrument.tick * trigger, 1)
                from pytrade.local_algos import calculate_spread
                spread = calculate_spread(snapshot, session.snapshots[second], instrument.tick, controller.catalog[second].tick)
                controller.local_algos.start("spread", first, "买", "开仓", 1, spread.ask + Decimal("100"), 1, second)
                local_started = True
            except ValueError as error:
                local_started = True
                errors.append(f"local paper smoke: {error}")

    def finish():
        if controller and args.screenshots:
            args.screenshots.mkdir(parents=True, exist_ok=True)
            controller.grab().save(str(args.screenshots / "controller.png"))
            for index, window in enumerate(controller.windows):
                window.grab().save(str(args.screenshots / f"dom_{index}.png"))
        report = {"started_at_utc": started, "finished_at_utc": datetime.now(timezone.utc).isoformat(), "scope": "Application gateway and normalization, market only, no trader API", "front": args.front, "duration_seconds": args.duration, "events": dict(counts), "errors": errors, "results": {key: {"observations": len(prices.get(key, set())), "last": catalog[key].price(snapshot.last), "bids": [[catalog[key].price(price), quantity] for price, quantity in snapshot.bids], "asks": [[catalog[key].price(price), quantity] for price, quantity in snapshot.asks], "trading_day": snapshot.trading_day, "action_day": snapshot.action_day, "quote_age_seconds": round(time.monotonic() - snapshot.received_monotonic, 3)} for key, snapshot in session.snapshots.items()}}
        if args.paper_smoke:
            report.update(scope="Application GUI/gateway plus local paper algorithms driven by received public simulation feed; no trader API or external orders", local_started=local_started, local_orders=[dict(id=order.id, key=order.key, side=order.side, offset=order.offset, type=order.order_type, status=str(order.status), filled=order.filled, quantity=order.quantity) for order in controller.ledger.orders.values()], local_fill_count=len(controller.ledger.fills), algorithms=[dict(id=task.id, kind=task.kind, status=task.status, message=task.message) for task in controller.local_algos.tasks.values()], no_trader_api=not bool(config.td_front or config.allow_orders), limitations=["Public simulation quotes are not a production market or external trading-account acceptance", "Finite-duration observation does not guarantee all limit orders or spread hedge legs fill", "No fees, margin or real queue priority simulated"])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        if controller:
            controller.close()
        else:
            gateway.close()

    gateway.event.connect(on_event)
    gateway.stopped.connect(app.quit)
    if controller:
        for window in list(controller.windows):
            if window.instrument.key not in args.instruments:
                window.close()
        existing = {window.instrument.key for window in controller.windows}
        for key in args.instruments:
            if key not in existing:
                controller.open_dom(key)
    else:
        for key in args.instruments:
            session.retain(key)
    QTimer.singleShot(args.duration * 1000, finish)
    app.exec()
    if not all(prices.get(key) for key in args.instruments):
        sys.exit(1)


if __name__ == "__main__":
    main()
