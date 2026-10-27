"""阶段3：降噪去混响（DeepFilterNet，可选）。

产物:
    02_stems/vocals_clean.wav   降噪后的人声

输入是 s02 的 `02_stems/vocals.wav`。走官方预编译 CLI
（`tools/deepfilternet/deep-filter.exe`），不依赖任何 Python 环境。
该 CLI 会自行处理采样率，输出保持输入的采样率与声道数，
这里再统一编码为配置的母带采样率。

`denoise.enabled` 为 false 时不做降噪，仍产出 `vocals_clean.wav`
（原样转码），以保证下游始终有同一个稳定路径可读。

用法:
    python scripts/s03_denoise.py projects/20261008_demo_clip [--force]
"""
from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

from lib import audio
from lib.config import load_project
from lib.state import run_stage

STAGE = "s03_denoise"
INPUT_REL = "02_stems/vocals.wav"
OUTPUT_REL = "02_stems/vocals_clean.wav"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEEP_FILTER = PROJECT_ROOT / "tools" / "deepfilternet" / "deep-filter.exe"


def run_deep_filter(src: Path, out_dir: Path) -> Path:
    """调用 deep-filter 降噪，返回输出文件路径。"""
    if not DEEP_FILTER.exists():
        raise RuntimeError(f"未找到 DeepFilterNet CLI: {DEEP_FILTER}")

    subprocess.run(
        [str(DEEP_FILTER), str(src), "-o", str(out_dir)],
        capture_output=True,
        text=True,
        check=True,
    )

    produced = out_dir / src.name
    if not produced.exists():
        raise RuntimeError(f"deep-filter 未产出预期文件: {produced}")
    return produced


def main() -> None:
    ap = argparse.ArgumentParser(description="阶段3：降噪去混响")
    ap.add_argument("project", help="项目目录")
    ap.add_argument("--force", action="store_true", help="强制重跑")
    ap.add_argument("--seg", help="兼容 pipeline --seg 透传，本阶段忽略")
    args = ap.parse_args()

    project_dir = Path(args.project).resolve()
    project = load_project(project_dir)
    cfg = project["config"]
    enabled = bool(cfg["denoise"]["enabled"])
    out_sr = cfg["audio"]["master_sample_rate"]

    vocals = project_dir / INPUT_REL
    if not vocals.exists():
        raise SystemExit(f"缺少 {INPUT_REL}，请先运行 s02_separate")

    stems_dir = project_dir / "02_stems"
    work_dir = project_dir / "work" / "deepfilter"

    def produce() -> list[str]:
        stems_dir.mkdir(parents=True, exist_ok=True)
        out_path = project_dir / OUTPUT_REL

        if not enabled:
            print("[denoise] denoise.enabled = false，跳过降噪，原样产出")
            audio.extract_audio(vocals, out_path, out_sr, 2)
            return [OUTPUT_REL]

        work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run_", dir=work_dir) as tmp:
            cleaned = run_deep_filter(vocals, Path(tmp))
            audio.extract_audio(cleaned, out_path, out_sr, 2)
        return [OUTPUT_REL]

    ran = run_stage(
        project_dir, STAGE, [INPUT_REL], cfg, produce,
        config_keys=["audio", "denoise"], force=args.force,
    )

    if ran or (project_dir / OUTPUT_REL).exists():
        out_path = project_dir / OUTPUT_REL
        print(f"{OUTPUT_REL}: {audio.probe_duration(out_path):.3f}s")
        if not enabled:
            print("提示：降噪当前未启用，可把 project.json 的 denoise.enabled 改为 true")


if __name__ == "__main__":
    main()