"""阶段10：装配成片。

产物:
    09_output/<project_id>_zh.mp4   中文配音成片（视频流直拷，替换音轨）
    09_output/<project_id>_zh.srt   中文字幕

源视频来自 `project.json` 的 `source_file`，音轨用 s09 的 `08_mix/mix.wav`。
视频流 `-c:v copy` 不重编码；音轨转 AAC。若源文件没有视频流，则只导出音频
（`.m4a`）。字幕用 ASR 时间轴 + 最终译文（编辑层优先）。

用法:
    python scripts/s10_assemble.py projects/20261008_demo_clip [--force]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from lib import audio
from lib.config import load_project
from lib.schemas import load_json, resolve_edited
from lib.state import run_stage
from lib.subtitles import write_srt

STAGE = "s10_assemble"
MIX_REL = "08_mix/mix.wav"
SEGMENTS_REL = "03_asr/segments.json"
TRANSLATION_REL = "04_translate/translation.json"
OUT_DIR = "09_output"


def _escape_filter_path(p: Path) -> str:
    """subtitles 滤镜的路径转义（Windows 盘符冒号与反斜杠）。"""
    return str(p).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def build_subtitles(project_dir) -> list[dict]:
    segments_path = resolve_edited(project_dir, SEGMENTS_REL)
    translation_path = resolve_edited(project_dir, TRANSLATION_REL)
    if not segments_path.exists() or not translation_path.exists():
        return []

    targets = {
        it["id"]: (it.get("target") or "").strip()
        for it in load_json(translation_path).get("segments", [])
    }
    items: list[dict] = []
    for seg in load_json(segments_path).get("segments", []):
        text = targets.get(seg["id"], "")
        if not text:
            continue
        items.append({"start": float(seg["start"]), "end": float(seg["end"]), "text": text})
    return items


def main() -> None:
    ap = argparse.ArgumentParser(description="阶段10：装配成片")
    ap.add_argument("project", help="项目目录")
    ap.add_argument("--force", action="store_true", help="强制重跑")
    ap.add_argument("--seg", action="append", help="（本阶段忽略，仅为与调度器参数对齐）")
    args = ap.parse_args()

    audio.use_bundled_ffmpeg()

    project_dir = Path(args.project).resolve()
    project = load_project(project_dir)

    mix = project_dir / MIX_REL
    if not mix.exists():
        raise SystemExit(f"缺少 {MIX_REL}，请先运行 s09_mix")

    source = project_dir / project["source_file"]
    if not source.exists():
        raise SystemExit(f"源视频不存在: {source}")

    out_dir = project_dir / OUT_DIR
    project_id = project["project_id"]
    has_video = audio.has_video(source)
    video_rel = f"{OUT_DIR}/{project_id}_zh.mp4"
    audio_rel = f"{OUT_DIR}/{project_id}_zh.m4a"
    srt_rel = f"{OUT_DIR}/{project_id}_zh.srt"

    sub_cfg = project["config"].get("subtitle", {})
    burn_in = bool(sub_cfg.get("burn_in"))

    def produce() -> list[str]:
        out_dir.mkdir(parents=True, exist_ok=True)
        outputs: list[str] = []

        if audio.has_subtitle(source):
            print("[assemble] 提示：源视频带内封字幕流（不继承；中文配音字幕另见下方输出）")

        items = build_subtitles(project_dir)
        if items:
            write_srt(items, project_dir / srt_rel)
            outputs.append(srt_rel)
            print(f"[assemble] 字幕 {len(items)} 条 -> {srt_rel}")

        if has_video:
            items = build_subtitles(project_dir)
            srt_path = project_dir / srt_rel
            if burn_in and items:
                style = (
                    f"FontName={sub_cfg.get('font', 'Microsoft YaHei')},"
                    f"FontSize={sub_cfg.get('font_size', 26)},"
                    f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                    f"BorderStyle=1,Outline=1,Shadow=0,MarginV={sub_cfg.get('margin_v', 40)}"
                )
                vf = f"subtitles='{_escape_filter_path(srt_path)}':force_style='{style}'"
                audio.run([
                    "-y", "-i", str(source), "-i", str(mix),
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-vf", vf,
                    "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
                    "-c:a", "aac", "-b:a", "192k",
                    "-shortest", str(project_dir / video_rel),
                ])
                print(f"[assemble] 成片（已烧录中文字幕）-> {video_rel}")
            else:
                audio.run([
                    "-y", "-i", str(source), "-i", str(mix),
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    "-shortest", str(project_dir / video_rel),
                ])
                print(f"[assemble] 成片 -> {video_rel}")
            outputs.append(video_rel)
        else:
            audio.run([
                "-y", "-i", str(mix), "-c:a", "aac", "-b:a", "192k",
                str(project_dir / audio_rel),
            ])
            outputs.append(audio_rel)
            print(f"[assemble] 源无视频流，导出音频 -> {audio_rel}")

        return outputs

    ran = run_stage(
        project_dir, STAGE, [MIX_REL, project["source_file"]], project["config"], produce,
        config_keys=["audio", "subtitle"], force=args.force,
    )

    if ran or (project_dir / (video_rel if has_video else audio_rel)).exists():
        print(f"完成：交付物在 {OUT_DIR}/")


if __name__ == "__main__":
    main()
