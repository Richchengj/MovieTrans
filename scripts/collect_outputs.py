r"""成片归拢：把各项目的成片按源视频原名复制到统一的「成片汇总」目录。

用法:
    python src/collect_outputs.py                    # 归拢 <projects-root> 下全部项目
    python src/collect_outputs.py --project 20261011_step2_shape   # 只归拢一个项目

规则:
    - 目标：<projects-root>\成片汇总\<源视频所在目录>\<源原名>_中文配音.mp4/.m4a
      （源所在目录取 project.json 的 source_origin 反推；批量新项目自动记录）
    - 老项目没有 source_origin 时，可用 --search-root（如课程根目录）按文件名反查
    - 已存在同名文件跳过（幂等，可反复跑）
    - batch_class.py 在每个项目质检通过后自动调用本模块
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

SUFFIX_TAG = "_中文配音"


def project_origin(data: dict, project_dir: Path, search_root: Path | None) -> Path | None:
    """源视频的原始完整路径：优先 source_origin，老项目回退按文件名反查。"""
    origin = data.get("source_origin")
    if origin and Path(origin).exists():
        return Path(origin)
    name = Path(data["source_file"]).name
    if search_root and search_root.exists():
        hits = list(search_root.rglob(name))
        if hits:
            return hits[0]
    return None


def collect_project(project_dir: Path, dest_root: Path,
                    search_root: Path | None = None) -> list[Path]:
    data = json.loads((project_dir / "project.json").read_text(encoding="utf-8-sig"))
    out_dir = project_dir / "09_output"
    vids = sorted(out_dir.glob("*_zh.mp4")) + sorted(out_dir.glob("*_zh.m4a"))
    if not vids:
        return []

    origin = project_origin(data, project_dir, search_root)
    if origin is not None:
        chapter = origin.parent.name
        stem = origin.stem
    else:
        chapter, stem = "未归档", Path(data["source_file"]).stem
    dest_dir = dest_root / chapter
    dest_dir.mkdir(parents=True, exist_ok=True)

    copied = []
    for v in vids:
        dst = dest_dir / f"{stem}{SUFFIX_TAG}{v.suffix}"
        if not dst.exists():
            shutil.copy2(v, dst)
            copied.append(dst)
    return copied


def main() -> int:
    ap = argparse.ArgumentParser(description="成片按原名归拢")
    ap.add_argument("--projects-root", default=None,
                    help="项目根目录（默认 <仓库>/projects/ 或环境 DUB_PROJECTS_DIR）")
    ap.add_argument("--project", help="只归拢指定项目 id")
    ap.add_argument("--dest", default=None, help="汇总目标目录（默认 <projects-root>\\成片汇总）")
    ap.add_argument("--search-root", default=None,
                    help="老项目反查源文件的搜索根（如课程根目录）")
    args = ap.parse_args()

    root = (Path(args.projects_root) if args.projects_root
            else Path(__file__).resolve().parents[1] / "projects")
    dest_root = Path(args.dest) if args.dest else root / "成片汇总"
    search_root = Path(args.search_root) if args.search_root else None

    if args.project:
        pdir = root / args.project
        projects = [pdir / "project.json"] if (pdir / "project.json").exists() else []
    else:
        projects = sorted(root.glob("*/project.json"))
    total = 0
    for pj in projects:
        if not pj.is_file():
            continue
        try:
            copied = collect_project(pj.parent, dest_root, search_root)
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            print(f"  [fail] {pj.parent.name}: {exc}", file=sys.stderr)
            continue
        for c in copied:
            print(f"  {pj.parent.name} -> {c}")
        total += len(copied)
    print(f"归拢完成：{total} 个新文件 -> {dest_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
