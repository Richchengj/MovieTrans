"""阶段9：混音 + 响度归一化。

产物:
    08_mix/dub.wav   拼好时间轴的纯配音轨
    08_mix/mix.wav   配音 + 背景，EBU R128 归一化后的成品音轨

先把 s08 的每段配音按 `03_asr/segments.json` 的起点铺到整条时间轴上
（静音底 + adelay + amix），得到 dub.wav；
再和 s02 的背景 `02_stems/no_vocals.wav` 相加。背景按 `mix.background_gain_db`
衰减，并用配音做侧链压缩（`mix.duck_db`）实现闪避，最后 `loudnorm` 到
`mix.target_lufs`，输出 48k 立体声。

用法:
    python scripts/s09_mix.py projects/20261008_demo_clip [--force]
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from lib import audio
from lib.config import load_project
from lib.schemas import load_json, resolve_edited
from lib.state import run_stage

STAGE = "s09_mix"
REPORT_REL = "07_align/align_report.json"
SEGMENTS_REL = "03_asr/segments.json"
BACKGROUND_REL = "02_stems/no_vocals.wav"
MASTER_REL = "01_audio/audio_master.wav"
DUB_REL = "08_mix/dub.wav"
MIX_REL = "08_mix/mix.wav"

OUT_SR = 48000


def _wav_seconds(path: Path) -> float:
    """读 wav 头拿时长（比 ffprobe 快两个数量级，批量截断判断用）。"""
    import wave

    try:
        with wave.open(str(path), "rb") as f:
            return f.getnframes() / float(f.getframerate() or 1)
    except Exception:  # noqa: BLE001 - 读不到就按 0 处理（不截断）
        return 0.0


def _render_window(placed: list[tuple[float, Path]], w0: float, w1: float,
                   sr: int, dst: Path) -> None:
    """把一个时间窗内的段落 amix 成 wav，长度固定 = w1 - w0。"""
    length = max(w1 - w0, 0.05)
    silence = dst.parent / f".sil_{dst.stem}.wav"
    audio.silence(silence, length, sr, 1)
    try:
        args = ["-y", "-i", str(silence)]
        for _, path in placed:
            args += ["-i", str(path)]
        labels = ["[0:a]"]
        filters = []
        for i, (start, path) in enumerate(placed, start=1):
            ms = int(round(max(start - w0, 0.0) * 1000))
            chain = f"[{i}:a]aresample={sr},aformat=channel_layouts=mono"
            # 跨窗口的段在窗口右边界截断，保证 concat 后总长精确
            over = start + _wav_seconds(path) - w1
            if over > 0.005:
                keep = max(length - max(start - w0, 0.0), 0.02)
                chain += f",atrim=0:{keep:.3f}"
            filters.append(f"{chain},adelay={ms}[a{i}]")
            labels.append(f"[a{i}]")
        filters.append(
            f"{''.join(labels)}amix=inputs={len(placed) + 1}:normalize=0:duration=first[out]"
        )
        args += [
            "-filter_complex", ";".join(filters),
            "-map", "[out]", "-ar", str(sr), "-ac", "1", "-c:a", "pcm_s16le", str(dst),
        ]
        audio.run(args)
    finally:
        silence.unlink(missing_ok=True)


def build_dub(placed: list[tuple[float, Path]], total: float, sr: int, dst: Path,
              max_batch: int = 100) -> None:
    """静音底 + 各段 adelay 后 amix，拼出整条配音轨。

    段数一多（长片）就必须分批：整条 222 段的输入列表 + filter_complex 会把
    Windows CreateProcess 的命令行顶到 32767 字符上限，实测直接
    `FileNotFoundError: [WinError 206] 文件名或扩展名太长`（2026-10-22，12 分钟片）。
    这里按段序切成 ≤max_batch 段的批，每批覆盖一个时间窗 [本批首段起点, 下批首段起点)，
    批内独立 amix，跨窗段在右边界截断，最后按窗口顺序 concat。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    placed = sorted(placed, key=lambda x: x[0])
    if not placed:
        audio.silence(dst, max(total, 0.1), sr, 1)
        return

    batches = [placed[i:i + max_batch] for i in range(0, len(placed), max_batch)]
    if len(batches) == 1:
        _render_window(placed, 0.0, max(total, 0.1), sr, dst)
        return

    tmp_dir = dst.parent / ".dub_parts"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[Path] = []
    try:
        for bi, batch in enumerate(batches):
            w0 = batch[0][0] if bi else 0.0
            w1 = batches[bi + 1][0][0] if bi + 1 < len(batches) else max(total, w0 + 0.1)
            chunk = tmp_dir / f"chunk_{bi:04d}.wav"
            _render_window(batch, w0, w1, sr, chunk)
            chunks.append(chunk)
        print(f"[mix] 配音轨分 {len(batches)} 批渲染（每批 ≤{max_batch} 段，规避命令行长度上限）")
        audio.concat([str(c) for c in chunks], str(dst), sr, 1)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def mix_with_background(dub: Path, background: Path | None, dst: Path, cfg: dict) -> None:
    mix = cfg["mix"]
    bg_db = float(mix["background_gain_db"])
    duck_db = float(mix["duck_db"])
    lufs = float(mix["target_lufs"])

    filters = [f"[0:a]aresample={OUT_SR},aformat=channel_layouts=stereo[voice]"]
    args = ["-y", "-i", str(dub)]

    if background is not None and background.exists():
        args += ["-i", str(background)]
        filters.append(
            f"[1:a]aresample={OUT_SR},aformat=channel_layouts=stereo,volume={bg_db}dB[bgsrc]"
        )
        if duck_db < 0:
            ratio = max(1.0, min(20.0, -duck_db))
            filters.append(
                f"[bgsrc][voice]sidechaincompress=threshold=0.05:ratio={ratio:.2f}"
                f":attack=20:release=400:level_sc=1[bg]"
            )
        else:
            filters.append("[bgsrc]anull[bg]")
        filters.append("[voice][bg]amix=inputs=2:normalize=0:duration=first[mixed]")
    else:
        filters.append("[voice]anull[mixed]")

    filters.append(f"[mixed]loudnorm=I={lufs}:TP=-1.5:LRA=11[out]")
    args += [
        "-filter_complex", ";".join(filters),
        "-map", "[out]", "-ar", str(OUT_SR), "-ac", "2", "-c:a", "pcm_s16le", str(dst),
    ]
    audio.run(args)


