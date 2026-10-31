# PyTrade

Cross-platform desktop client for Chinese futures market data and trading, rebuilt with Python 3.12 + PySide6 + CTP (openctp-ctp).

![CI](https://github.com/OWNER/REPO/actions/workflows/ci.yml/badge.svg)

## Status

Preview (0.1.0rc1). Real market data works; external order routing is not yet validated against a live broker account. See [Scope](#scope).

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
- Unknown behaviors of the reference client are intentionally disabled (FOK, spread orders, A/B buttons).

## License

MIT. Bundled sound files are from the original reference software package.
