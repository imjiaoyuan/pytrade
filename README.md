# PyTrade

Cross-platform desktop client for Chinese futures market data and trading, rebuilt with Python 3.12 + PySide6 + CTP (openctp-ctp).

![CI](https://github.com/jiaoyuan/pytrade/actions/workflows/ci.yml/badge.svg)

## Status

Preview (0.1.0rc1). Market feeds have passed historical checks; current availability varies by provider. External order routing is not yet validated against a broker account. See [Scope](#scope).

## Platforms

| Platform | CTP | Notes |
| --- | --- | --- |
| Windows x64 | 6.7.11 | primary target |
| Linux x64 | 6.7.11 | zh_CN.GB18030 locale bundled |
| macOS (10.15+/arm64) | 6.7.7.1 | newest wheel available on PyPI |

## Run from source

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-desktop.txt   # macOS: pip install PySide6 openctp-ctp==6.7.7.1
.venv/bin/python src/main.py
```

Linux requires the `zh_CN.GB18030` locale for the CTP native library:

```bash
localedef -i zh_CN -f GB18030 --no-archive packaging/pyinstaller/locale/zh_CN.GB18030
```

Or install it system-wide (`/usr/lib/locale`). The app auto-detects the bundled copy via `LOCPATH`.

## Modes

- **Offline simulation** — local price generator, no network.
- **Online simulation** — real market feed + local limit-order matching. No trader API, no external orders.
- **Market only** — read-only quotes via CTP market API.
- **External simulation / live read-only** — requires your own broker credentials, entered locally and never stored.

Credentials are kept in memory only; nothing is written to config, logs, or the repository.

Local simulation supports GFD/FAK/FOK orders, close-before-open net orders, best-limit and sniper algorithms, conditional orders, and two-leg synthetic spreads. Open **配置 / 工具 → 本地策略 / 双腿价差** to manage algorithms. Legs execute separately and can leave unmatched positions; these algorithms never send external orders.

Use **SimNow 快速配置…** on the login page to fill a trading-hours or 7×24 reference preset. Enter your own InvestorID and account password locally; applying a preset does not connect or enable external orders. Confirm current endpoint details in your SimNow console. Use **清除连接配置** to remove the saved connection parameters and session credentials, or **只接行情，改用在线模拟** to use market data with local matching instead of a trading account.

The login page can check all market TCP endpoints in the background. A reachable port is not a successful CTP login or a verified feed, and historical success does not imply current availability.

## Build (PyInstaller onedir)

```bash
pip install -r requirements-desktop.txt pyinstaller
python -m PyInstaller --noconfirm packaging/pyinstaller/PyTrade.spec
```

CI builds all three platforms on every tag (`v*`); artifacts are attached to the GitHub Release.

## Verify a build

```bash
PyTrade/PyTrade --diagnostics diag.json --native-check
PyTrade/PyTrade --smoke-test --screenshot shots/
```

## Test

```bash
PYTHONPATH=src python -m unittest discover -s tests
```

## Scope

- Market data verified against several CTP fronts (1/5 depth observed).
- External order routing (simulation broker account) not yet integration-tested; live order placement is disabled.
- The narrow DOM provides explicit local algorithm mappings: A = best-limit, B = sniper, CntrbySprd = two-leg spread, and M-OrderX = conditional order. These mappings are available in offline and online local simulation; equivalence to the reference client's algorithms remains unverified. Spread positions can be opened or closed, with both legs checked before starting a close task.

## License

MIT. Bundled sound files are from the original reference software package.

Selected trading algorithms adapt MIT-licensed [VeighNa/vn.py](https://github.com/vnpy/vnpy) modules. Copyright notices and source commits are retained in [assets/third_party/vnpy](assets/third_party/vnpy).
