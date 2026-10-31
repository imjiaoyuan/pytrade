import argparse
import hashlib
import json
import struct
from pathlib import Path


class FormReader:
    def __init__(self, raw, offset):
        self.raw = raw
        self.position = offset

    def take(self, count):
        if count < 0 or self.position + count > len(self.raw):
            raise ValueError("窗体资源越界")
        value = self.raw[self.position:self.position + count]
        self.position += count
        return value

    def byte(self):
        return self.take(1)[0]

    def number(self, code):
        return struct.unpack(code, self.take(struct.calcsize(code)))[0]

    def short(self):
        return self.take(self.byte()).decode("gb18030")

    def properties(self):
        properties = {}
        while True:
            name = self.short()
            if not name:
                return properties
            properties[name] = self.value()

    def value(self):
        kind = self.byte()
        if kind in {0, 13}:
            return None
        if kind == 1:
            values = []
            while self.raw[self.position] != 0:
                values.append(self.value())
            self.take(1)
            return values
        if kind in {2, 3, 4, 19}:
            return self.number({2: "<b", 3: "<h", 4: "<i", 19: "<q"}[kind])
        if kind == 5:
            return dict(extended_bytes=self.take(10).hex())
        if kind in {6, 7}:
            return self.short()
        if kind in {8, 9}:
            return kind == 9
        if kind == 10:
            return dict(binary_bytes=len(self.take(self.number("<I"))))
        if kind == 11:
            values = []
            while True:
                name = self.short()
                if not name:
                    return values
                values.append(name)
        if kind in {12, 18, 20}:
            count = self.number("<I")
            encoding, width = ("utf-16-le", 2) if kind == 18 else ("utf-8", 1) if kind == 20 else ("gb18030", 1)
            return self.take(count * width).decode(encoding)
        if kind == 14:
            values = []
            while self.raw[self.position] != 0:
                if self.byte() != 1:
                    raise ValueError("不支持的窗体集合编码")
                values.append(self.properties())
            self.take(1)
            return values
        if kind in {15, 17, 21}:
            return self.number("<f" if kind == 15 else "<d")
        if kind == 16:
            return self.number("<q") / 10000
        raise ValueError("不支持的窗体属性编码")

    def component(self, depth=0):
        if depth > 50:
            raise ValueError("窗体嵌套过深")
        if self.raw[self.position] & 0xf0 == 0xf0:
            flags = self.byte() & 0xf
            if flags & 2:
                self.value()
        classname = self.short()
        name = self.short()
        if not classname.startswith("T") or not name:
            raise ValueError("无效的窗体对象")
        properties = self.properties()
        children = []
        while self.raw[self.position] != 0:
            children.append(self.component(depth + 1))
        self.take(1)
        return dict(classname=classname, name=name, properties=properties, children=children)


def extract(executable):
    raw = executable.read_bytes()
    offset = 0
    failures = []
    while True:
        offset = raw.find(b"TPF0", offset)
        if offset < 0:
            break
        reader = FormReader(raw, offset + 4)
        try:
            component = reader.component()
            if component["classname"] == "TYYWin":
                return component, offset, reader.position, hashlib.sha256(raw).hexdigest()
        except (ValueError, UnicodeError, IndexError, struct.error):
            failures.append(offset)
        offset += 4
    raise ValueError(f"未提取到DOM窗体；有{len(failures)}个非窗体签名或未识别资源")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, default=Path("reference_materials/original_software/0401.exe"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("请使用新文件名保留历史证据")
    component, start, end, checksum = extract(args.exe)
    keep = {"Left", "Top", "Width", "Height", "ClientWidth", "ClientHeight", "PixelsPerInch", "TextHeight", "Font.Name", "Font.Height", "Font.Color", "Font.Charset", "Caption", "Hint", "Color", "Align", "Visible", "Enabled", "Checked", "RowCount", "ColCount", "DefaultRowHeight", "DefaultColWidth", "Text"}
    components = []

    def collect(node, root=False):
        properties = {key: value for key, value in node["properties"].items() if key in keep or key.startswith("On")}
        if root:
            properties.pop("Caption", None)
            properties.pop("Hint", None)
        components.append(dict(classname=node["classname"], name=node["name"], properties=properties))
        for child in node["children"]:
            collect(child)

    collect(component, True)
    report = dict(source=str(args.exe), sha256=checksum, evidence_kind="static DFM properties; not original runtime behavior", byte_start=start, byte_end=end, components=components, documentation="https://docwiki.embarcadero.com/Libraries/Sydney/en/System.Classes.TValueType")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(components=len(components), byte_start=hex(start), byte_end=hex(end)), ensure_ascii=False))


if __name__ == "__main__":
    main()
