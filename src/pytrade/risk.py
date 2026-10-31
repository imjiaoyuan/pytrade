from dataclasses import dataclass
from decimal import Decimal

from .positions import allocation_changes, position_id


@dataclass(frozen=True)
class RiskLimits:
    total: int = 0
    group: int = 0
    cancels: int = 0
    loss: str = "0"

    def validate(self):
        if any(type(value) is not int or value <= 0 for value in (self.total, self.group, self.cancels)):
            raise ValueError("请填写账户总持仓、分组持仓和日撤单上限，须为正整数")
        try:
            loss = Decimal(self.loss)
        except (ArithmeticError, TypeError, ValueError):
            raise ValueError("日亏损上限须为正数") from None
        if not loss.is_finite() or loss <= 0:
            raise ValueError("日亏损上限须为正数")


class RiskBook:
    def __init__(self, state=None):
        state = state or {}
        if not isinstance(state, dict):
            raise ValueError("风险记录格式无效")
        self.allocations = state.get("allocations", {})
        self.processed = set(state.get("processed", []))
        self.day = state.get("day", "")
        self.baseline = state.get("baseline")
        self.cancel_count = state.get("cancel_count", 0)
        self.cancel_tokens = state.get("cancel_tokens", {})
        self.loss_locked = state.get("loss_locked", False)
        self.pnl = None
        if not isinstance(self.allocations, dict) or not isinstance(state.get("processed", []), list) or any(not isinstance(key, str) for key in self.processed):
            raise ValueError("分组或成交归属记录无效")
        for key, groups in self.allocations.items():
            if not isinstance(key, str) or len(key.split("|")) != 5 or not isinstance(groups, dict):
                raise ValueError("持仓归属格式无效")
            if any(not str(group).isdigit() or not 1 <= int(group) <= 20 or type(quantity) is not int or quantity < 0 for group, quantity in groups.items()):
                raise ValueError("持仓分配数量无效")
        if type(self.cancel_count) is not int or self.cancel_count < 0 or not isinstance(self.cancel_tokens, dict) or type(self.loss_locked) is not bool or not isinstance(self.day, str):
            raise ValueError("日风控记录无效")
        try:
            if self.baseline is not None and not Decimal(self.baseline).is_finite():
                raise ValueError("账户日基准无效")
        except (ArithmeticError, TypeError):
            raise ValueError("账户日基准无效") from None

    def state(self):
        return dict(allocations={key: dict(groups) for key, groups in self.allocations.items()}, processed=list(self.processed), day=self.day, baseline=self.baseline, cancel_count=self.cancel_count, cancel_tokens=dict(self.cancel_tokens), loss_locked=self.loss_locked)

    def set_day(self, day):
        if day != self.day:
            if self.day:
                rolled = {}
                for identifier, groups in self.allocations.items():
                    parts = identifier.split("|")
                    if parts[0] in {"SHFE", "INE"}:
                        parts[3] = "2"
                    target = rolled.setdefault("|".join(parts), {})
                    for group, quantity in groups.items():
                        target[group] = target.get(group, 0) + quantity
                self.allocations = rolled
            self.day = day
            self.baseline = None
            self.cancel_count = 0
            self.cancel_tokens = {}
            self.loss_locked = False
            self.pnl = None

    def equity(self, accounts, day, limits):
        self.set_day(day)
        self.pnl = None
        rows = [row for row in accounts if row.get("CurrencyID") == "CNY"]
        if len(rows) != 1:
            return
        row = rows[0]
        try:
            if row.get("TradingDay", day) != day:
                return
            values = [Decimal(str(row[name])) for name in ("Balance", "Deposit", "Withdraw")]
            if any(not value.is_finite() or abs(value) > Decimal("1e50") for value in values):
                return
            adjusted = values[0] - values[1] + values[2]
            if self.baseline is None:
                self.baseline = str(adjusted)
            self.pnl = adjusted - Decimal(self.baseline)
            if Decimal(limits.loss) > 0 and self.pnl <= -Decimal(limits.loss):
                self.loss_locked = True
        except (KeyError, ValueError, ArithmeticError):
            return

    def reconcile(self, positions, trades, orders):
        held, deltas = allocation_changes(positions, orders, trades, self.processed, self.day)
        invalid = set()
        for (identifier, group), change in deltas.items():
            groups = self.allocations.setdefault(identifier, {})
            quantity = groups.get(group, 0) + change
            if quantity < 0:
                invalid.add(identifier)
            else:
                groups[group] = quantity
        for key, allocations in list(self.allocations.items()):
            if key in invalid or sum(allocations.values()) > held.get(key, 0):
                del self.allocations[key]

    def assign(self, row, group, quantity):
        if type(group) is not int or not 1 <= group <= 20 or type(quantity) is not int or quantity < 0:
            raise ValueError("组别或分配数量无效")
        allocations = self.allocations.setdefault(position_id(row), {})
        others = sum(value for key, value in allocations.items() if key != str(group))
        if others + quantity > row.get("Position", 0):
            raise ValueError("分组分配超过实际持仓，不能重复分配")
        allocations[str(group)] = quantity

    def held(self, group, key=None, direction=None, date=None):
        return sum(groups.get(str(group), 0) for identifier, groups in self.allocations.items() if (key is None or ".".join(identifier.split("|")[:2]) == key) and (direction is None or identifier.split("|")[2] == direction) and (date is None or identifier.split("|")[3] == date))

    def check_open(self, limits, positions, pending, quantity, group):
        limits.validate()
        if self.pnl is None:
            raise ValueError("资金或出入金信息不完整，无法核验日亏损；禁止开仓")
        if self.loss_locked or self.pnl <= -Decimal(limits.loss):
            raise ValueError("已达到日亏损上限，禁止开仓；允许合规减仓")
        total = sum(row.get("Position", 0) for row in positions)
        reserved = sum(max(0, row.get("VolumeTotalOriginal", 0) - row.get("VolumeTraded", 0)) for row in pending)
        if total + reserved + quantity > limits.total:
            raise ValueError("超过账户总持仓上限，含未成交开仓")
        group_pending = sum(max(0, row.get("VolumeTotalOriginal", 0) - row.get("VolumeTraded", 0)) for row in pending if row.get("Group") == group)
        if self.held(group) + group_pending + quantity > limits.group:
            raise ValueError("超过分组持仓上限，含未成交开仓")

    def reserve_cancel(self, limits, token):
        if limits.cancels <= 0:
            raise ValueError("请先填写日撤单上限")
        if self.cancel_count >= limits.cancels:
            raise ValueError("已达到日撤单上限，撤单未发送")
        self.cancel_count += 1
        self.cancel_tokens[token] = self.day

    def unsent_cancel(self, token):
        if self.cancel_tokens.pop(token, None) == self.day:
            self.cancel_count = max(0, self.cancel_count - 1)
