import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path


def sensitive_key(key):
    return bool(re.search(r"password|passwd|secret|token|auth|account|investor|userid|(?:^|_)user(?:name|id)?$|^pw$|密码|账号|授权", key, re.I))


@dataclass
class DesktopConfig:
    font: str = "新宋体"
    row_height: int = 13
    dom_height: int = 960
    wheel_steps: int = 3

    def validate(self):
        for name, low, high in (("row_height", 10, 30), ("dom_height", 540, 1800), ("wheel_steps", 1, 100)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} 须为 {low}–{high} 的整数")
        if not isinstance(self.font, str) or not self.font.strip() or len(self.font) > 100:
            raise ValueError("字体名称无效")


def parse_legacy(path):
    raw = Path(path).read_bytes()
    if len(raw) > 2_000_000:
        raise ValueError("配置文件超过 2MB")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            source = raw.decode(encoding)
            break
        except UnicodeError:
            continue
    else:
        raise ValueError("配置编码无法识别")
    entries, diagnostics, candidates = [], [], {}
    section, seen = "", set()
    mapping = {"MainFont": "font", "TickRowHeights": "row_height", "InstrumentWindowHeights": "dom_height", "MouseWheelSpeed": "wheel_steps"}
    for number, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
        elif "=" in line and not stripped.startswith((";", "#")):
            key, value = (part.strip() for part in line.split("=", 1))
            secret = sensitive_key(key)
            entries.append(dict(section=section, key=key, value="[已移除敏感字段]" if secret else value, line=number))
            identity = (section, key)
            if identity in seen:
                diagnostics.append(f"第 {number} 行 {key} 重复，原值逐项保留；显示候选采用最后一项")
            seen.add(identity)
            if secret:
                diagnostics.append(f"第 {number} 行敏感字段不导入")
                continue
            if key in mapping:
                name = mapping[key]
                try:
                    candidate = value if name == "font" else int(value)
                    DesktopConfig(**{name: candidate}).validate()
                    candidates[name] = candidate
                except ValueError:
                    diagnostics.append(f"第 {number} 行 {key} 显示参数无效，未应用")
            elif key.startswith("MOrderTime") and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d", value):
                diagnostics.append(f"第 {number} 行 {key} 时间格式未确认，保留原值")
            elif "PORT" in key.upper():
                if not value.isdigit() or not 1 <= int(value) <= 65535:
                    diagnostics.append(f"第 {number} 行 {key} 端口无效，保留但不启用")
            elif "Ratios" in key:
                try:
                    values = [int(part) for part in value.split(",")]
                    if not values or any(part <= 0 for part in values):
                        raise ValueError()
                except ValueError:
                    diagnostics.append(f"第 {number} 行 {key} 比例无效，保留但不启用")
        elif stripped and not stripped.startswith((";", "#")):
            diagnostics.append(f"第 {number} 行无法识别，未应用")
    if Path(path).suffix.lower() == ".xml" and not source.lstrip().startswith("<"):
        diagnostics.append("扩展名为 XML，实际按键值配置解析")
    return dict(encoding=encoding, entries=entries, candidates=candidates, diagnostics=diagnostics)


class ConfigStore:
    def __init__(self, directory):
        self.path = Path(directory) / "desktop.json"
        self.imported, self.overrides, self.legacy = {}, {}, []
        self.error = ""
        if self.path.exists():
            try:
                state = json.loads(self.path.read_text(encoding="utf-8"))
                if state.get("version") != 1:
                    raise ValueError("不支持的桌面配置版本")
                self.imported = state["imported"]
                self.overrides = state["overrides"]
                self.legacy = state["legacy"]
                self.effective()
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                self.imported, self.overrides, self.legacy = {}, {}, []
                self.error = "桌面配置无效，已使用默认显示；原文件保留，请修复后再保存"
        self.effective()

    def effective(self):
        config = DesktopConfig(**(self.imported | self.overrides))
        config.validate()
        return config

    def apply_import(self, parsed):
        old_imported, old_legacy = self.imported, self.legacy
        self.imported = parsed["candidates"]
        self.legacy = parsed["entries"]
        try:
            self.save()
        except (ValueError, OSError):
            self.imported, self.legacy = old_imported, old_legacy
            raise

    def set_overrides(self, values):
        config = DesktopConfig(**values)
        config.validate()
        old_overrides = self.overrides
        self.overrides = asdict(config)
        try:
            self.save()
        except (ValueError, OSError):
            self.overrides = old_overrides
            raise

    def save(self):
        if self.error:
            raise ValueError(self.error)
        self.effective()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(dict(version=1, imported=self.imported, overrides=self.overrides, legacy=self.legacy), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


class AccountStore:
    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        if self.path.exists():
            try:
                state = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("profiles"), dict):
                    raise ValueError("账号档案格式无效")
                for name, profile in state["profiles"].items():
                    if not isinstance(name, str) or not name.strip() or not isinstance(profile, dict):
                        raise ValueError("账号档案条目无效")
                    if any(sensitive_key(key) and key not in {"auth_code_used", "app_id"} for key in profile):
                        raise ValueError("账号档案包含不允许保存的字段")
                self.data = state["profiles"]
            except (OSError, ValueError, TypeError) as error:
                self.error = f"账号档案不可读（{error}），本次以空档案启动；原文件保留"
        self.error = ""

    def read(self):
        return {name: dict(profile) for name, profile in self.data.items()}

    def save_profile(self, name, values, previous=""):
        if not name or len(name) > 40:
            raise ValueError("账号名称须为1–40个字符")
        for key in values:
            if sensitive_key(key) and key != "app_id":
                raise ValueError(f"凭据字段不保存：{key}")
        values = {key: value for key, value in values.items() if key not in {"user"}}
        if previous and previous != name and previous in self.data:
            order = list(self.data)
            order[order.index(previous)] = name
            self.data = {key: (values if key == name else self.data[key]) for key in order}
        else:
            self.data[name] = dict(values)
        self.save()

    def delete(self, name):
        if name not in self.data:
            raise ValueError("账号不存在")
        del self.data[name]
        self.save()

    def reorder(self, name, offset):
        names = list(self.data)
        if name not in names:
            raise ValueError("账号不存在")
        index = names.index(name) + offset
        if not 0 <= index < len(names):
            return
        names.remove(name)
        names.insert(index, name)
        self.data = {key: self.data[key] for key in names}
        self.save()

    def note_used(self, name):
        from datetime import datetime
        if name in self.data:
            self.data[name]["used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            self.save()

    def instruments(self, name):
        stored = self.data.get(name, {}).get("instruments")
        return dict(stored) if isinstance(stored, dict) else {}

    def save_instruments(self, name, catalog):
        if name not in self.data:
            raise ValueError("账号不存在")
        self.data[name]["instruments"] = {key: dict(value) for key, value in catalog.items()}
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(dict(version=1, profiles=self.data), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


def copy_instrument_table(store, source, target):
    if source not in store.data or target not in store.data:
        raise ValueError("源账号或目标账号不存在")
    instruments = store.instruments(source)
    if not instruments:
        raise ValueError(f"账号“{source}”没有已保存的合约表；连接后自动保存核验结果")
    store.save_instruments(target, instruments)
    return len(instruments)
