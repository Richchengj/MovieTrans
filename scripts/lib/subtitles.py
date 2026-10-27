"""字幕文件生成。"""
from __future__ import annotations

from pathlib import Path


def fmt_time(seconds: float) -> str:
    """秒 -> SRT 时间戳 HH:MM:SS,mmm"""
    ms = int(round(max(seconds, 0.0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(items: list[dict], path, text_key: str = "text") -> str:
    """items 为含 start / end / <text_key> 的字典列表。"""
    lines: list[str] = []
    for i, it in enumerate(items, 1):
        lines.append(str(i))
        lines.append(f"{fmt_time(it['start'])} --> {fmt_time(it['end'])}")
        lines.append(str(it[text_key]))
        lines.append("")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")
    return str(p)
