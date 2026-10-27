"""阶段1：从视频拆出音轨。

产物:
    01_audio/audio_master.wav   48k 立体声，母带
    01_audio/audio_asr.wav      16k 单声道，供 ASR

用法:
    python scripts/s01_extract_audio.py projects/20261008_demo_clip [--force]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from lib import audio
from lib.config import load_project
from lib.state import run_stage

STAGE = "s01_extract_audio"


def main() -> None:
    ap = argparse.ArgumentParser(description="阶段1：拆音轨")
    ap.add_argument("project", help="项目目录")
    ap.add_argument("--force", action="store_true", help="强制重跑")
    ap.add_argument("--seg", help="兼容 pipeline --seg 透传，本阶段忽略")
    args = ap.parse_args()

    problems = audio.check_ffmpeg()
    if problems:
        raise SystemExit("ffmpeg 不可用：\n  - " + "\n  - ".join(problems))

    project_dir = Path(args.project).resolve()
    project = load_project(project_dir)
    cfg = project["config"]

    source_rel = project["source_file"]
    source = project_dir / source_rel
    if not source.exists():
        raise SystemExit(f"源文件不存在: {source}")

    master = project_dir / "01_audio" / "audio_master.wav"
    asr = project_dir / "01_audio" / "audio_asr.wav"

    def produce() -> list[str]:
        audio.extract_audio(source, master, cfg["audio"]["master_sample_rate"], 2)
        audio.extract_audio(source, asr, cfg["audio"]["asr_sample_rate"], 1)
        return ["01_audio/audio_master.wav", "01_audio/audio_asr.wav"]

    ran = run_stage(
        project_dir, STAGE, [source_rel], cfg, produce,
        config_keys=["audio"], force=args.force,
    )

    if ran or master.exists():
        print(f"母带时长: {audio.probe_duration(master):.3f}s")


if __name__ == "__main__":
    main()
