import json
import time
from collections import Counter
from pathlib import Path

from .configuration import sensitive_key
from .external import ConnectionConfig, ExternalSession


class ReplayGateway:
    def __init__(self):
        self.commands = []

    def send(self, command):
        self.commands.append(command)


def replay_session(path):
    path = Path(path)
    if path.stat().st_size > 20_000_000:
        raise ValueError("回放文件超过 20MB")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or data.get("source") != "synthetic" or not isinstance(data.get("events"), list):
        raise ValueError("须使用版本1、明确标注 synthetic 的脱敏回放文件")

    def verify(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if sensitive_key(key) and item:
                    raise ValueError("回放含可能敏感的字段，请先脱敏")
                verify(item)
        elif isinstance(value, list):
            for item in value:
                verify(item)

    verify(data)
    gateway = ReplayGateway()
    session = ExternalSession(ConnectionConfig(md_front="tcp://127.0.0.1:1"), gateway)
    counts = Counter()
    for event in data["events"]:
        if event.get("kind") == "retain":
            session.retain(event["key"])
        else:
            if event.get("kind") == "quote":
                event["received"] = time.monotonic()
            session.handle(event)
        counts[event["kind"]] += 1
    if any(command["command"] in {"insert", "cancel"} for command in gateway.commands):
        raise ValueError("只读回放产生了交易命令")
    result = dict(source="synthetic, read-only, no network", events=dict(counts), order_states={key: row.get("OrderStatus") for key, row in session.orders.items()}, trades=len(session.trades), snapshots={key: snapshot.last for key, snapshot in session.snapshots.items()}, logged_in=session.logged_in, ready=session.ready, faulted=session.faulted)
    for key, expected in data.get("expected", {}).items():
        if result.get(key) != expected:
            raise ValueError(f"回放结果不符合预期：{key}")
    return result
