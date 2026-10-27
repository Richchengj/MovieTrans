"""按阶段切换到对应的模型环境。

SPEC 约定每个模型一个独立环境（env_sep / env_asr / env_tts），
但 pipeline.py 用同一个解释器调度所有阶段。这里用「自我重新执行」切环境：
当前解释器缺依赖时，改用目标环境的 python 重跑本脚本。
这样无论从哪个环境启动，每个阶段都能拿到它需要的依赖。

另外，本项目根目录含中文（`d:\\电影翻译`），而部分 C++ 依赖
（sentencepiece、kaldifst/wetext）用窄字符打开文件，遇到非 ASCII 路径会直接
报错。`envs` 目录因此额外通过 C 盘 junction 暴露成一个 ASCII 路径
（如 `C:\\dub_envs` → `d:\\电影翻译\\envs`），本模块优先使用该 ASCII 路径，
让 `sys.prefix` 与 site-packages 都是 ASCII，从而绕开这些库的限制。
可用环境变量 `DUB_ENVS_DIR` 覆盖该 ASCII 目录。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_REEXEC_FLAG = "DUB_REEXEC_ENV"

ENVS_DIR_ENV = "DUB_ENVS_DIR"
DEFAULT_ASCII_ENVS = Path("C:/dub_envs")


def ascii_envs_dir() -> Path | None:
    """返回一个可用的 ASCII envs 目录；没有则返回 None。"""
    candidates: list[Path] = []
    configured = os.environ.get(ENVS_DIR_ENV)
    if configured:
        candidates.append(Path(configured))
    candidates.append(DEFAULT_ASCII_ENVS)
    # 项目根为 ASCII 时（如 D:\MovieTrans），项目内 envs 本身就是合法 ASCII 路径，
    # 不再强依赖 C:\dub_envs junction（2026-10-18 项目迁入 MovieTrans 后补）
    candidates.append(PROJECT_ROOT / "envs")
    for candidate in candidates:
        if str(candidate).isascii() and candidate.exists():
            return candidate
    return None


def torch_home(project_root: Path | None = None) -> str | None:
    """返回 torch.hub 缓存目录（TORCH_HOME），无缓存则 None。

    与 envs 同理：torch 的 jit 序列化在 C++ 层用窄字符 fopen 打开文件，
    中文路径会直接失败，所以优先用 C 盘 junction（C:\\dub_torch）暴露的
    ASCII 路径；其次用项目内 models/torch（发布包本身路径是 ASCII）。
    """
    candidates: list[Path] = []
    configured = os.environ.get("DUB_TORCH_DIR")
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path("C:/dub_torch"))
    candidates.append(Path("C:/dub_models/torch"))
    if project_root is not None:
        candidates.append(Path(project_root) / "models" / "torch")
    for candidate in candidates:
        if str(candidate).isascii() and (candidate / "hub").is_dir():
            return str(candidate)
    return None


def _python_in(base: Path, env_name: str) -> Path:
    return base / env_name / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def env_python(env_name: str) -> Path:
    ascii_dir = ascii_envs_dir()
    if ascii_dir is not None:
        return _python_in(ascii_dir, env_name)
    return _python_in(PROJECT_ROOT / "envs", env_name)


def has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _reexec(python: Path, env_name: str) -> None:
    # 必须立即刷出：子进程会直接继承本进程的 stdout，
    # 若这里不刷新，提示会排到子进程输出之后，看起来像是最后才切的环境。
    print(f"[env] 改用 {env_name}（{python}）重新执行", flush=True)
    env = dict(os.environ, **{_REEXEC_FLAG: env_name})
    script = Path(sys.argv[0]).resolve()
    raise SystemExit(subprocess.call([str(python), str(script), *sys.argv[1:]], env=env))


def ensure_env(env_name: str, required_module: str) -> None:
    """确保当前解释器能 import required_module，否则切到 env_name 重跑本脚本。

    已经在目标环境、但当前解释器路径含非 ASCII 时，也会切到 ASCII 路径重跑，
    以兼容打不开非 ASCII 路径的 C++ 依赖。
    """
    target = env_python(env_name)

    if has_module(required_module):
        if str(Path(sys.executable)).isascii() or not target.exists():
            return
        if os.environ.get(_REEXEC_FLAG) == env_name:
            return
        _reexec(target, env_name)
        return

    if os.environ.get(_REEXEC_FLAG) == env_name:
        raise SystemExit(
            f"已在 {env_name} 中运行，但仍无法 import {required_module}；请检查该环境。"
        )

    if not target.exists():
        raise SystemExit(f"环境 {env_name} 不存在（找不到 {target}）")

    _reexec(target, env_name)
