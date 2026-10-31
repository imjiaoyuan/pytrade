import atexit
import copy
import csv
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import shutil
import threading
from pathlib import Path
from uuid import uuid4


def transaction_paths(directory, config):
    identity = hashlib.sha256(f"{config.environment}|{config.broker}|{config.user}".encode()).hexdigest()
    legacy = hashlib.sha256(f"{config.environment}|{config.td_front}|{config.broker}|{config.user}".encode()).hexdigest()
    root = Path(directory) / "transactions"
    return root / f"{identity}.json", root / f"{legacy}.json"


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_file = open(self.path.with_suffix(self.path.suffix + ".lock"), "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.lock_file.write(b"0")
                self.lock_file.flush()
                self.lock_file.seek(0)
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            raise ValueError("同一账户交易日志正被另一实例使用，禁止并发交易") from None
        self.queue = queue.Queue(maxsize=1000)
        self.written = 0
        self.sequence = 0
        self.error = ""
        self.closed = False
        self.stop_requested = threading.Event()
        self.pending = None
        self.coalesced = 0
        self.thread = threading.Thread(target=self.run, name="transaction-writer", daemon=True)
        self.thread.start()

    def submit(self, state, target=None, owned=False):
        if self.closed:
            raise ValueError("交易日志已关闭，禁止继续提交")
        if self.error:
            raise ValueError(self.error)
        payload = state if owned else copy.deepcopy(state)
        self.sequence += 1
        try:
            self.queue.put_nowait((self.sequence, payload, target or self.path))
        except queue.Full:
            self.error = "交易持久化队列已满，禁止交易"
            raise ValueError(self.error)
        return self.sequence

    def archive(self, state, day):
        if not isinstance(day, str) or not day.isdigit() or len(day) != 8:
            raise ValueError("归档交易日无效")
        target = self.path.parent / "archive" / f"{self.path.stem}-{day}-{uuid4().hex}.json"
        return self.submit(state, target)

    def run(self):
        while True:
            try:
                if self.pending is not None:
                    item, self.pending = self.pending, None
                else:
                    item = self.queue.get(timeout=0.1)
            except queue.Empty:
                if self.stop_requested.is_set():
                    return
                continue
            batch = [item]
            try:
                if item is None:
                    return
                sequence, payload, target = item
                while True:
                    try:
                        candidate = self.queue.get_nowait()
                    except queue.Empty:
                        break
                    if candidate is None or candidate[2] != target:
                        self.pending = candidate
                        break
                    batch.append(candidate)
                    sequence, payload, target = candidate
                    self.coalesced += 1
                if not self.error:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    temporary = target.with_suffix(".tmp")
                    with open(temporary, "w", encoding="utf-8", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
                        json.dump(payload, stream, ensure_ascii=False)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, target)
                    if os.name == "posix":
                        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            os.fsync(directory)
                        finally:
                            os.close(directory)
                    self.written = sequence
            except Exception:
                self.error = "交易日志写入失败，禁止交易；请保留文件并重新对账"
            finally:
                for _ in batch:
                    self.queue.task_done()

    def flush(self):
        self.queue.join()
        if self.error:
            raise ValueError(self.error)

    def close(self):
        self.closed = True
        self.stop_requested.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise ValueError("交易日志尚未写完，禁止恢复交易")
        self.lock_file.close()
        if self.error:
            raise ValueError(self.error)


def load_journal(path):
    path = Path(path)
    if path.stat().st_size > 50_000_000:
        raise ValueError("交易日志超过大小上限")
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or type(state.get("version")) is not int or state.get("version") not in {1, 2} or not isinstance(state.get("orders"), dict) or not isinstance(state.get("trades"), dict):
        raise ValueError("交易日志格式无效")
    if any(not isinstance(row, dict) for name in ("orders", "trades") for row in state[name].values()):
        raise ValueError("交易日志记录结构无效")
    if state["version"] == 1:
        backup = path.with_suffix(".v1.backup.json")
        if not backup.exists():
            shutil.copy2(path, backup)
    return state


def export_csv(path, headers, rows):
    def cell(value):
        value = str(value if value is not None else "")
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value
    with open(path, "w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow([cell(value) for value in headers])
        writer.writerows([cell(value) for value in row] for row in rows)


def configure_logging(directory):
    handler = logging.handlers.RotatingFileHandler(Path(directory) / "application.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    messages = queue.Queue(maxsize=10000)
    listener = logging.handlers.QueueListener(messages, handler)
    logging.basicConfig(handlers=[logging.handlers.QueueHandler(messages)], level=logging.INFO, force=True)
    listener.start()
    atexit.register(listener.stop)
