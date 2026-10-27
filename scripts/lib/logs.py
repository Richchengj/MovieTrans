"""阶段日志：把控制台输出同时写入 `logs/<stage>.log`。

按 SPEC 第 6 节约定，每个阶段一个日志文件。实现方式是给 sys.stdout /
sys.stderr 套一层「同时写控制台与文件」的转发，因此第三方库（demucs 的
进度条、whisperx 的日志）打到 stderr 的内容也会被收进来。

只在阶段真正执行时安装：被跳过的阶段不覆盖上一次的日志，
以免丢掉上一次真实运行的详细输出。
"""
from __future__ import annotations

import atexit
import sys
from datetime import datetime
from pathlib import Path


class _Tee:
    """把写入转发到多个流。"""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for stream in self._streams:
            stream.write(data)
        return len(data)

    def flush(self):
        for stream in self._streams:
            stream.flush()

    def isatty(self):
        # 沿用控制台的判断，避免 tqdm 之类的库在控制台退化成多行输出
        return bool(getattr(self._streams[0], "isatty", lambda: False)())

    def __getattr__(self, name):
        return getattr(self._streams[0], name)


class _StampWriter:
    """写文件时给每个完整行加 [HH:MM:SS] 前缀，便于事后核对时间线。

    一次 write 可能只有半行，所以按行缓冲，凑成完整行才落盘。
    """

    def __init__(self, handle):
        self._h = handle
        self._buf = ""

    def write(self, data):
        self._buf += data
        parts = self._buf.split("\n")
        self._buf = parts.pop()
        for line in parts:
            self._h.write(f"[{datetime.now().strftime('%H:%M:%S')}] {line.rstrip(chr(13))}\n")
        return len(data)

    def flush(self):
        if self._buf:
            self._h.write(f"[{datetime.now().strftime('%H:%M:%S')}] {self._buf.rstrip(chr(13))}\n")
            self._buf = ""
        self._h.flush()


_installed: Path | None = None
_state: dict = {}


def _teardown() -> None:
    """退出前先还原 sys.stdout/stderr，再关文件。

    顺序很关键：解释器在 finalization 阶段还会 flush 一次 sys.stdout，
    若此时它仍指向已关闭的文件，就会抛 "Exception ignored in ..." 并让
    进程以非 0 退出，进而被 pipeline 误判为阶段失败。
    """
    handle = _state.get("handle")
    if handle is None:
        return
    original = _state.get("original")
    if original:
        sys.stdout, sys.stderr = original
    try:
        handle.flush()
    finally:
        handle.close()
    _state["handle"] = None


def install(project_dir, stage: str) -> Path:
    """开始记录该阶段的日志；同一进程内只安装一次。"""
    global _installed
    if _installed is not None:
        return _installed

    log_dir = Path(project_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{stage}.log"

    handle = path.open("w", encoding="utf-8", newline="")
    handle.write(f"# {stage}\n")
    handle.write(f"# started {datetime.now().isoformat(timespec='seconds')}\n")
    handle.write(f"# argv: {' '.join(sys.argv)}\n\n")
    handle.flush()

    _state["original"] = (sys.stdout, sys.stderr)
    _state["handle"] = handle
    sys.stdout = _Tee(sys.stdout, _StampWriter(handle))
    sys.stderr = _Tee(sys.stderr, _StampWriter(handle))
    atexit.register(_teardown)

    _installed = path
    return path
