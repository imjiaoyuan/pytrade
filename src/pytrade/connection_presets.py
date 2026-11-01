SIMNOW_PRESETS = {
    "intraday": {
        "label": "SimNow 盘中 · 第一套第一组",
        "md_front": "tcp://182.254.243.31:30011",
        "td_front": "tcp://182.254.243.31:30001",
    },
    "7x24": {
        "label": "SimNow 7×24 · 第二套",
        "md_front": "tcp://182.254.243.31:40011",
        "td_front": "tcp://182.254.243.31:40001",
    },
}


def simnow_parameters(name, market_only=False):
    if name not in SIMNOW_PRESETS:
        raise ValueError("未知 SimNow 环境")
    preset = SIMNOW_PRESETS[name]
    return dict(md_front=preset["md_front"], td_front="" if market_only else preset["td_front"], broker="9999", app_id="" if market_only else "simnow_client_test", auth_code="" if market_only else "0000000000000000")
