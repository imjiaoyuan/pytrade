"""Read-only CTP market-data verification; empty credentials, no trader API.

Install requirements-market-data.txt in .venv, then run with --front tcp://HOST:PORT.
Writes a bounded evidence report. No account lookup, credential guessing or orders.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import threading
import time

# This wrapper requires zh_CN.GB18030 on Linux at import time. A local locale
# keeps the host system unchanged; see PLAN.md, market-data section, for the build command.
local_locale = Path(__file__).resolve().parents[1] / ".runtime" / "locale"
if os.name == "posix" and local_locale.is_dir():
    os.environ.setdefault("LOCPATH", str(local_locale))

from openctp_ctp import mdapi


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    if isinstance(value, float) and (not math.isfinite(value) or abs(value) > 1e100):
        return None
    return value


class MarketProbe(mdapi.CThostFtdcMdSpi):
    def __init__(self, front, instruments, flow, test_api=False):
        super().__init__()
        self.front = front
        self.instruments = instruments
        self.lock = threading.Lock()
        self.events = []
        self.counts = Counter()
        self.first_quotes = {}
        self.last_quotes = {}
        self.quote_times = {}
        self.changed_quotes = Counter()
        self.login_attempted = False
        args = (str(flow) + "/", False, False, False) if test_api else (str(flow) + "/",)
        self.api = mdapi.CThostFtdcMdApi.CreateFtdcMdApi(*args)
        self.api.RegisterSpi(self)
        self.api.RegisterFront(front)

    def record(self, kind, **data):
        with self.lock:
            self.counts[kind] += 1
            if len(self.events) < 1000:
                self.events.append(dict(received_at_utc=now(), kind=kind, **data))

    def OnFrontConnected(self):
        self.record("front_connected")
        if not self.login_attempted:
            self.login_attempted = True
            req = mdapi.CThostFtdcReqUserLoginField()
            result = self.api.ReqUserLogin(req, 1)
            self.record("login_request", return_code=result, credentials="empty")

    def OnFrontDisconnected(self, reason):
        self.record("front_disconnected", reason=reason)

    def OnRspUserLogin(self, login, info, request_id, is_last):
        error = info.ErrorID if info else 0
        self.record("login_response", error_id=error,
                    error_message=info.ErrorMsg if info else "",
                    trading_day=login.TradingDay if login else "")
        if error == 0:
            result = self.api.SubscribeMarketData(
                [x.encode("ascii") for x in self.instruments], len(self.instruments))
            self.record("subscription_request", return_code=result,
                        instruments=self.instruments)

    def OnRspSubMarketData(self, instrument, info, request_id, is_last):
        self.record("subscription_response",
                    instrument=instrument.InstrumentID if instrument else "",
                    error_id=info.ErrorID if info else 0,
                    error_message=info.ErrorMsg if info else "")

    def OnRspError(self, info, request_id, is_last):
        self.record("api_error", error_id=info.ErrorID if info else None,
                    error_message=info.ErrorMsg if info else "")

    def OnRtnDepthMarketData(self, data):
        fields = ["InstrumentID", "ExchangeID", "TradingDay", "ActionDay",
                  "UpdateTime", "UpdateMillisec", "LastPrice", "Volume", "Turnover",
                  "OpenInterest", "UpperLimitPrice", "LowerLimitPrice"]
        fields += [f"{side}{field}{level}" for level in range(1, 6)
                   for side in ["Bid", "Ask"] for field in ["Price", "Volume"]]
        quote = {field: clean(getattr(data, field, None)) for field in fields}
        instrument = quote["InstrumentID"]
        with self.lock:
            previous = self.last_quotes.get(instrument)
            if previous and any(previous.get(k) != quote[k] for k in
                                ["LastPrice", "Volume", "BidPrice1", "AskPrice1",
                                 "BidVolume1", "AskVolume1"]):
                self.changed_quotes[instrument] += 1
            self.first_quotes.setdefault(instrument, dict(quote, received_at_utc=now()))
            self.last_quotes[instrument] = dict(quote, received_at_utc=now())
            stamps = self.quote_times.setdefault(instrument, set())
            if len(stamps) < 2000:
                stamps.add((quote["UpdateTime"], quote["UpdateMillisec"]))
        self.record("quote", **quote)

    def snapshot(self):
        with self.lock:
            return {"front": self.front, "counts": dict(self.counts),
                    "first_quotes": dict(self.first_quotes),
                    "last_quotes": dict(self.last_quotes),
                    "distinct_quote_times": {k: len(v) for k, v in self.quote_times.items()},
                    "changed_quote_counts": dict(self.changed_quotes),
                    "events": list(self.events)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--front", action="append", required=True)
    parser.add_argument("--duration", type=float, default=25)
    parser.add_argument("--instruments", nargs="+", default=["ag2612", "au2612", "rb2701"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test-api", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.duration <= 60:
        parser.error("duration must be between 1 and 60 seconds")
    root = Path(__file__).resolve().parents[1]
    probes = []
    started = now()
    for i, front in enumerate(args.front):
        flow = root / ".runtime" / f"md_{time.time_ns()}_{i}"
        flow.mkdir(parents=True)
        probe = MarketProbe(front, args.instruments, flow, args.test_api)
        probes.append(probe)
        probe.api.Init()
    print("Market-only verification started:", args.front, flush=True)
    time.sleep(args.duration)
    report = {"started_at_utc": started, "finished_at_utc": now(),
              "api_version": mdapi.CThostFtdcMdApi.GetApiVersion(),
              "scope": "Market API only; one empty-credential login per front; no trading API",
              "duration_seconds": args.duration,
              "test_api": args.test_api,
              "results": [probe.snapshot() for probe in probes]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    for result in report["results"]:
        print(result["front"], result["counts"],
              "distinct_times=", result["distinct_quote_times"],
              "changed=", result["changed_quote_counts"], flush=True)
    for probe in probes:
        probe.api.RegisterSpi(None)
        probe.api.Release()


if __name__ == "__main__":
    main()
