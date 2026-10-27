"""ffmpeg / ffprobe 封装。

优先使用项目内置的完整版 ffmpeg（tools/ffmpeg/bin/），
因为系统 PATH 里的 ffmpeg 可能是精简版（缺少音频编解码器）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_AUDIO_ENCODERS = ("pcm_s16le",)


def _candidates(name: str) -> list[str]:
    exe = f"{name}.exe" if os.name == "nt" else name
    out: list[str] = []
    env = os.environ.get(f"{name.upper()}_BIN")
    if env:
        out.append(env)
    out.append(str(PROJECT_ROOT / "tools" / "ffmpeg" / "bin" / exe))
    which = shutil.which(name)
    if which:
        out.append(which)
    return out


def _resolve(name: str) -> str:
    for candidate in _candidates(name):
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError(
        f"未找到 {name}。请安装完整版 ffmpeg 到 tools/ffmpeg/bin/，"
        f"或设置环境变量 {name.upper()}_BIN。"
    )


def ffmpeg_bin() -> str:
    return _resolve("ffmpeg")


def ffprobe_bin() -> str:
    return _resolve("ffprobe")


def use_bundled_ffmpeg() -> None:
    """把项目内置的 ffmpeg/ffprobe 目录前置到 PATH。

    有些第三方库（demucs、whisperx）会直接按名字调用 `ffmpeg` / `ffprobe`，
    绕开本模块的解析逻辑；而系统 PATH 上可能是精简版，导致解析失败。
    """
    bindir = str(Path(ffmpeg_bin()).parent)
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep)
    if bindir not in parts:
        os.environ["PATH"] = bindir + os.pathsep + current


def check_ffmpeg() -> list[str]:
    """返回问题列表；空列表表示当前 ffmpeg 可用。"""
    try:
        exe = ffmpeg_bin()
    except RuntimeError as exc:
        return [str(exc)]

    encoders = subprocess.run(
        [exe, "-hide_banner", "-encoders"], capture_output=True, text=True
    ).stdout
    problems: list[str] = []
    for enc in REQUIRED_AUDIO_ENCODERS:
        if enc not in encoders:
            problems.append(f"{exe} 缺少音频编码器 {enc}（可能是精简版 ffmpeg）")
    return problems


def segment_rms_db(path, start: float, dur: float) -> float | None:
    """测量 [start, start+dur] 窗口的整体 RMS（dBFS）。

    静音（<= -100 dBFS）、测量失败或窗口无效返回 None。
    用 ffmpeg astats，不依赖 numpy，任何环境可跑。
    """
    if dur <= 0.05:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg_bin(), "-hide_banner", "-nostats",
             "-ss", f"{max(start, 0):.3f}", "-t", f"{dur:.3f}", "-i", str(path),
             "-af", "astats=measure_overall=RMS_level:measure_perchannel=none",
             "-f", "null", "-"],
            capture_output=True, text=True,
        )
    except (RuntimeError, subprocess.SubprocessError):
        return None
    for line in proc.stderr.splitlines():
        if "RMS level dB:" in line:
            try:
                value = float(line.split("RMS level dB:")[1].strip())
            except ValueError:
                return None
            return None if value <= -100 else value
    return None


def probe_duration(path) -> float:
    """探测媒体时长（秒）。

    优先用 ffprobe；若当前 ffprobe 是精简版、无法解析该容器，
    则退回解析 ffmpeg 的 stderr。
    """
    try:
        out = subprocess.run(
            [
                ffprobe_bin(),
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                str(path),
            ],
            capture_output=True, text=True,
        )
        if out.returncode == 0:
            value = json.loads(out.stdout).get("format", {}).get("duration")
            if value is not None:
                return float(value)
    except (RuntimeError, ValueError, subprocess.SubprocessError):
        pass

    proc = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    if not match:
        raise RuntimeError(f"无法探测时长: {path}")
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def probe_streams(path) -> list[dict]:
    """列出媒体文件的流信息；解析失败时返回空列表。"""
    try:
        out = subprocess.run(
            [
                ffprobe_bin(),
                "-v", "error",
                "-show_entries", "stream=codec_type",
                "-of", "json",
                str(path),
            ],
            capture_output=True, text=True,
        )
    except (RuntimeError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    try:
        return json.loads(out.stdout).get("streams", [])
    except ValueError:
        return []


def has_video(path) -> bool:
    return any(s.get("codec_type") == "video" for s in probe_streams(path))


def has_subtitle(path) -> bool:
    """源容器是否带内封字幕流（mkv/mp4 的 srt/ass 等）。"""
    return any(s.get("codec_type") == "subtitle" for s in probe_streams(path))


def extract_audio(src, dst, sample_rate: int, channels: int) -> None:
    """从任意媒体文件中提取无损 PCM 音轨。"""
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin(),
        "-y",
        "-i", str(src),
        "-vn",
        "-ac", str(channels),
        "-ar", str(sample_rate),
        "-c:a", "pcm_s16le",
        str(dst),
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)


def trim_silence(src, dst, threshold_db: float = -45.0, keep: float = 0.15) -> bool:
    """砍掉头尾静音（TTS 输出普遍带 0.3~1s 拖尾，虚高时长让兜底倍率失真）。

    两端各保留 keep 秒余量，阈值 threshold_db。失败返回 False，调用方用原音频。
    """
    dst = Path(dst)
    chain = (
        f"areverse,silenceremove=start_periods=1:start_threshold={threshold_db}dB:"
        f"start_silence={keep},areverse,"
        f"silenceremove=start_periods=1:start_threshold={threshold_db}dB:"
        f"start_silence={keep}"
    )
    try:
        run(["-i", str(src), "-filter:a", chain, "-y", str(dst)])
    except Exception:  # noqa: BLE001 - 修剪失败不算致命
        return False
    return dst.exists() and dst.stat().st_size > 44


def tempo_fit(src, dst, tempo: float) -> bool:
    """atempo 变速贴合：把音频拉快 tempo 倍（仅 s07 超预算兜底用）。

    tempo > 2.0 时拆成多级链式 filter（ffmpeg 单级 atempo 限 0.5~2.0），
    例如 x2.66 -> atempo=2.0,atempo=1.33。成功且产物存在返回 True；
    ffmpeg 失败返回 False，调用方保留原音频继续走原分类逻辑。
    """
    tempo = max(0.25, tempo)
    dst = Path(dst)
    stages: list[str] = []
    remaining = tempo
    while remaining > 2.0:
        stages.append("atempo=2.0")
        remaining /= 2.0
    if remaining > 0.25:
        stages.append(f"atempo={remaining:.4f}")
    try:
        run(["-i", str(src), "-filter:a", ",".join(stages), "-y", str(dst)])
    except Exception:  # noqa: BLE001 - 兜底路径不许抛
        return False
    return dst.exists() and dst.stat().st_size > 44


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """以统一参数调用 ffmpeg。"""
    proc = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", *args],
        capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败：{' '.join(args)}\n{proc.stderr}")
    return proc


def cut(src, dst, start: float, end: float, sample_rate: int | None = None,
        channels: int | None = None) -> None:
    """截取 [start, end) 区间为 PCM wav。"""
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    args = ["-y", "-ss", f"{max(start, 0.0):.3f}", "-to", f"{max(end, 0.0):.3f}", "-i", str(src)]
    if channels:
        args += ["-ac", str(channels)]
    if sample_rate:
        args += ["-ar", str(sample_rate)]
    args += ["-c:a", "pcm_s16le", str(dst)]
    run(args)


def concat(parts, dst, sample_rate: int | None = None, channels: int | None = None) -> None:
    """按顺序拼接多个 wav（同一格式）。"""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    listfile = dst.parent / f".{dst.stem}.concat.txt"
    listfile.write_text(
        "\n".join(f"file '{Path(p).resolve().as_posix()}'" for p in parts) + "\n",
        encoding="utf-8",
    )
    args = ["-y", "-f", "concat", "-safe", "0", "-i", str(listfile)]
    if channels:
        args += ["-ac", str(channels)]
    if sample_rate:
        args += ["-ar", str(sample_rate)]
    args += ["-c:a", "pcm_s16le", str(dst)]
    try:
        run(args)
    finally:
        listfile.unlink(missing_ok=True)


def silence(dst, duration: float, sample_rate: int, channels: int = 1) -> None:
    """生成指定时长的静音 wav。"""
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    run([
        "-y", "-f", "lavfi",
        "-i", f"anullsrc=r={sample_rate}:cl={'mono' if channels == 1 else 'stereo'}",
        "-t", f"{duration:.3f}", "-c:a", "pcm_s16le", str(dst),
    ])


def change_speed(src, dst, speed: float, sample_rate: int, channels: int) -> None:
    """变速不变调（优先 rubberband，退回 atempo）。"""
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    speed = max(speed, 0.01)
    args = ["-y", "-i", str(src), "-filter:a"]
    filters = []
    # rubberband 一次只支持 0.25~4.0，超出就串 atempo
    remaining = speed
    while remaining > 4.0:
        filters.append("atempo=4.0")
        remaining /= 4.0
    while remaining < 0.25:
        filters.append("atempo=0.25")
        remaining /= 0.25
    filters.append(f"rubberband=tempo={remaining:.6f}")
    args.append(",".join(filters))
    args += ["-ac", str(channels), "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(dst)]
    run(args)