def main() -> None:
    ap = argparse.ArgumentParser(description="阶段9：混音与响度归一化")
    ap.add_argument("project", help="项目目录")
    ap.add_argument("--force", action="store_true", help="强制重跑")
    ap.add_argument("--seg", action="append", help="（本阶段忽略，仅为与调度器参数对齐）")
    args = ap.parse_args()

    audio.use_bundled_ffmpeg()

    project_dir = Path(args.project).resolve()
    project = load_project(project_dir)
    cfg = project["config"]

    report_path = resolve_edited(project_dir, REPORT_REL)
    if not report_path.exists():
        raise SystemExit(f"缺少 {REPORT_REL}，请先运行 s08_align")
    report = load_json(report_path)

    segments_path = resolve_edited(project_dir, SEGMENTS_REL)
    if not segments_path.exists():
        raise SystemExit(f"缺少 {SEGMENTS_REL}，请先运行 s04_asr")
    starts = {
        seg["id"]: float(seg["start"])
        for seg in load_json(segments_path).get("segments", [])
    }

    master = project_dir / MASTER_REL
    background = project_dir / BACKGROUND_REL
    if not background.exists():
        print(f"[mix] 未找到 {BACKGROUND_REL}，将只输出配音轨")

    placed: list[tuple[float, Path]] = []
    for entry in report.get("segments", []):
        file_rel = entry.get("file")
        if not file_rel:
            continue
        path = project_dir / file_rel
        if not path.exists():
            continue
        start = entry.get("start")
        if start is None:
            start = starts.get(entry["id"])
        if start is None:
            print(f"[mix] {entry['id']} 缺少起点，跳过")
            continue
        placed.append((float(start), path))
    placed.sort(key=lambda item: item[0])

    if not placed:
        raise SystemExit("没有可混音的段落（检查 s07/s08 是否产出）")

    def total_duration() -> float:
        if master.exists():
            return audio.probe_duration(master)
        return max(start + audio.probe_duration(path) for start, path in placed)

    def produce() -> list[str]:
        total = total_duration()
        build_dub(placed, total, OUT_SR, project_dir / DUB_REL)
        mix_with_background(
            project_dir / DUB_REL, background, project_dir / MIX_REL, cfg
        )
        print(f"[mix] 配音 {len(placed)} 段，总长 {total:.3f}s，输出 {MIX_REL}")
        return [DUB_REL, MIX_REL]

    input_rels = [
        str(report_path.relative_to(project_dir)).replace("\\", "/"),
        str(segments_path.relative_to(project_dir)).replace("\\", "/"),
    ] + [str(path.relative_to(project_dir)).replace("\\", "/") for _, path in placed]
    if master.exists():
        input_rels.append(MASTER_REL)
    if background.exists():
        input_rels.append(BACKGROUND_REL)

    ran = run_stage(
        project_dir, STAGE, input_rels, cfg, produce,
        config_keys=["mix", "audio"], force=args.force,
    )

    if ran or (project_dir / MIX_REL).exists():
        print("提示：请试听 08_mix/mix.wav，确认无双重人声、响度合适（R4 卡点）")


if __name__ == "__main__":
    main()
