# Cross-platform spec: produces PyTrade.exe on Windows, PyTrade elsewhere.
import sys
from pathlib import Path

repo = Path(SPECPATH).resolve().parents[1]

datas = [
    (repo / 'data/service_catalog.json', 'data'),
    (repo / 'data/Instruments.snapshot.xml', 'data'),
    (repo / 'data/original_dom_spec.json', 'data'),
    (repo / 'assets/sounds', 'assets/sounds'),
]

hiddenimports = ['openctp_ctp', 'openctp_ctp.mdapi', 'openctp_ctp.traderapi']

linux_only = []
if sys.platform == 'linux':
    linux_only = [(repo / 'packaging/pyinstaller/locale', '.runtime/locale')]

a = Analysis(
    [str(repo / 'src/main.py')],
    pathex=[str(repo / 'src')],
    binaries=[],
    datas=datas + linux_only,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PyTrade',
    debug=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='PyTrade',
)