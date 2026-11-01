import time

from .version import VERSION


def availability(controller):
    session = controller.external
    rows = [
        ("版本", f"{VERSION} · 试用版", "当前不属于实盘交易正式交付"),
        ("离线模拟", "运行中" if controller.offline_mode else "可手动切换", "持仓与撮合仅本次会话有效"),
    ]
    rows.append(("在线模拟", "真实行情＋本地撮合" if controller.paper and session else "行情已断开" if controller.paper else "可在登录页选择", "不发送外部委托；不含手续费、保证金和真实排队"))
    if session:
        now = time.monotonic()
        fresh = sum(0 <= now - snapshot.received_monotonic <= session.config.stale_seconds for snapshot in session.snapshots.values())
        rows.append(("真实行情", f"{session.md_status} · 有效报价 {fresh} 个", "来源、交易日和深度按实际回调显示"))
        if session.config.td_front:
            enabled = session.config.environment == "simulation" and session.config.allow_orders and session.logged_in and session.settled and session.ready and not session.faulted
            rows.append(("外部模拟报撤单", "接口门槛已满足" if enabled else "暂未就绪", "门槛满足不代表账户交易验收已通过"))
            rows.append(("账户对账", session.td_status, session.error or "资金、费用和保证金仍须与柜台逐笔核对"))
        else:
            rows.append(("外部模拟报撤单", "未连接交易账户", "本地填写模拟账户、风险限额并确认结算"))
    else:
        rows.append(("真实行情", "未连接", "登录页选择行情服务后连接"))
        rows.append(("外部模拟报撤单", "未连接", "本地填写模拟账户并完成实际验收"))
    rows.extend([
        ("实盘报撤单", "禁用", "当前版本只允许实盘登录和查询"),
        ("本地止损", "可在模拟模式启用", "退出后失效；限价平仓不保证成交"),
        ("服务端托管止损", "未接入实际服务", "需指定提供方及接口并完成托管与成交核对"),
        ("本地模拟委托", "GFD / FAK / FOK、先平后开", "FAK/FOK 使用可见模拟流动性，外部账户仍仅普通限价"),
        ("本地策略与价差", "最优价 / 狙击 / 条件单 / 双腿价差", "仅本地模拟，双腿非原子成交；原版A/B、价差锁、提前挂单规则仍待核对"),
        ("原版高级操作", "未确认的控件禁用", "A/B、M-OrderX、CntrbySprd 等须原版运行证据"),
        ("Windows交付", "待实机验证", "构建脚本和安装包定义不等于Windows验收通过"),
    ])
    return rows
