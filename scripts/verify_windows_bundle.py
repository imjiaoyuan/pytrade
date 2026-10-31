import argparse
import hashlib
import json
import struct
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


RESOURCES = ("data/service_catalog.json", "data/Instruments.snapshot.xml", "data/original_dom_spec.json")
RESOURCES += tuple(f"assets/sounds/{name}" for name in ("Cancellation.wav", "cashreg.wav", "chimes.wav", "Nomoney.wav"))


def pe_machine(path):
    with path.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("缺少PE头")
        stream.seek(0x3c)
        offset = struct.unpack("<I", stream.read(4))[0]
        stream.seek(offset)
        if stream.read(4) != b"PE\0\0":
            raise ValueError("PE签名无效")
        return struct.unpack("<H", stream.read(2))[0]


def inspect_bundle(root):
    missing = [name for name in ("PyTrade.exe", *RESOURCES) if not (root / name).is_file()]
    files, invalid = {}, []
    paths = list(root.rglob("*"))
    if not any(path.name.lower() == "qwindows.dll" for path in paths):
        missing.append("Qt platforms/qwindows.dll")
    for path in paths:
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        with path.open("rb") as stream:
            files[relative] = hashlib.file_digest(stream, "sha256").hexdigest()
        if path.suffix.lower() in {".exe", ".dll", ".pyd"}:
            try:
                if pe_machine(path) != 0x8664:
                    invalid.append(relative)
            except (OSError, ValueError, struct.error):
                invalid.append(relative)
        if path.suffix.lower() in {".so", ".mp4"} or any(part.lower() in {"original_software", "screenshots", ".runtime", ".venv", "transactions"} for part in path.relative_to(root).parts):
            invalid.append(relative)
        if path.name.lower() in {"connection.json", "risk.json", "layout.json", "application.log"}:
            invalid.append(relative)
    if not any("openctp_ctp" in path.as_posix() and path.suffix.lower() == ".pyd" for path in paths):
        missing.append("openctp_ctp native extensions")
    return dict(layout_ok=not missing and not invalid, missing=missing, invalid_files=invalid, sha256=files)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--layout-only", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32" and not args.layout_only:
        parser.error("Windows原生验证必须在Windows执行；Linux只能使用 --layout-only")
    root = args.root.resolve()
    output = args.output.resolve()
    if root in output.parents:
        parser.error("验证报告须放在分发目录外，避免改变校验清单")
    report = dict(version=1, recorded_at_utc=datetime.now(timezone.utc).isoformat(), windows_native_checked=False, network_used=False, account_used=False, **inspect_bundle(root))
    if report["layout_ok"] and not args.layout_only:
        with tempfile.TemporaryDirectory(prefix="pytrade-bundle-") as directory:
            temporary = Path(directory)
            executable = root / "PyTrade.exe"
            native = temporary / "native.json"
            try:
                subprocess.run([str(executable), "--diagnostics", str(native), "--native-check"], cwd=directory, check=True, timeout=60)
                report["native"] = json.loads(native.read_text(encoding="utf-8"))
                if not report["native"].get("ok"):
                    raise ValueError("原生依赖检查未通过")
                subprocess.run([str(executable), "--state-dir", str(temporary / "state"), "--smoke-test", "--screenshot", str(temporary / "screens")], cwd=directory, check=True, timeout=120)
                report["gui_smoke"] = all((temporary / "screens" / name).is_file() for name in ("controller.png", "dom.png"))
                report["windows_native_checked"] = report["gui_smoke"]
            except (subprocess.SubprocessError, ValueError, OSError) as error:
                report["runtime_error"] = type(error).__name__
    report["ok"] = report["layout_ok"] and (args.layout_only or report["windows_native_checked"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "sha256"}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
