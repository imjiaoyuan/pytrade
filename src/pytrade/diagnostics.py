import json
import multiprocessing
import os
import platform
import struct
import sys
from datetime import datetime, timezone

from PySide6.QtCore import qVersion

from .resources import locale_path, required_resources
from .version import CHANNEL, VERSION


def native_probe(connection):
    try:
        if sys.platform == "linux":
            locale_path()
        from openctp_ctp import mdapi, tdapi
        from .ctp_worker import FIELDS
        classes = {"quote": "CThostFtdcDepthMarketDataField", "instruments": "CThostFtdcInstrumentField", "orders": "CThostFtdcOrderField", "trades": "CThostFtdcTradeField", "positions": "CThostFtdcInvestorPositionField", "account": "CThostFtdcTradingAccountField", "settlement": "CThostFtdcSettlementInfoField"}
        missing = {}
        for kind, name in classes.items():
            value = getattr(tdapi, name)()
            fields = [field for field in FIELDS[kind] if not hasattr(value, field)]
            if fields:
                missing[kind] = fields
        connection.send(dict(ok=not missing, md_version=mdapi.CThostFtdcMdApi.GetApiVersion(), td_version=tdapi.CThostFtdcTraderApi.GetApiVersion(), missing_fields=missing, network_used=False))
    except BaseException as error:
        connection.send(dict(ok=False, error=type(error).__name__, network_used=False))
    finally:
        connection.close()


def check_native(timeout=10):
    context = multiprocessing.get_context("spawn")
    incoming, outgoing = context.Pipe(duplex=False)
    process = context.Process(target=native_probe, args=(outgoing,), daemon=True)
    process.start()
    outgoing.close()
    result = dict(ok=False, error="原生依赖检查超时或进程退出", network_used=False)
    try:
        if incoming.poll(timeout):
            try:
                result = incoming.recv()
            except EOFError:
                pass
    finally:
        incoming.close()
        process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
    result["exit_code"] = process.exitcode
    if process.exitcode != 0:
        result["ok"] = False
    return result


def diagnostics(native=False):
    resources = {str(path.relative_to(path.parents[1])): path.is_file() for path in required_resources()}
    report = dict(version=1, generated_at_utc=datetime.now(timezone.utc).isoformat(), application_version=VERSION, channel=CHANNEL, system=platform.system(), system_version=platform.release(), python=platform.python_version(), bits=struct.calcsize("P") * 8, qt=qVersion(), resources=resources, network_used=False, capabilities=dict(offline=True, market=True, external_simulation="requires local account and acceptance", live_orders=False, hosted_stop_provider=False, original_advanced_controls=False))
    report["capabilities"]["paper_trading"] = "real market plus local limit matching; no trader API"
    if native:
        report["native"] = check_native()
    report["ok"] = all(resources.values()) and report["bits"] == 64 and (not native or report["native"]["ok"])
    return report


def write_diagnostics(path, native=False):
    report = diagnostics(native)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report["ok"]
