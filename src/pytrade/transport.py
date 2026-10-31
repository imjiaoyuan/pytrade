import errno
import socket
from urllib.parse import urlsplit


def check_market_transport(address):
    endpoint = urlsplit(address)
    try:
        with socket.create_connection((endpoint.hostname, endpoint.port), timeout=4):
            return ""
    except OSError as error:
        if error.errno in {errno.EPERM, errno.EACCES}:
            return "系统拒绝行情网络连接，请检查程序联网权限、沙箱或防火墙；不是账号密码错误"
        if isinstance(error, (TimeoutError, socket.timeout)):
            return "行情服务器 TCP 连接超时，请检查网络、服务地址及服务方可用状态；尚未进入账号登录"
        if isinstance(error, socket.gaierror):
            return "行情服务器域名解析失败，请检查地址和网络"
        if error.errno == errno.ECONNREFUSED:
            return "行情服务器拒绝 TCP 连接，请核对端口或更换可用行情服务；尚未进入账号登录"
        return f"行情网络连接失败（系统代码 {error.errno}），请检查网络与服务地址"
