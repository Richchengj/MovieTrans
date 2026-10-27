"""统一入口：按顺序调度各阶段。

用法:
    python scripts/pipeline.py projects/20261008_demo_clip            # 跑到已实现的最后一步
    python scripts/pipeline.py projects/20261008_demo_clip --from s04_asr
    python scripts/pipeline.py projects/20261008_demo_clip --only s01_extract_audio
    python scripts/pipeline.py projects/20261008_demo_clip --force
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
from lib import audio  # noqa: E402

STAGES: list[tuple[str, str]] = [
    ("s01_extract_audio", "s01_extract_audio.py"),
    ("s02_separate", "s02_separate.py"),
    ("s03_denoise", "s03_denoise.py"),
    ("s04_asr", "s04_asr.py"),
    ("s05_translate", "s05_translate.py"),
    ("s06_voice_profile", "s06_voice_profile.py"),
    ("s07_tts", "s07_tts.py"),
    ("s08_align", "s08_align.py"),
    ("s09_mix", "s09_mix.py"),
    ("s10_assemble", "s10_assemble.py"),
]


def warn_review_pending(project_dir: Path) -> None:
    """s07 前置提醒：review 段先精简再合成，避免「合成完→改译文→整段重跑」。

    非阻塞：只提示，不影响自动运行（TTS 约占全链路 70%，废合成是最大浪费源）。
    """
    tpath = project_dir / "04_translate" / "translation.json"
    if not tpath.exists():
        return
    try:
        d = json.loads(tpath.read_text(encoding="utf-8"))
        segs = d.get("segments", d) if isinstance(d, dict) else d
        review = [s.get("id") for s in segs if s.get("strategy") == "review"]
    except Exception:  # noqa: BLE001 - 提醒而已，解析失败就闭嘴
        return
    if review:
        print(f"[warn] 有 {len(review)} 段译文超预算待精简（见 04_translate/review.csv）")
        print("[warn] 建议先在 R1 复核精简完再进 s07——现在继续自动合成，改完译文后这些段会重跑")


def main() -> int:
    ap = argparse.ArgumentParser(description="流水线调度器")
    ap.add_argument("project", help="项目目录")
    ap.add_argument("--from", dest="from_stage", help="从该阶段开始")
    ap.add_argument("--only", dest="only_stage", help="只跑该阶段")
    ap.add_argument("--force", action="store_true", help="强制重跑")
    ap.add_argument("--seg", help="只处理指定段落（透传给阶段脚本）")
    args = ap.parse_args()

    project_dir = Path(args.project).resolve()
    if not (project_dir / "project.json").exists():
        raise SystemExit(f"不是有效项目目录: {project_dir}")

    names = [name for name, _ in STAGES]
    if args.only_stage:
        selected = [args.only_stage]
    elif args.from_stage:
        if args.from_stage not in names:
            raise SystemExit(f"未知阶段: {args.from_stage}（可选: {', '.join(names)}）")
        selected = names[names.index(args.from_stage):]
    else:
        selected = names

    for name, filename in STAGES:
        if name not in selected:
            continue
        script = SCRIPTS_DIR / filename
        if not script.exists():
            print(f"[todo] {name}：脚本尚未实现，停止", flush=True)
            break

        if name == "s07_tts":
            warn_review_pending(project_dir)

        cmd = [sys.executable, str(script), str(project_dir)]
        if args.force:
            cmd.append("--force")
        if args.seg:
            cmd += ["--seg", args.seg]

        # 必须 flush：stdout 被重定向时是块缓冲的，子进程直接写同一个 fd，
        # 不刷的话阶段标题会排到子进程输出之后，看起来像乱序。
        print(f"\n=== {name} ===", flush=True)
        code = subprocess.call(cmd)
        if code != 0:
            print(f"[fail] {name} 退出码 {code}，流水线中止", flush=True)
            return code

        # ASR 完成后检查：没有可翻译语音（纯演奏/音乐视频）→ 交付原样成片
        if name == "s04_asr":
            seg_path = project_dir / "03_asr" / "segments.json"
            n_segs = 0
            if seg_path.exists():
                try:
                    n_segs = len(json.loads(seg_path.read_text(encoding="utf-8")).get("segments", []))
                except (json.JSONDecodeError, OSError):
                    n_segs = 0
            if n_segs == 0:
                out_dir = project_dir / "09_output"
                out_dir.mkdir(parents=True, exist_ok=True)
                source = project_dir / json.loads(
                    (project_dir / "project.json").read_text(encoding="utf-8-sig")
                )["source_file"]
                ext = ".mp4" if audio.has_video(source) else ".m4a"
                dst = out_dir / f"{project_dir.name}_zh{ext}"
                shutil.copyfile(source, dst)
                print(
                    f"[pipeline] 未检测到语音（纯演奏/音乐视频），跳过 s05-s10；"
                    f"原样成片 -> 09_output/{dst.name}",
                    flush=True,
                )
                break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
