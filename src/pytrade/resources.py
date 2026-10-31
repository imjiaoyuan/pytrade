import os
import sys
from pathlib import Path


def application_root():
    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
        internal = root / "_internal"
        return internal if internal.is_dir() else root
    if "__compiled__" in globals():
        return Path(sys.argv[0]).resolve().parent
    return Path(__file__).resolve().parents[2]


def reference_path(*parts):
    legacy = application_root().joinpath("reference_materials", *parts)
    if legacy.exists() and not application_root().joinpath("data", *parts).exists():
        return legacy
    return application_root().joinpath("data", *parts)


def locale_path():
    path = application_root() / ".runtime" / "locale"
    if (path / "zh_CN.GB18030").is_dir() or (path / "zh_CN.utf8").is_dir():
        os.environ.setdefault("LOCPATH", str(path))
    return path


def required_resources():
    return (
        data_path("service_catalog.json"),
        data_path("Instruments.snapshot.xml"),
        data_path("original_dom_spec.json"),
        *(application_root() / "assets" / "sounds" / name for name in ("Cancellation.wav", "cashreg.wav", "chimes.wav", "Nomoney.wav")),
    )


def data_path(*parts):
    return reference_path(*parts)
