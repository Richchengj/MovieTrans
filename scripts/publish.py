"""发布包生成器（2026-10-18，E-3 工程评审项）。

把开发仓打成可换机重建的发布包，消除「手工同步四处」的漂移风险。

用法：
    python scripts/publish.py src            # 纯源码包 -> E:\\MovieTrans-src（约 150MB）
    python scripts/publish.py src --out X:\\  # 自定义输出根

SRC 包含：scripts/（流水线+lib+webui+fetch_models）、tools/ 源码与二进制
（ffmpeg 完整版 / deepfilternet CLI / index-tts 源码，**不含 .venv**）、
全部 md 文档、《换机说明.md》（自动生成）。
SRC 不含：envs/（venv 换机重建）、models/（权重走 fetch_models.py）、
projects/（项目数据不随包）、work/、__pycache__。

PUB（含 15GB 权重）与 dev（26GB 开发副本）目标位在 E 盘空间不足（30G）
时暂缓——扩盘或改发网盘后用 --target 扩展。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]

EXCLUDE_DIRS = {".workbuddy", "work", "__pycache__", ".venv", "envs", "models", "projects", ".git"}
EXCLUDE_SUFFIX = {".pyc", ".log", ".zip", ".tmp"}
KEEP_TOOLS = {"ffmpeg", "deepfilternet", "index-tts"}  # index-tts 仅源码，.venv 已在上方排除

DOCS = ["README.md", "README_BILINGUAL.md", "SPEC.md", "ROADMAP.md",
        "BENCHMARK.md", "需要人工操作.md", "TTS备选评估.md"]


def _ignore(dir: str, names: list[str]) -> set[str]:
    skip = set()
    for n in names:
        p = Path(dir) / n
        if p.is_dir() and (n in EXCLUDE_DIRS):
            skip.add(n)
        elif p.is_file() and (p.suffix in EXCLUDE_SUFFIX):
            skip.add(n)
    return skip


def build_src(out_root: str) -> Path:
    dst = Path(out_root) / "MovieTrans-src"
    if dst.exists():
        print(f"清理旧目标: {dst}")
        shutil.rmtree(dst)
    dst.mkdir(parents=True)

    # 1. scripts 全量（流水线 + lib + webui + fetch_models.py）
    shutil.copytree(SRC_ROOT / "scripts", dst / "scripts", ignore=_ignore)
    # 2. tools：只保留白名单（ffmpeg 二进制 / deepfilternet / index-tts 源码）
    (dst / "tools").mkdir()
    for name in KEEP_TOOLS:
        s = SRC_ROOT / "tools" / name
        if s.exists():
            shutil.copytree(s, dst / "tools" / name, ignore=_ignore)
    # 3. envs 只带 README（搭建说明），venv 本体不带
    (dst / "envs").mkdir()
    shutil.copy2(SRC_ROOT / "envs" / "README.md", dst / "envs" / "README.md")
    # 4. 文档
    for doc in DOCS:
        s = SRC_ROOT / doc
        if s.exists():
            shutil.copy2(s, dst / doc)
    # 5. 换机说明（自动生成）
    (dst / "换机说明.md").write_text(_onboarding_md(dst.name), encoding="utf-8")

    total = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
    print(f"OK -> {dst}  ({total / (1 << 20):.0f} MB)")
    return dst


def _onboarding_md(pkg_name: str) -> str:
    return f"""# {pkg_name} · 换机重建说明

从零在一台新 Windows 机器上恢复完整开发/运行能力。全程约 30–60 分钟
（主要是模型下载，取决于网速）。

## 0. 前置

- Python 3.12（`py -3.12`，**不要用系统自带精简版 python**）
- NVIDIA 显卡 + 驱动（无卡可跑 CPU，TTS 慢约 10 倍）
- 磁盘：C 盘预留 ~6GB（模型权重），D 盘放本包

## 1. 建四个 venv（细节见 envs/README.md）

```powershell
py -3.12 -m venv envs\\env_base
py -3.12 -m venv envs\\env_sep
py -3.12 -m venv envs\\env_asr
# env_tts 特殊：Python 3.11 + uv sync，见 envs/README.md「env_tts（IndexTTS）特殊说明」
```

## 2. 装依赖

按 envs/README.md 各节执行（pip 走清华源；torch 按驱动选 cu124/cu126，
**先装 whisperx 再回装 CUDA 版 torch**，顺序错了会换成 CPU 版）。

## 3. 下模型权重（约 6GB）

```powershell
python scripts/fetch_models.py all      # 走 hf-mirror，清单见脚本头部
```

模型落到 `models/`（建议 C 盘：`mklink /J C:\\dub_models <包路径>\\models`，
`lib/envs.py` 会自动用 ASCII 路径，绕开中文路径 C++ 库坑）。

## 4. 验证 + 开跑

```powershell
python scripts/selftest.py              # 预期全绿（含各 venv import 冒烟）
python scripts/new_project.py <项目名> <源视频>
python scripts/pipeline.py projects/<项目名>
python scripts/qc.py projects/<项目名>
```

## 已知坑（都踩过，别再踩）

1. **WorkBuddy/TRAE 沙箱里跑 pipeline 先清 PYTHONPATH**：
   `PYTHONPATH= python scripts/pipeline.py projects/<项目名>`，
   否则 s02 临时目录清理会被沙箱垫片 fail-closed 崩掉（详见 envs/README.md）
2. **项目路径不要含中文**：sentencepiece 等 C++ 库窄字符 fopen 直接报错
3. **GPU 显存 <8GB 时**：project.json 的 device 改 cuda→cpu（或跑之前退掉
   ComfyUI 等占显存的程序）
4. **发布纪律**：改代码后 `python scripts/publish.py src` 重新出包，
   别手工往发布位拷文件（漂移实锤过）
"""


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "src"
    out = "E:\\"
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    if target == "src":
        build_src(out)
    else:
        print(f"未知目标: {target}（当前仅支持 src；PUB/dev 待 E 盘扩容）")
        raise SystemExit(2)
