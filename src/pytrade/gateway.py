import multiprocessing
import queue
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from .ctp_worker import worker_main
from .resources import locale_path
from .protocol import command_message


class CtpGateway(QObject):
    event = Signal(dict)
    stopped = Signal()

    def __init__(self, config, state_dir, parent=None):
        super().__init__(parent)
        self.context = multiprocessing.get_context("spawn")
        self.incoming = self.context.Queue(maxsize=1000)
        self.outgoing = self.context.Queue(maxsize=20000)
        flow = Path(state_dir) / "ctp" / uuid.uuid4().hex
        self.process = self.context.Process(target=worker_main, args=(config, self.incoming, self.outgoing, str(flow), str(locale_path())), daemon=True)
        self.closing = False
        self.deadline = None
        self.reported_exit = False
        self.metrics = dict(received=0, maximum_batch=0, observed_backlog=0, maximum_poll_ms=0.0)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(50)
        self.process.start()

    def send(self, message):
        if self.closing:
            raise ValueError("CTP 正在断开，请等待完成")
        try:
            self.incoming.put_nowait(command_message(message))
        except queue.Full:
            self.event.emit({"kind": "fatal", "message": "CTP 命令队列已满，禁止交易；请断开后重新对账"})
            raise ValueError("CTP 命令队列已满")

    def poll(self):
        started = time.perf_counter()
        count = 0
        if not self.closing:
            for _ in range(1000):
                try:
                    event = self.outgoing.get_nowait()
                except queue.Empty:
                    break
                self.event.emit(event)
                count += 1
                if time.perf_counter() - started >= 0.008:
                    break
        self.metrics["received"] += count
        self.metrics["maximum_batch"] = max(self.metrics["maximum_batch"], count)
        self.metrics["maximum_poll_ms"] = max(self.metrics["maximum_poll_ms"], (time.perf_counter() - started) * 1000)
        try:
            self.metrics["observed_backlog"] = max(self.metrics["observed_backlog"], self.outgoing.qsize())
        except (OSError, NotImplementedError):
            pass
        if not self.process.is_alive():
            if not self.closing and not self.reported_exit:
                self.reported_exit = True
                self.event.emit({"kind": "fatal", "message": f"CTP 进程已退出({self.process.exitcode})，连接失效，须重连对账"})
            if self.closing:
                self.finish()
        elif self.closing and time.monotonic() > self.deadline:
            self.process.kill()
            self.deadline = time.monotonic() + 1

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.deadline = time.monotonic() + 3
        try:
            self.incoming.put_nowait({"command": "stop"})
        except queue.Full:
            self.process.terminate()

    def finish(self):
        self.timer.stop()
        self.process.join(timeout=0)
        for channel in (self.incoming, self.outgoing):
            channel.cancel_join_thread()
            channel.close()
        self.stopped.emit()
