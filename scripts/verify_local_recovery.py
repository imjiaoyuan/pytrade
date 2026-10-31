import argparse
import json
import platform
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pytrade.positions import position_id
from pytrade.records import Journal, load_journal
from pytrade.risk import RiskBook


def verify(count):
    orders, trades, positions = {}, {}, {}
    expected = defaultdict(lambda: defaultdict(int))
    symbols = (("SHFE", "synthetic_au"), ("INE", "synthetic_sc"), ("DCE", "synthetic_m"), ("CZCE", "synthetic_sr"))
    for index in range(count):
        pair = index // 2
        exchange, symbol = symbols[pair % len(symbols)]
        hedge = "3" if pair % 5 == 0 else "1"
        group = pair % 20 + 1
        manual = pair % 7 == 0
        opening = index % 2 == 0
        quantity = 3 if opening else 1
        row = dict(ExchangeID=exchange, InstrumentID=symbol, HedgeFlag=hedge, PosiDirection="2", PositionDate="1", Position=0)
        identifier = position_id(row)
        position = positions.setdefault(identifier, row)
        position["Position"] += quantity if opening else -quantity
        order = dict(TradingDay="20261030", ExchangeID=exchange, InstrumentID=symbol, OrderSysID=f"synthetic-{index}", CombHedgeFlag=hedge, OrderStatus="0")
        if not manual:
            order["Group"] = group
        orders[str(index)] = order
        trades[str(index)] = order | dict(TradeID=f"synthetic-{index}", HedgeFlag=hedge, Direction="0" if opening else "1", OffsetFlag="0" if opening else "3" if exchange in {"SHFE", "INE"} else "1", Volume=quantity)
        if not manual and hedge == "1":
            expected[identifier][str(group)] += quantity if opening else -quantity
    items = list(trades.items())
    random.Random(20261030).shuffle(items)
    trades = dict(items)
    book = RiskBook()
    book.set_day("20261030")
    started = time.perf_counter()
    book.reconcile(list(positions.values()), trades, orders)
    reconcile_ms = (time.perf_counter() - started) * 1000
    expected = {key: dict(groups) for key, groups in expected.items()}
    checks = dict(mixed_hedge_manual_and_group_attribution=book.allocations == expected)
    book.reconcile(list(positions.values()), trades, orders)
    checks["duplicate_trades_idempotent"] = book.allocations == expected
    with TemporaryDirectory(prefix="pytrade-local-recovery-") as directory:
        path = Path(directory) / "transactions.json"
        writer = Journal(path)
        started = time.perf_counter()
        try:
            for revision in range(100):
                state = dict(version=2, orders=dict(orders), trades=dict(trades), risk=book.state(), stops=[], observations=dict(synthetic_revision=revision))
                writer.submit(state, owned=True)
                if revision == 49:
                    writer.archive(state, "20261030")
            writer.flush()
        finally:
            writer.close()
        journal_ms = (time.perf_counter() - started) * 1000
        restored = load_journal(path)
        checks["latest_snapshot_durable"] = restored["observations"]["synthetic_revision"] == 99 and writer.written == writer.sequence
        archives = list((path.parent / "archive").glob("*.json"))
        checks["archive_boundary_preserved"] = len(archives) == 1 and json.loads(archives[0].read_text())["observations"]["synthetic_revision"] == 49
        recovered = RiskBook(restored["risk"])
        recovered.reconcile(list(positions.values()), restored["trades"], restored["orders"])
        checks["restart_does_not_duplicate_attribution"] = recovered.allocations == expected
        recovered.set_day("20261102")
        rolled = {}
        for identifier, groups in expected.items():
            fields = identifier.split("|")
            if fields[0] in {"SHFE", "INE"}:
                fields[3] = "2"
            rolled["|".join(fields)] = groups
        checks["trading_day_rollover"] = recovered.allocations == rolled and recovered.cancel_count == 0 and recovered.baseline is None
        journal_bytes = path.stat().st_size
    return dict(source="synthetic; no network, credentials, trader API or external orders", orders=count, trades=count, position_rows=len(positions), reconcile_ms=reconcile_ms, journal_100_snapshots_ms=journal_ms, journal_bytes=journal_bytes, coalesced_snapshots=writer.coalesced, checks=checks, ok=all(checks.values()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=20000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 20 <= args.count <= 100000 or args.count % 2:
        parser.error("count 须为20–100000之间的偶数")
    if args.output.exists():
        parser.error("请使用新文件名保留历史证据")
    result = verify(args.count)
    result.update(generated_at_utc=datetime.now(timezone.utc).isoformat(), system=platform.platform(), python=platform.python_version())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
