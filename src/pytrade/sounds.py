from .resources import application_root


SOUND_FILES = {"accepted": "chimes.wav", "fill": "cashreg.wav", "canceled": "Cancellation.wav", "nomoney": "Nomoney.wav"}


CUE_LABELS = {"accepted": "委托受理", "fill": "成交", "canceled": "撤单", "nomoney": "资金不足拒单"}


class SoundTracker:
    def __init__(self):
        self.source = None
        self.orders = {}
        self.fills = set()

    def observe(self, source, orders, fills, audible=True):
        cues = []
        if source is self.source and audible:
            for identifier, status in orders.items():
                previous = self.orders.get(identifier)
                if status == "已撤单" and previous != status:
                    cues.append("canceled")
                elif status in {"已受理", "未成交"} and previous not in {status, "部分成交", "全部成交", "已撤单"}:
                    cues.append("accepted")
                elif status == "资金不足" and previous not in {status, "已撤单"}:
                    cues.append("nomoney")
            cues.extend("fill" for identifier in fills if identifier not in self.fills)
        self.source = source
        self.orders = dict(orders)
        self.fills = set(fills)
        return cues


class TradeSounds:
    def __init__(self, parent, report):
        self.parent = parent
        self.report = report
        self.enabled = True
        self.same_order_cancel = False
        self.effects = None
        self.failed = False
        self.unsupported = set()

    def play(self, cue):
        if not self.enabled or self.failed:
            return
        from PySide6.QtGui import QGuiApplication
        if QGuiApplication.platformName() in {"offscreen", "minimal"}:
            return
        if self.same_order_cancel and cue == "accepted":
            cue = "canceled"
        if self.effects is None:
            try:
                from PySide6.QtCore import QUrl
                from PySide6.QtMultimedia import QSoundEffect
                self.effects = {}
                for name, filename in SOUND_FILES.items():
                    effect = QSoundEffect(self.parent)
                    effect.setVolume(0.65)
                    effect.statusChanged.connect(lambda effect=effect: self.check_status(effect))
                    effect.setSource(QUrl.fromLocalFile(str(application_root() / "assets" / "sounds" / filename)))
                    self.effects[name] = effect
            except (ImportError, OSError, RuntimeError):
                self.failed = True
                self.report("音效初始化失败，请检查 QtMultimedia 与音频设备；交易状态不受影响")
                return
        effect = self.effects[cue]
        if cue in self.unsupported:
            return
        for current in self.effects.values():
            current.stop()
        effect.play()

    def check_status(self, effect):
        if effect.status() == effect.Status.Error and not self.failed:
            cue = next((name for name, value in self.effects.items() if value is effect), "")
            if cue == "nomoney":
                self.unsupported.add(cue)
                self.report("Nomoney.wav 加载失败，资金不足提示音不可用；其余音效与交易状态不受影响")
                return
            self.failed = True
            self.report("原版音效加载失败，请检查音频设备和 assets/sounds 文件")
