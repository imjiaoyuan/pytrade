from dataclasses import dataclass

from .domain import ACTIVE


@dataclass
class DomControls:
    only_open: bool = False
    nearby: bool = False
    nearby_ticks: int = 5
    limit_lock: bool = False
    white_lock: bool = False
    pending_lock: bool = False

    def validate(self, snapshot, side, offset, ticks, orders):
        if self.only_open and offset != "开仓":
            raise ValueError("OnlyOpen 已开启，本窗仅允许开仓")
        if snapshot is None:
            raise ValueError("行情缺失，不能提交本窗模拟委托")
        if self.nearby and abs(ticks - snapshot.last) > self.nearby_ticks:
            raise ValueError(f"本地近价限制：委托距最新价不得超过 {self.nearby_ticks} tick")
        if self.limit_lock:
            if snapshot.upper_limit is None or snapshot.lower_limit is None:
                raise ValueError("涨跌停价格缺失，涨跌停锁阻止委托")
            if ticks >= snapshot.upper_limit or ticks <= snapshot.lower_limit:
                raise ValueError("涨跌停锁阻止在涨跌停价及其外侧挂单")
        if self.white_lock and ticks not in dict(snapshot.bids) and ticks not in dict(snapshot.asks):
            raise ValueError("白格单锁：本地模拟仅允许在有可见盘口数量的价位挂单")
        if self.pending_lock and any(order.key == snapshot.key and order.side == side and order.offset == offset and order.ticks == ticks and order.status in ACTIVE for order in orders):
            raise ValueError("挂单锁定：本合约同方向、同开平、同价位已有模拟挂单")
