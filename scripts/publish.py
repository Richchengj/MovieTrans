r"""发行包生成器：lite（轻量源码包）/ full（离线整合包）。

用法:
    python scripts/publish.py lite                 # -> E:\MovieTrans-lite（约 250 MB）
    python scripts/publish.py lite --out F:\dist   # 自定义输出根
    python scripts/publish.py full                 # -> E:\MovieTrans-full（约 24 GB，需先备 wheels）
    python scripts/publish.py full --no-models     # 出包但不复制 14GB 权重（自测用）

两种形态（2026-10-27 起）:
    lite  源码 + 工具二进制 + 文档。权重靠 `fetch_models.py`（魔搭优先，实测 3.7 MB/s），
          环境靠 `setup_envs.py`（清华 pip 源）。给有网络、愿意等下载的人。
    full  上述全部 + 预置权重 + 离线 wheel 仓库 + 便携 Python。解压后双击
          `安装_离线.bat`，全程不联网。给内网/无稳定网络的人。

**打包上的两个坑（都在这里处理，别再踩）**:
    1. `envs/env_tts` 是 junction，实体在 `tools/index-tts/.venv`。按普通目录递归复制
       会得到空壳或重复 7 GB —— 这里用 `_copy_tree` 对链接只记录不跟随，并单独校验。
    2. venv 不可跨机搬迁（`pyvenv.cfg` 的 home 是绝对路径）。所以 full 不塞 venv，
       改为「包内 Python + 离线 wheel」，首次安装时现建。

SRC（旧名）等同于 lite，保留兼容。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXCLUDE_DIRS = {
    ".workbuddy", "work", "__pycache__", ".git", "envs", "models", "projects",
    ".venv", ".venv_broken_20261019", ".venv_broken_20261018",
    "_broken_env_asr_20261019", "_broken_env_sep_20261019",
}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".tmp", ".part"}

KEEP_TOOLS = ("ffmpeg", "deepfilternet", "index-tts")
DOCS = [
    "README.md", "README_BILINGUAL.md", "SPEC.md", "ROADMAP.md", "BENCHMARK.md",
    "CHANGELOG.md", "LICENSE", "VERSION",
    "需要人工操作.md", "TTS备选评估.md", "翻译引擎对比.md",
]
ROOT_FILES = ("LICENSE", "VERSION", "CHANGELOG.md", ".gitignore")


def _ignore(dir: str, names: list[str]) -> set[str]:
    skip = set()
    d = Path(dir)
    for n in names:
        p = d / n
        if p.is_dir() and (n in EXCLUDE_DIRS):
            skip.add(n)
        elif p.is_file() and p.suffix in EXCLUDE_SUFFIX:
            skip.add(n)
    return skip


def _copy_tree(src: Path, dst: Path) -> None:
    """复制目录，遇到 junction / symlink 只记录不跟随。

    Windows 上 `shutil.copytree` 默认会跟随 junction，把 7 GB 的实体复制一份；
    而 publish 的排除表又把 `.venv` 排除掉，结果两头都不对。这里显式区分。
    """
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in EXCLUDE_DIRS:
            continue
        target = dst / item.name
        if item.is_symlink() or _is_junction(item):
            # 不跟随：留一个标记文件，解包后由安装脚本重建
            (dst / f"{item.name}.LINK").write_text(
                f"# 此目录原本是链接，实体位于：{_link_target(item)}\n"
                f"# 打包时未复制；安装脚本会重建。\n",
                encoding="utf-8",
            )
            continue
        if item.is_dir():
            _copy_tree(item, target)
        elif item.is_file() and item.suffix not in EXCLUDE_SUFFIX:
            shutil.copy2(item, target)


def _is_junction(p: Path) -> bool:
    try:
        return os.path.islink(p) or (hasattr(os, "readlink") and _real(p) != _abs(p))
    except OSError:
        return False


def _abs(p: Path) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def _real(p: Path) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def _link_target(p: Path) -> str:
    try:
        return os.path.realpath(str(p))
    except OSError:
        return "<未知>"


def _dir_size(p: Path) -> int:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def _mb(n: int) -> str:
    return f"{n / (1 << 20):,.0f} MB" if n < (1 << 30) else f"{n / (1 << 30):.1f} GB"


# ---------------------------------------------------------------- lite

def build_lite(out_root: str, name: str = "MovieTrans-lite") -> Path:
    dst = Path(out_root) / name
    if dst.exists():
        # 改名归档而不是删：一来不会误删上次的产物，二来运行时对
        # 批量删除（>50 文件）有确认拦截，rmtree 会被直接拒掉（2026-10-27 实测）
        bak = dst.with_name(
            f"{dst.name}_bak_{__import__('datetime').datetime.now():%Y%m%d_%H%M%S}")
        print(f"旧目标改名归档: {dst.name} -> {bak.name}")
        dst.rename(bak)
    dst.mkdir(parents=True)

    print("[1/5] scripts/")
    _copy_tree(ROOT / "scripts", dst / "scripts")

    print("[2/5] tools/（ffmpeg / deepfilternet / index-tts 源码，不含 venv）")
    (dst / "tools").mkdir(exist_ok=True)
    for n in KEEP_TOOLS:
        s = ROOT / "tools" / n
        if s.exists():
            _copy_tree(s, dst / "tools" / n)

    print("[3/5] envs/README.md（搭建说明，不带 venv 本体）")
    (dst / "envs").mkdir(exist_ok=True)
    env_readme = ROOT / "envs" / "README.md"
    if env_readme.exists():
        shutil.copy2(env_readme, dst / "envs" / "README.md")

    print("[4/5] 文档")
    for doc in DOCS:
        s = ROOT / doc
        if s.exists():
            shutil.copy2(s, dst / doc)
    (dst / "projects").mkdir(exist_ok=True)
    (dst / "projects" / "README.md").write_text(
        "# projects/\n\n"
        "每个视频一个子目录，由 `python scripts/new_project.py <项目名> <源视频>` 创建。\n"
        "项目数据（含译文、配音、成片）默认不进版本库。\n",
        encoding="utf-8",
    )

    print("[5/5] 启动脚本 + 安装说明")
    _write_lite_launchers(dst)
    (dst / "安装说明.md").write_text(_install_md(offline=False), encoding="utf-8")

    size = _dir_size(dst)
    print(f"\nOK -> {dst}  ({_mb(size)})")
    return dst


def _write_lite_launchers(dst: Path) -> None:
    (dst / "1_安装.bat").write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "cd /d \"%~dp0\"\r\n"
        "title MovieTrans 安装\r\n"
        "echo ============================================\r\n"
        "echo  MovieTrans 0.9.0  安装\r\n"
        "echo  需要：Python 3.12 + 3.11（官方版，带 py 启动器）\r\n"
        "echo        网络（权重走魔搭镜像，依赖走清华源）\r\n"
        "echo ============================================\r\n"
        "echo.\r\n"
        "py -3.12 -c \"import sys\" >nul 2>&1 || (echo [X] 没找到 Python 3.12，请先安装：https://www.python.org/downloads/windows/ & pause & exit /b 1)\r\n"
        "py -3.11 -c \"import sys\" >nul 2>&1 || (echo [X] 没找到 Python 3.11（TTS 环境需要），请先安装 & pause & exit /b 1)\r\n"
        "echo [1/2] 建四个 Python 环境（约 15-30 分钟）\r\n"
        "py -3.12 scripts\\setup_envs.py\r\n"
        "if errorlevel 1 (echo [X] 环境安装失败，见 envs\\README.md & pause & exit /b 1)\r\n"
        "echo.\r\n"
        "echo [2/2] 下载模型权重（约 14 GB，可断点续跑）\r\n"
        "py -3.12 scripts\\fetch_models.py\r\n"
        "if errorlevel 1 (echo [X] 权重下载失败，重跑本脚本即可续传 & pause & exit /b 1)\r\n"
        "echo.\r\n"
        "echo [OK] 安装完成，双击 2_启动.bat 开始使用\r\n"
        "pause\r\n",
        encoding="utf-8",
    )
    (dst / "2_启动.bat").write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "cd /d \"%~dp0\"\r\n"
        "title MovieTrans\r\n"
        "if not exist \"envs\\env_base\\Scripts\\python.exe\" (\r\n"
        "  echo [X] 还没安装，请先双击 1_安装.bat\r\n"
        "  pause & exit /b 1\r\n"
        ")\r\n"
        "echo 浏览器会自动打开 http://127.0.0.1:8765 ，关闭本窗口即停止服务\r\n"
        "\"envs\\env_base\\Scripts\\python.exe\" scripts\\webui.py\r\n"
        "pause\r\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------- full

def build_full(out_root: str, name: str = "MovieTrans-full", with_models: bool = True) -> Path:
    dst = Path(out_root) / name
    if dst.exists():
        # 改名归档而不是删：一来不会误删上次的产物，二来运行时对
        # 批量删除（>50 文件）有确认拦截，rmtree 会被直接拒掉（2026-10-27 实测）
        bak = dst.with_name(
            f"{dst.name}_bak_{__import__('datetime').datetime.now():%Y%m%d_%H%M%S}")
        print(f"旧目标改名归档: {dst.name} -> {bak.name}")
        dst.rename(bak)

    print("=== 1. 先做 lite 骨架 ===")
    build_lite(out_root, name=name)
    dst = Path(out_root) / name

    print("\n=== 2. 校验 env_tts 链接没有被误复制 ===")
    env_tts = dst / "envs" / "env_tts"
    if env_tts.exists() and not any(env_tts.iterdir()):
        raise SystemExit("打包错误：env_tts 被打成空目录（junction 被跟随），中止")

    print("\n=== 3. 复制权重 ===")
    if with_models:
        src_models = ROOT / "models"
        if not src_models.exists():
            raise SystemExit("models/ 不存在，无法出整合包")
        need = _dir_size(src_models)
        free = shutil.disk_usage(Path(out_root)).free
        if free < need * 1.1:
            raise SystemExit(f"目标盘空间不足：需 {_mb(need)}，可用 {_mb(free)}")
        print(f"  models/ -> {dst / 'models'}  ({_mb(need)})")
        shutil.copytree(src_models, dst / "models")
    else:
        print("  跳过（--no-models）")

    print("\n=== 4. 检查离线 wheel 仓库 ===")
    wheels = ROOT / "work" / "wheelhouse"
    if wheels.exists():
        print(f"  {wheels} -> {dst / 'wheels'}")
        shutil.copytree(wheels, dst / "wheels")
    else:
        print("  ! 没有 work/wheelhouse，整合包将无法离线安装依赖")
        print("    先运行：python scripts/make_wheelhouse.py")

    print("\n=== 5. 离线安装入口 ===")
    offline_bat = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "cd /d \"%~dp0\"\r\n"
        "title MovieTrans 离线安装\r\n"
        "echo 从包内 wheels 目录安装，全程不联网\r\n"
        "if not exist \"wheels\" (echo [X] 缺 wheels 目录，这个包不完整 & pause & exit /b 1)\r\n"
        "py -3.12 \"scripts\\setup_envs_offline.py\"\r\n"
        "pause\r\n"
    )
    (dst / "安装_离线.bat").write_text(offline_bat, encoding="utf-8")
    # 必须盖掉 lite 骨架留下的联网安装器：整合包里双击它会走 pip 官方源 +
    # 魔搭下载，在内网机器上白等一小时然后失败，而 `2_启动.bat` 的提示恰好
    # 也叫用户去点这个文件。两个入口做成同一份内容，点错也走离线。
    (dst / "1_安装.bat").write_text(offline_bat, encoding="utf-8")
    (dst / "安装说明.md").write_text(_install_md(offline=True), encoding="utf-8")

    size = _dir_size(dst)
    print(f"\nOK -> {dst}  ({_mb(size)})")
    _write_manifest(dst)
    return dst


def _write_manifest(dst: Path) -> None:
    """给发布包留一份清单，便于校验完整性。"""
    manifest = {
        "name": dst.name,
        "version": (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        if (ROOT / "VERSION").exists() else "",
        "built_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "has_models": (dst / "models").exists(),
        "has_wheels": (dst / "wheels").exists(),
        "size_bytes": _dir_size(dst),
    }
    (dst / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"manifest.json -> {dst / 'manifest.json'}")


# ---------------------------------------------------------------- 文档

def _install_md(offline: bool) -> str:
    head = """# MovieTrans 0.9.0 · 安装说明

