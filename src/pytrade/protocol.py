import math
from typing import Literal, TypedDict


VERSION = 1
CommandName = Literal["subscriptions", "sync", "confirm_settlement", "insert", "cancel", "stop", "probe_market_disconnect"]


class Command(TypedDict, total=False):
    version: int
    command: CommandName
    cycle: int
    instruments: bool
    keys: list[str]
    row: dict
    cancel_token: str


class Event(TypedDict, total=False):
    version: int
    kind: str
    epoch: int
    cycle: int
    key: str
    row: dict
    rows: list[dict]
    received: float
    message: str
    channel: str
    error_id: int


def validate_version(message):
    if not isinstance(message, dict) or type(message.get("version", VERSION)) is not int or message.get("version", VERSION) != VERSION:
        raise ValueError("接口消息版本不兼容，禁止交易；须重新连接并核对版本")


def command_message(values):
    validate_version(values)
    name = values.get("command")
    if name not in {"subscriptions", "sync", "confirm_settlement", "insert", "cancel", "stop", "probe_market_disconnect"}:
        raise ValueError("未知的接口命令，未执行")
    if name == "subscriptions":
        keys = values.get("keys")
        if not isinstance(keys, list) or len(keys) > 1000 or any(not isinstance(key, str) or "." not in key for key in keys):
            raise ValueError("订阅命令合约列表无效")
    if name == "sync" and (type(values.get("cycle")) is not int or values["cycle"] <= 0):
        raise ValueError("查询命令周期无效")
    if name in {"insert", "cancel"} and not isinstance(values.get("row"), dict):
        raise ValueError("交易命令缺少委托结构")
    return {**values, "version": VERSION}


def validate_event(event):
    validate_version(event)
    kind = event.get("kind")
    if kind not in {"md_state", "subscription", "quote", "td_state", "td_login", "query_row", "query_rows", "sync_done", "order", "trade", "insert_error", "cancel_error", "settled", "error", "fatal", "probe_fault"}:
        raise ValueError("未知的接口事件，禁止交易")
    if kind in {"md_state", "subscription", "quote"} and (type(event.get("epoch")) is not int or event["epoch"] < 0):
        raise ValueError("行情事件连接代号无效")
    if kind in {"subscription", "quote"} and (not isinstance(event.get("key"), str) or "." not in event["key"]):
        raise ValueError("行情事件合约无效")
    if kind in {"quote", "order", "trade", "query_row"} and not isinstance(event.get("row"), dict):
        raise ValueError("接口事件数据结构无效")
    if kind == "quote":
        received = event.get("received")
        if type(received) not in {int, float} or not math.isfinite(received) or received < 0:
            raise ValueError("行情事件接收时间无效")
    if kind in {"query_row", "query_rows", "sync_done"} and (type(event.get("cycle")) is not int or event["cycle"] <= 0):
        raise ValueError("查询事件周期无效")
    if kind in {"query_row", "query_rows"} and event.get("query") not in {"instruments", "orders", "trades", "positions", "account", "settlement"}:
        raise ValueError("查询事件类别无效")
    if kind == "query_rows":
        rows = event.get("rows")
        if not isinstance(rows, list) or len(rows) > 100000 or any(not isinstance(row, dict) for row in rows):
            raise ValueError("查询事件记录列表无效")
    if kind in {"error", "fatal", "insert_error", "cancel_error"} and not isinstance(event.get("message"), str):
        raise ValueError("接口错误事件缺少说明")
    if kind in {"md_state", "td_state"} and not isinstance(event.get("status"), str):
        raise ValueError("连接状态事件无效")
    if kind in {"insert_error", "cancel_error"} and not isinstance(event.get("order_key"), str):
        raise ValueError("交易错误事件缺少原委托标识")
    if "not_sent" in event and type(event["not_sent"]) is not bool:
        raise ValueError("交易请求发送状态无效")
    if kind == "td_login":
        day = event.get("day")
        if not isinstance(day, str) or len(day) != 8 or not day.isdigit() or any(type(event.get(name)) is not int or event[name] < 0 for name in ("front", "session")):
            raise ValueError("交易登录回报标识或交易日无效")
