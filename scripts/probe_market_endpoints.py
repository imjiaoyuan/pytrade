"""Check only TCP reachability of the bundled market endpoints; sends no payload.

Usage: python3 scripts/probe_market_endpoints.py --output PATH
An open port is not a successful login or a verified market-data subscription.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import time
import xml.etree.ElementTree as ET


def probe(entry):
    name, host, port = entry
    result = {"name": name, "host": host, "port": port}
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=3):
            result["status"] = "tcp_connected"
    except OSError as exc:
        result.update(status="tcp_failed", error=type(exc).__name__, detail=str(exc))
    result["elapsed_ms"] = round((time.monotonic() - start) * 1000, 1)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    tree = ET.parse(root / "reference_materials/original_software/HQAddress.xml")
    entries = [(e.attrib["Name"], e.text.strip(), int(e.attrib["Port"]))
               for e in tree.getroot().findall("Address")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(probe, entries))
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "TCP connect only, no login, subscription or trading request",
              "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for item in results:
        print(item["name"], item["status"], item["elapsed_ms"], item.get("error", ""))


if __name__ == "__main__":
    main()
