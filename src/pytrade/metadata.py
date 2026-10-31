from decimal import Decimal

from .domain import Instrument


def query_instrument(row, day, timestamp):
    tick = Decimal(str(row["PriceTick"]))
    multiplier = int(row["VolumeMultiple"])
    minimum = int(row.get("MinLimitOrderVolume", 0))
    maximum = int(row.get("MaxLimitOrderVolume", 0))
    key = f"{row['ExchangeID']}.{row['InstrumentID']}"
    if not tick.is_finite() or tick <= 0 or multiplier <= 0 or minimum <= 0 or maximum < minimum:
        raise ValueError("合约交易参数无效")
    expiry = row.get("ExpireDate", "")
    enabled = row.get("IsTrading") in {True, 1} and row["ProductClass"] == "1" and (not expiry or expiry >= day)
    return Instrument(key, row["InstrumentName"], tick, multiplier, 1, 0, expiry, "ctp-query", timestamp, day, row["ProductClass"], minimum, maximum, enabled)