英文电影 → 中文配音的本地流水线。十阶段全自动，克隆原演员音色。

- 显卡：NVIDIA，显存 ≥ 8 GB 更顺畅（无卡可跑 CPU，TTS 慢约 10 倍）
- 系统：Windows 10/11
- 磁盘：权重约 14 GB，环境约 19 GB，加工作区建议留 60 GB
"""
    if offline:
        body = """
## 磁盘预估

| 阶段 | 占用 |
|---|---|
| 解压后（源码 + 权重 + wheels） | 约 25 GB |
| 装完四个环境（envs/） | 约 44 GB |
| 之后 `wheels/` 可删 | 释放约 10 GB |

建议预留 **80 GB**（含一个项目的中间产物）。

## 安装（离线，全程不联网）

**唯一的前置**：这台机器要有 Python 3.12 和 3.11
（[官方安装包](https://www.python.org/downloads/windows/)，装时勾上 py launcher；
国内可用[华为云镜像](https://mirrors.huaweicloud.com/python/)）。
其余依赖全部在包内 `wheels/`，不需要联网。

1. 解压到**英文路径**，例如 `D:\\MovieTrans`（路径含中文会让部分 C++ 库报错）
2. 双击 `安装_离线.bat`（或 `1_安装.bat`，两者内容相同，都是离线安装）

脚本会：用包内 `wheels/` 里的 wheel 建四个 Python 环境（约 10 分钟）。
权重已在 `models/` 里，不需要下载。

3. 双击 `2_启动.bat`

## 检查安装是否完整

```
py -3.12 scripts\\selftest.py
```

预期 `67 通过 / 0 失败`。若出现「导入超时（未判定）」提示，重跑一次即可
（冷盘首次加载 torch 会慢，可用 `set MT_SELFTEST_IMPORT_TIMEOUT=1200` 放宽）。
"""
    else:
        body = """
## 安装（联网）

需要 **Python 3.12 和 3.11**（[官方安装包](https://www.python.org/downloads/windows/)，
装的时候勾上 py launcher）。国内下载慢可用
[华为云镜像](https://mirrors.huaweicloud.com/python/)。

1. 解压到**英文路径**，例如 `D:\\MovieTrans`（路径含中文会让部分 C++ 库报错）
2. 双击 `1_安装.bat`

脚本会做两件事：
- 建四个 Python 环境（pip 走清华源，torch 走 PyTorch 官方源）
- 下载模型权重（14 GB，走魔搭镜像约 3.7 MB/s，可断点续跑）

网络中断不要紧，重跑 `1_安装.bat` 会续传。

3. 双击 `2_启动.bat`，浏览器自动打开 `http://127.0.0.1:8765`

## 检查安装是否完整

```
py -3.12 scripts\\selftest.py
```

预期 `67 通过 / 0 失败`。
"""
    tail = """
## 第一次跑

1. Web 界面点「新建项目」，选一个英文视频
2. 配置页填翻译用的 API Key（硅基流动 / 任意 OpenAI 兼容接口），
   不填则退回本地翻译模型（质量明显更差）
3. 点「运行」，等流水线跑完
4. 到「复核」页听原声、改译文、试听配音
5. 到「交付物」页拿成片 mp4 与 srt

## 已知限制（不是故障）

- **音色不是每段都像**。同人跨语言余弦正常 0.22–0.35，阈值 0.18。
  某角色台词量特别大时可能整体偏低，qc 会点名是哪个说话人
- **长片上限**：已验证最长 44.5 分钟；90 分钟以上未验证
- **GPT-SoVITS 零样本不可交付**（音色实测 0.07），只作实验用
- **云端 TTS 排队不可控**：长片后期可能很慢，见到告警再决定是否切本地

## 出问题了看这里

- `python scripts/qc.py projects/<项目>` 看 13 项质检明细
- `需要人工操作.md` 有人工复核流程与常见故障
- `python scripts/selftest.py` 先确认环境本身是好的
"""
    return head + body + tail


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="生成 MovieTrans 发行包")
    ap.add_argument("target", nargs="?", default="lite",
                    choices=["lite", "full", "src"], help="lite / full（src 同 lite）")
    ap.add_argument("--out", default="E:\\", help="输出根目录（默认 E:\\）")
    ap.add_argument("--no-models", action="store_true", help="full 包不复制权重（自测用）")
    args = ap.parse_args()

    if args.target in ("lite", "src"):
        build_lite(args.out)
    else:
        build_full(args.out, with_models=not args.no_models)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
