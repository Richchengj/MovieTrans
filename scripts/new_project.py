"""新建项目：建立目录骨架并登记源文件。

用法:
    python scripts/new_project.py 20261008_demo_clip "D:\\path\\to\\clip.mp4"
    python scripts/new_project.py 20261008_demo "clip.mp4" --out E:\\dub_projects
    python scripts/new_project.py 20261023_x "clip.mp4" --config-from projects/20261022_bcs_zero2_sf

默认建在 <仓库>/projects/ 下；--out 可指定别的父目录（比如仓库所在盘
空间紧张时把项目放到大盘——项目目录与仓库互相独立，流水线照常工作）。

配置继承（2026-10-22）：新项目默认会**自动挑最近一个带 API Key 的项目**
继承 `translation` 与 `tts` 两段配置。起因是 A 项目（44.5 分钟纪录片）
按 DEFAULT_CONFIG 建出来是 `deepseek` + 空 key，s05 静默退回本地 MarianMT
（review 占 32%），白跑一遍翻译。用 --config-from 可显式指定模板。
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

from lib.config import DEFAULT_CONFIG, _deep_merge

STAGE_DIRS = [
    "00_source", "01_audio", "02_stems", "03_asr", "04_translate",
    "05_voices", "06_tts", "07_align", "08_mix", "09_output", "work", "logs",
]

# 只继承「跟机器/账号绑定」的两段；其余（对齐、混音、字幕）一律用默认值，
# 免得把一个项目的临时调参带进下一个项目
INHERIT_SECTIONS = ("translation", "tts")


def _read_cfg(project_dir: Path) -> dict:
    try:
        blob = json.loads((project_dir / "project.json").read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return blob.get("config") or {}


def _has_key(cfg: dict) -> bool:
    tr = cfg.get("translation") or {}
    sf = (cfg.get("tts") or {}).get("siliconflow") or {}
    return bool(tr.get("api_key") or sf.get("api_key"))


def find_template(root: Path) -> Path | None:
    """在 root 下找最近一个带 API Key 的项目当配置模板。"""
    if not root.exists():
        return None
    cands = [p for p in root.iterdir() if p.is_dir() and (p / "project.json").exists()]
    cands.sort(key=lambda p: (p / "project.json").stat().st_mtime, reverse=True)
    for proj in cands:
        if _has_key(_read_cfg(proj)):
            return proj
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="新建翻译项目")
    ap.add_argument("project_id", help="项目ID，如 20261008_demo_clip")
    ap.add_argument("source", help="源视频文件路径")
    ap.add_argument("--out", help="项目父目录（默认 <仓库>/projects/）")
    ap.add_argument("--config-from", help="配置模板项目目录（默认自动挑最近带 Key 的项目）")
    ap.add_argument("--no-inherit", action="store_true", help="不继承任何项目配置，全用默认值")
    args = ap.parse_args()

    root = Path(args.out).resolve() if args.out else Path(__file__).resolve().parent.parent / "projects"
    project_dir = root / args.project_id
    if project_dir.exists():
        raise SystemExit(f"项目已存在: {project_dir}")

    source = Path(args.source).resolve()
    if not source.exists():
        raise SystemExit(f"源文件不存在: {source}")

    for d in STAGE_DIRS:
        (project_dir / d).mkdir(parents=True, exist_ok=True)

    shutil.copy2(source, project_dir / "00_source" / source.name)

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    repo_projects = Path(__file__).resolve().parent.parent / "projects"
    template = None
    if not args.no_inherit:
        if args.config_from:
            template = Path(args.config_from).resolve()
        else:
            # 先看项目父目录，再看仓库默认 projects/（--out 到别的盘时也能继承）
            template = find_template(root)
            if template is None and root != repo_projects:
                template = find_template(repo_projects)
    inherited: list[str] = []
    if template is not None:
        if not (template / "project.json").exists():
            raise SystemExit(f"配置模板不存在: {template}")
        tcfg = _read_cfg(template)
        for key in INHERIT_SECTIONS:
            if isinstance(tcfg.get(key), dict):
                config[key] = _deep_merge(config[key], tcfg[key])
                inherited.append(key)
        # 模板自带的 api_key 就足够；避免继承到一个没有 key 的模板还沾沾自喜
        if not _has_key(config):
            print(f"[new] ⚠ 模板 {template.name} 未带 API Key，"
                  "首次 s05 会退回本地翻译模型（译文质量低）")

    project = {
        "project_id": args.project_id,
        "created": datetime.now().isoformat(timespec="seconds"),
        "source_file": f"00_source/{source.name}",
        # 原始完整路径：成片按原名归拢（collect_outputs.py）反查章节结构用
        "source_origin": str(source),
        "config": config,
    }
    (project_dir / "project.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (project_dir / "state.json").write_text(
        json.dumps({"project_id": args.project_id, "stages": {}}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已创建项目: {project_dir}")
    if inherited:
        tr = config["translation"]
        print(f"[new] 继承 {template.name} 的 {', '.join(inherited)}："
              f"翻译={tr.get('provider')}/{tr.get('model')}"
              f"（key {'有' if tr.get('api_key') else '无'}），"
              f"TTS={config['tts'].get('engine')}")
    elif not args.no_inherit:
        print("[new] 未找到可继承的模板（没有项目带 API Key），使用默认配置："
              "s05 会因缺 Key 退回本地模型，建议先填 translation.api_key")


if __name__ == "__main__":
    main()
