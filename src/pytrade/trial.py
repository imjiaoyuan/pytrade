import base64
import hmac
import hashlib
import os
import struct
import sys
from datetime import datetime, timedelta
from pathlib import Path

TRIAL_HOURS = 24
STATE_NAME = ".cache.bin"
UNLOCK_NAME = "pytrade.key"
STATE_MAGIC = b"PT01"
ROLLBACK_TOLERANCE = timedelta(minutes=10)
SETTINGS_KEY = "cache/v1"

_SECRET = bytes.fromhex(
    "6d1f4b8e2c0a9537d84e21fb7c93ae5510d6482f7b3e5a19cc407d82ea5b36f1"
)
_UNLOCK_DIGEST = "d97cfe45f193cfedc20eeef5bae64cd9b8df1fc3ff50b8636b141ef5f7f2fcbc"


class TrialExpired(Exception):
    pass


def _mac(payload):
    return hmac.new(b"pytrade-trial\x00" + payload[:8], payload, hashlib.sha256).digest()


def _encode(first_run, last_run):
    seconds_of_day = int((first_run - first_run.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds())
    payload = (
        STATE_MAGIC
        + struct.pack(">III", first_run.toordinal(), seconds_of_day, last_run.toordinal())
    )
    return payload + _mac(payload)


def _decode(blob):
    if not isinstance(blob, (bytes, bytearray)) or len(blob) != 4 + 12 + 32:
        raise ValueError("trial state size")
    blob = bytes(blob)
    payload, mac = blob[:-32], blob[-32:]
    if payload[:4] != STATE_MAGIC:
        raise ValueError("trial state magic")
    if not hmac.compare_digest(_mac(payload), mac):
        raise ValueError("trial state signature")
    ordinal, seconds_of_day, last_ordinal = struct.unpack(">III", payload[4:])
    if seconds_of_day >= 86400 or last_ordinal < ordinal:
        raise ValueError("trial state fields")
    return datetime.fromordinal(ordinal) + timedelta(seconds=seconds_of_day), datetime.fromordinal(last_ordinal)


class TrialState:
    def __init__(self, first_run, last_run, expired_reason=None):
        self.first_run = first_run
        self.last_run = last_run
        self.expired_reason = expired_reason

    @property
    def deadline(self):
        return self.first_run + timedelta(hours=TRIAL_HOURS)

    def remaining(self, now=None):
        now = now or datetime.now()
        return self.deadline - now

    def remaining_text(self, now=None):
        left = self.remaining(now)
        seconds = left.total_seconds()
        if seconds <= 0:
            return "已到期限"
        hours, rem = divmod(int(seconds), 3600)
        if hours >= 1:
            return f"{hours} 小时" if rem < 60 else f"{hours} 小时 {rem // 60} 分"
        return f"{int(seconds // 60) + 1} 分钟"

    def label(self, now=None):
        if self.expired_reason:
            return f"试用版 · {self.expired_reason}"
        return f"试用版 · 剩余 {self.remaining_text(now)}"


class _FileRecord:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        return _decode(self.path.read_bytes())

    def write(self, blob):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(blob)


class _SettingsRecord:
    def __init__(self, settings, key):
        self.settings = settings
        self.key = key

    def read(self):
        text = self.settings.value(self.key, "")
        if not text:
            raise ValueError("trial settings empty")
        return _decode(base64.b64decode(text))

    def write(self, blob):
        self.settings.setValue(self.key, base64.b64encode(blob).decode("ascii"))
        self.settings.sync()


def _settings_record():
    try:
        from PySide6.QtCore import QCoreApplication, QSettings
        app = QCoreApplication.instance()
        if app is None or not app.organizationName() or not app.applicationName():
            return None
        return _SettingsRecord(QSettings(), SETTINGS_KEY)
    except Exception:
        return None


def _state_paths(state_dir, install_root):
    paths = [Path(state_dir) / STATE_NAME]
    if install_root is not None:
        alt = Path(install_root) / STATE_NAME
        if alt.parent != paths[0].parent:
            paths.append(alt)
    return paths


def _unlock_candidates(install_root):
    roots = [install_root, _default_install_root()]
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    seen, paths = set(), []
    for root in roots:
        if root is None:
            continue
        path = Path(root) / UNLOCK_NAME
        if path.parent not in seen:
            seen.add(path.parent)
            paths.append(path)
    return paths


def unlock_file_valid(install_root=None):
    for path in _unlock_candidates(install_root):
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if hmac.compare_digest(content, _UNLOCK_DIGEST):
            return True
    return False


def trial_enabled(install_root=None):
    if os.environ.get("PYTRADE_NO_TRIAL") == "1":
        return False
    if unlock_file_valid(install_root if install_root is not None else _default_install_root()):
        return False
    return bool(TRIAL_HOURS and TRIAL_HOURS > 0)


def _default_install_root():
    from .resources import application_root
    return application_root()


def load_trial(state_dir, install_root=None, now=None):
    now = now or datetime.now()
    records = [_FileRecord(path) for path in _state_paths(state_dir, install_root)]
    settings = _settings_record()
    if settings is not None:
        records.append(settings)
    valid = []
    for record in records:
        try:
            valid.append((record, record.read()))
        except (OSError, ValueError, OverflowError, TypeError):
            continue
    if not valid:
        first_run = now.replace(microsecond=0)
        _persist(valid, [(record, (first_run, first_run)) for record in records], first_run, first_run)
        return TrialState(first_run, first_run)
    first_run = min(pair[1][0] for pair in valid)
    last_run = max(pair[1][1] for pair in valid)
    if last_run > now + ROLLBACK_TOLERANCE or now < first_run - ROLLBACK_TOLERANCE:
        _persist(valid, [(record, (first_run, last_run)) for record in records], first_run, max(last_run, now.replace(microsecond=0)))
        return TrialState(first_run, last_run, "检测到系统时钟异常")
    state = TrialState(first_run, max(last_run, now.replace(microsecond=0)))
    _persist(valid, [(record, (state.first_run, state.last_run)) for record in records], state.first_run, state.last_run)
    if state.remaining(now).total_seconds() <= 0:
        return TrialState(state.first_run, state.last_run, "试用已到期")
    return state


def _persist(valid_records, all_records, first_run, last_run):
    blob = _encode(first_run, last_run)
    for record, _ in all_records:
        try:
            record.write(blob)
        except (OSError, ValueError):
            continue


def check_trial(state_dir, install_root=None, now=None):
    state = load_trial(state_dir, install_root, now)
    if state.expired_reason:
        raise TrialExpired(state.expired_reason)
    return state


def current_trial_state(state_dir, install_root=None, now=None):
    if not trial_enabled(install_root):
        return None
    try:
        return load_trial(state_dir, install_root, now)
    except Exception:
        return TrialState(datetime.now(), datetime.now(), "试用状态异常，请联系开发者")


def enforce_trial_at_startup(state_dir, install_root=None):
    from PySide6.QtWidgets import QApplication, QMessageBox
    if not trial_enabled(install_root):
        return None
    state = current_trial_state(state_dir, install_root)
    if state and state.expired_reason:
        app = QApplication.instance()
        QMessageBox.warning(
            None,
            "试用期已结束",
            f"{state.label()}\n\n如需继续使用，请联系开发者获取正式版本。",
        )
        if app is not None:
            app.quit()
        raise SystemExit(0)
    return state
