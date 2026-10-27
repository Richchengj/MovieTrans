"""一键重建 MovieTrans 的四个 Python 环境（env_base / env_sep / env_asr / env_tts）。

搬家 / 换机后 venv 必然失效（pyvenv.cfg 硬编码旧机 Python 路径），本脚本
按 envs/README.md 的实测结论原地重建。只依赖标准库。

用法:
    python scripts/setup_envs.py            # 全部（base → sep → asr → tts）
    python scripts/setup_envs.py sep asr    # 只重建指定环境
    python scripts/setup_envs.py --check    # 只体检不重建

环境与关键依赖（详见 envs/README.md）:
    env_base  py3.12  openai + imageio-ffmpeg + huggingface_hub
    env_sep   py3.12  torch 2.6.0+cu124 + demucs + numpy
    env_asr   py3.12  whisperx + torch 2.8.0+cu126（顺序：先 whisperx 再回装 CUDA torch）
    env_tts   py3.11  tools/index-tts 内 uv sync（非常规 venv），torch 2.8.0+cu128 回装，
                      最后 junction 挂到 envs/env_tts

设计要点:
    - 失效 venv 一律改名 envs/_broken_<名字>_<日期>（可回滚，确认无用后手动删）
    - pip 走清华源；torch CUDA wheel 走 download.pytorch.org 官方源
    - 阶段之间互不干扰，可单独重跑；幂等（已是好环境则跳过）
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENVS = PROJECT_ROOT / "envs"
TUNA = "https://pypi.tuna.tsinghua.edu.cn/simple"
PYTORCH_CU124 = "https://download.pytorch.org/whl/cu124"
PYTORCH_CU126 = "https://download.pytorch.org/whl/cu126"
PYTORCH_CU128 = "https://download.pytorch.org/whl/cu128"

# 本机解释器（新机 2026-10-19；找不到时改这里）
PY312_CANDIDATES = [
    Path(r"C:\Users\Admin1\AppData\Local\Programs\Python\Python312\python.exe"),
    Path(r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"),
]
PY311_CANDIDATES = [
    Path(r"D:\Py311\python.exe"),
    Path(r"C:\Users\Admin1\AppData\Local\Programs\Python\Python311\python.exe"),
]
UV_CANDIDATES = [
    Path(r"D:\Py311\Scripts\uv.exe"),
    Path(r"C:\Users\Admin1\.local\bin\uv.exe"),
]


def find_py(candidates: list[Path], ver: str, env_var: str = "", launcher: str = "") -> Path:
    """按「显式候选 → 环境变量 → py 启动器 → PATH」的顺序找一个解释器。

    2026-10-27 起不再只认本机写死的路径：轻量发行版要在别人的机器上跑，
    只要装了官方 Python（带 py 启动器）或 uv 就能自动找到。
    仍可用环境变量强制指定，例如 `set MT_PY311=D:\\Python311\\python.exe`。
    """
    for p in candidates:
        if p.exists():
            return p
    if env_var and os.environ.get(env_var):
        p = Path(os.environ[env_var])
        if p.exists():
            return p
    if launcher:
        try:
            r = subprocess.run(
                ["py", launcher, "-c", "import sys;print(sys.executable)"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0 and r.stdout.strip():
                p = Path(r.stdout.strip())
                if p.exists():
                    return p
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    if launcher == "-3.12" or launcher == "-3.11":
        p = shutil.which(f"python{launcher[1:]}")
        if p:
            return Path(p)
    hint = f"，或设环境变量 {env_var}" if env_var else ""
    raise SystemExit(
        f"找不到 Python {ver}{hint}。\n"
        f"  官方安装包：https://www.python.org/downloads/windows/（勾选 py launcher）\n"
        f"  国内镜像：https://mirrors.huaweicloud.com/python/"
    )


PY312 = find_py(PY312_CANDIDATES, "3.12", "MT_PY312", "-3.12")
PY311 = find_py(PY311_CANDIDATES, "3.11", "MT_PY311", "-3.11")


def find_uv() -> Path:
    """uv 只有 env_tts 需要，惰性查找——没装 uv 不该挡住 base/sep/asr。"""
    p = find_py(UV_CANDIDATES, "uv", "MT_UV", "")
    return p


def venv_python(name: str) -> Path:
    return ENVS / f"env_{name}" / "Scripts" / "python.exe"


def venv_path_ok(p: Path) -> bool:
    """venv 存在、能启动、pyvenv.cfg 的 home 还活着 → 视为可用。"""
    if not (p / "Scripts" / "python.exe").exists():
        return False
    cfg = p / "pyvenv.cfg"
    if cfg.exists():
        for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("home = "):
                return Path(line[7:].strip()).exists()
    return True


def venv_ok(name: str) -> bool:
    return venv_path_ok(ENVS / f"env_{name}")


def quarantine(dir_name: str) -> None:
    """失效 venv 改名保留（同盘 rename 瞬时完成），不直接删。dir_name 传字面名如 env_base。"""
    src = ENVS / dir_name
    if not src.exists():
        return
    if not any(src.iterdir()):  # 空目录（如搬家中丢失的 env_tts）直接删
        src.rmdir()
        print(f"[clean] {src}（空目录，已移除）")
        return
    dst = ENVS / f"_broken_{dir_name}_{date.today():%Y%m%d}"
    if dst.exists():
        shutil.rmtree(dst)
    src.rename(dst)
    print(f"[quarantine] {src} -> {dst}（确认无用后可手动删）")


def run(cmd: list[str], **kw) -> None:
    print(f"[run] {' '.join(str(c) for c in cmd)}", flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def pip_install(name: str, *pkgs: str, index: str = TUNA) -> None:
    run([venv_python(name), "-m", "pip", "install", "--upgrade", "pip", "-i", index])
    run([venv_python(name), "-m", "pip", "install", *pkgs, "-i", index])


def make_venv(name: str, base_python: Path) -> None:
    if venv_ok(name):
        print(f"[skip] env_{name} 已可用")
        return
    quarantine(f"env_{name}")
    run([base_python, "-m", "venv", ENVS / f"env_{name}"])
    pip_install(name, "pip")


def build_base() -> None:
    make_venv("base", PY312)
    pip_install("base", "openai", "imageio-ffmpeg", "huggingface_hub")


def build_sep() -> None:
    make_venv("sep", PY312)
    pip_install("sep", "torch==2.6.0+cu124", "torchaudio==2.6.0+cu124", index=PYTORCH_CU124)
    pip_install("sep", "demucs", "numpy")
    run([venv_python("sep"), "-c", "import torch, demucs; print('sep ok, cuda =', torch.cuda.is_available())"])


def build_asr() -> None:
    make_venv("asr", PY312)
    # 顺序坑：whisperx 会把 torch 换成 CPU 版，必须先装它再回装 CUDA 构建
    pip_install("asr", "whisperx")
    pip_install("asr", "torch==2.8.0+cu126", "torchaudio==2.8.0+cu126",
                "torchvision==0.23.0+cu126", index=PYTORCH_CU126)
    run([venv_python("asr"), "-c",
         "import torch, whisperx, pyannote.audio, ctranslate2; "
         "print('asr ok, cuda =', torch.cuda.is_available())"])


def build_tts() -> None:
    """IndexTTS 非常规 venv：真实环境在 tools/index-tts/.venv，junction 挂到 envs/env_tts。"""
    repo = PROJECT_ROOT / "tools" / "index-tts"
    dot_venv = repo / ".venv"
    py = dot_venv / "Scripts" / "python.exe"

    # 以 torch+CUDA 探测为准（venv 骨架在 ≠ 包齐，uv 中途被杀时就是这种状态）
    def torch_ready() -> bool:
        if not py.exists():
            return False
        probe = subprocess.run(
            [str(py), "-c", "import torch; print(int(torch.cuda.is_available()))"],
            capture_output=True, text=True, cwd=repo)
        return probe.stdout.strip() == "1"

    if not torch_ready():
        if py.exists() and not venv_path_ok(dot_venv):
            dst = repo / f".venv_broken_{date.today():%Y%m%d}"
            if dst.exists():
                shutil.rmtree(dst)
            dot_venv.rename(dst)
            print(f"[quarantine] {dot_venv} -> {dst}")
        # uv sync 幂等：包齐了秒过，缺了续装
        run([find_uv(), "sync", "--extra", "webui",
             "--default-index", "https://mirrors.aliyun.com/pypi/simple",
             "--python", PY311], cwd=repo)
        # uv 装的 torch 2.7+ PyPI Windows wheel 自带 CUDA 12.6，先探测；
        # 只有真是 CPU 版才回装 CUDA 构建（省 2.7GB 重复下载）
        if not torch_ready():
            run([py, "-m", "pip", "install", "torch==2.8.0+cu126",
                 "torchaudio==2.8.0+cu126", "--index-url", PYTORCH_CU126,
                 "--force-reinstall", "--no-deps"])
    run([py, "-c",
         "import torch; print('tts ok, cuda =', torch.cuda.is_available())"])

    # junction：envs/env_tts -> tools/index-tts/.venv
    link = ENVS / "env_tts"
    if link.exists() and not link.is_symlink() and any(link.iterdir()):
        # 真目录且非空：视为失效残留，隔离
        quarantine("env_tts")
    elif link.exists() and not any(link.iterdir()):
        link.rmdir()
    if not link.exists():
        run(["cmd", "/c", "mklink", "/J", str(link), str(dot_venv)])
    print("[done] env_tts（junction -> tools/index-tts/.venv）")


BUILDERS = {"base": build_base, "sep": build_sep, "asr": build_asr, "tts": build_tts}


def main() -> int:
    ap = argparse.ArgumentParser(description="一键重建 MovieTrans Python 环境")
    ap.add_argument("names", nargs="*", choices=[*BUILDERS, "all"], default=None)
    ap.add_argument("--check", action="store_true", help="只体检不重建")
    args = ap.parse_args()
    targets = list(BUILDERS) if not args.names or "all" in args.names else args.names

    if args.check:
        bad = [n for n in targets if not venv_ok(n)]
        print("\n".join(f"  {'ok  ' if n not in bad else 'FAIL'} env_{n}" for n in targets))
        return 1 if bad else 0

    for n in targets:
        print(f"\n===== env_{n} =====", flush=True)
        BUILDERS[n]()
    print("\n[all done] 建议跑 python scripts/selftest.py 验收")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
