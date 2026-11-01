import errno
import socket
import time
from urllib.parse import urlsplit


def probe_market_transport(address):
    start = time.monotonic()
    state, message, code = "connected", "TCP 可达，尚未验证 CTP 登录及行情", None
    try:
        endpoint = urlsplit(address)
        if endpoint.scheme != "tcp" or not endpoint.hostname or not endpoint.port:
            raise ValueError("invalid endpoint")
        with socket.create_connection((endpoint.hostname, endpoint.port), timeout=4):
            pass
    except ValueError:
        state, message = "invalid", "行情地址无效，请填写 tcp://主机:端口"
    except OSError as error:
        code = error.errno
        if error.errno in {errno.EPERM, errno.EACCES}:
            state, message = "permission", "系统拒绝行情网络连接，请检查程序联网权限、沙箱或防火墙；不是账号密码错误"
        elif isinstance(error, (TimeoutError, socket.timeout)):
            state, message = "timeout", "行情服务器 TCP 连接超时，请检查网络、服务地址及服务方可用状态；尚未进入账号登录"
        elif isinstance(error, socket.gaierror):
            state, message = "dns", "行情服务器域名解析失败，请检查地址和网络"
        elif error.errno == errno.ECONNREFUSED:
            state, message = "refused", "行情地址拒绝 TCP 连接，可能是服务未监听或网络路径拒绝；尚未进入账号登录"
        else:
            state, message = "error", f"行情网络连接失败（系统代码 {error.errno}），请检查网络与服务地址"
    return dict(state=state, message=message, errno=code, elapsed_ms=round((time.monotonic() - start) * 1000, 1))


def check_market_transport(address):
    result = probe_market_transport(address)
    return "" if result["state"] == "connected" else result["message"]
