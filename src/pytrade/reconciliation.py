from decimal import Decimal

from .positions import position_id


def validate_snapshot(positions, accounts, day):
    identifiers = set()
    for row in positions:
        if not all(isinstance(row.get(name), str) and row[name] for name in ("ExchangeID", "InstrumentID", "HedgeFlag")):
            raise ValueError("持仓查询缺少合约或套保标识")
        if row.get("PosiDirection") not in {"1", "2", "3"} or row.get("PositionDate") not in {"1", "2"}:
            raise ValueError("持仓查询方向或今昨仓标识无效")
        identifier = position_id(row)
        if identifier in identifiers:
            raise ValueError("持仓查询存在重复记录，禁止重复计算可平与风险")
        identifiers.add(identifier)
        for name in ("Position", "TodayPosition", "YdPosition", "LongFrozen", "ShortFrozen"):
            value = row.get(name, None if name == "Position" else 0)
            if type(value) is not int or value < 0:
                raise ValueError("持仓或冻结数量无效")
        if row.get("TodayPosition", 0) > row["Position"]:
            raise ValueError("今仓数量超过持仓数量")
        if row.get("TradingDay", day) != day:
            raise ValueError("持仓查询交易日与当前登录不一致")
    currencies = set()
    for row in accounts:
        currency = row.get("CurrencyID")
        if not isinstance(currency, str) or not currency or currency in currencies:
            raise ValueError("资金查询币种缺失或重复，须核对账户范围")
        currencies.add(currency)
        if row.get("TradingDay", day) != day:
            raise ValueError("资金查询交易日与当前登录不一致")
        for name in ("Balance", "Available", "Deposit", "Withdraw"):
            try:
                value = Decimal(str(row[name]))
                if not value.is_finite() or abs(value) > Decimal("1e50"):
                    raise ValueError
            except (KeyError, ValueError, ArithmeticError):
                raise ValueError("资金或出入金数值无效，禁止交易") from None
