import os
import socket
import struct
import sys
from pathlib import Path
from urllib.parse import urlsplit


def drop_own_market_connection(config):
    if not config.probe_mode or config.td_front or config.allow_orders or not config.md_anonymous or config.user or config.password or config.auth_code or config.md_user or config.md_password:
        raise ValueError("仅允许在专用的空凭据、无交易接口行情验证中注入断线")
    if sys.platform != "linux":
        raise ValueError("当前故障注入仅支持Linux验证进程")
    endpoint = urlsplit(config.md_front)
    address = f"{struct.unpack('<I', socket.inet_aton(endpoint.hostname))[0]:08X}:{endpoint.port:04X}"
    inodes = set()
    for line in Path("/proc/self/net/tcp").read_text().splitlines()[1:]:
        fields = line.split()
        if fields[2] == address and fields[3] == "01":
            inodes.add(fields[9])
    dropped = 0
    for descriptor in Path("/proc/self/fd").iterdir():
        try:
            link = descriptor.readlink().as_posix()
            if link.startswith("socket:[") and link[8:-1] in inodes:
                with socket.socket(fileno=os.dup(int(descriptor.name))) as connection:
                    connection.shutdown(socket.SHUT_RDWR)
                    dropped += 1
        except OSError:
            continue
    if not dropped:
        raise ValueError("未定位到本验证进程的行情TCP连接")
    return dropped
