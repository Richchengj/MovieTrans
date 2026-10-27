"""生成离线 wheel 仓库（给 full 整合包用）。

做法：把四个 venv 里**已装好的依赖**导成 requirements，再用 pip 把它们
（含全部依赖）下载成 .whl 放进同一个目录。整合包解压后 `pip install
--no-index --find-links wheels` 就能在**完全不联网**的情况下重建同样的环境。

用法:
    python scripts/make_wheelhouse.py            # 四个环境全下
    python scripts/make_wheelhouse.py base asr   # 只下指定环境
    python scripts/make_wheelhouse.py --list     # 看会下什么，不下载

产物（默认 work/wheelhouse/，已 gitignore）:
    req_env_base.txt / req_env_sep.txt / req_env_asr.txt / req_env_tts.txt
    *.whl                     全部依赖轮子（四个环境共用一份，自动去重）

注意:
    - torch 的 CUDA 变体（+cu124/+cu126/+cu128）只在 PyTorch 官方源有，
      所以每个环境要带自己的 --index-url，否则会下成 CPU 版
    - 体积预计 10–15 GB（三份 torch 各自带全套 CUDA 运行库）
    - 想瘦身就统一 torch 版本，但那要重验 s02/s04/s07，属独立课题
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WHEELS = ROOT / "work" / "wheelhouse"

TUNA = "https://pypi.tuna.tsinghua.edu.cn/simple"

# env 名 -> (目录名, torch 源)
ENVS = {
    "base": ("env_base", None),
    "sep": ("env_sep", "https://download.pytorch.org/whl/cu124"),
    "asr": ("env_asr", "https://download.pytorch.org/whl/cu126"),
    "tts": ("env_tts", "https://download.pytorch.org/whl/cu128"),
}

# 这些不该进 wheel 仓库：本机路径的 editable 安装、包管理器自身
SKIP_PREFIX = ("-e ", "file://", "#")
SKIP_NAMES = {"pip", "setuptools", "wheel", "uv"}


def env_python(name: str) -> Path:
    return ROOT / "envs" / ENVS[name][0] / "Scripts" / "python.exe"


def find_uv() -> Path | None:
    for p in (Path(r"D:\Py311\Scripts\uv.exe"),
              Path(r"C:\Users\Admin1\.local\bin\uv.exe")):
        if p.exists():
            return p
    w = shutil.which("uv")
    return Path(w) if w else None


def freeze(name: str) -> list[str]:
    py = env_python(name)
    if not py.exists():
        raise SystemExit(f"{name}: venv 不存在（{py}）")

    # env_tts 是 uv 建的，默认不带 pip —— 依次尝试 pip / uv / ensurepip
    attempts: list[tuple[str, list[str]]] = [
        ("pip", [str(py), "-m", "pip", "freeze", "--all"]),
    ]
    uv = find_uv()
    if uv:
        attempts.append(("uv", [str(uv), "pip", "freeze", "--python", str(py)]))
    attempts.append(("ensurepip", [str(py), "-m", "ensurepip", "--quiet"]))

    stdout = ""
    for kind, cmd in attempts:
        if kind == "ensurepip":
            subprocess.run(cmd, capture_output=True, text=True)
            r = subprocess.run([str(py), "-m", "pip", "freeze", "--all"],
                               capture_output=True, text=True)
        else:
            r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            stdout = r.stdout
            break
    if not stdout:
        raise SystemExit(f"{name}: 无法导出依赖清单（pip/uv 都不可用，ensurepip 也失败）")

    out = []
    for line in stdout.splitlines():
        s = _normalize(line.strip())
        if not s or s.startswith(SKIP_PREFIX):
            continue
        base = s.split("==")[0].split(" @ ")[0].strip().lower()
        if base in SKIP_NAMES:
            continue
        out.append(s)
    return out


def _normalize(line: str) -> str:
    """把 `pkg @ file:///.../pkg-1.2.3+cu126-cp311-win_amd64.whl` 还原成 `pkg==1.2.3+cu126`。

    env_tts 的 torch 当初是用本地 wheel 装的，那条 wheel 后来随 work/ 清理删掉了；
    若原样写进 requirements，别人机器上根本找不到。从文件名反推版本号即可，
    pip 会按 `+cuXXX` 后缀去对应的 PyTorch 源取同样版本。
    """
    if " @ file://" not in line:
        return line
    name, _, path = line.partition(" @ ")
    fname = path.replace("\\", "/").rsplit("/", 1)[-1]
    # pkgname-1.2.3+cu126-cp311-cp311-win_amd64.whl
    parts = fname.split("-")
    if len(parts) < 2:
        return ""
    version = parts[1].replace(".whl", "")
    return f"{name.strip()}=={version}"


def torch_index_for(lines: list[str], default: str | None) -> str | None:
    """按 requirements 里 torch 的实际版本后缀选 PyTorch 源（+cu124/+cu126/+cu128）。"""
    for line in lines:
        if line.lower().startswith("torch=="):
            m = re.search(r"\+cu(\d+)", line)
            if m:
                return f"https://download.pytorch.org/whl/cu{m.group(1)}"
    return default


def download(name: str, req_file: Path, dest: Path, lines: list[str]) -> None:
    py = env_python(name)
    cmd = [str(py), "-m", "pip", "download", "-r", str(req_file), "-d", str(dest),
           "--only-binary=:all:", "--index-url", TUNA]
    torch_idx = torch_index_for(lines, ENVS[name][1])
    if torch_idx:
        cmd += ["--extra-index-url", torch_idx]
        print(f"[{name}] torch 源: {torch_idx}")
    print(f"\n[{name}] pip download ...")
    r = subprocess.run(cmd, text=True)
    if r.returncode != 0:
        print(f"[{name}] 下载有失败项（常见：某个包只有 sdist）。")
        print("       可重跑本脚本续传，或对单个包用 --no-binary :all: 补。")


def main() -> int:
    ap = argparse.ArgumentParser(description="生成离线 wheel 仓库")
    ap.add_argument("envs", nargs="*", default=[], help="base sep asr tts（默认全下）")
    ap.add_argument("--list", action="store_true", help="只列依赖，不下载")
    ap.add_argument("--out", default=str(WHEELS))
    args = ap.parse_args()

    names = args.envs or list(ENVS)
    bad = [n for n in names if n not in ENVS]
    if bad:
        raise SystemExit(f"未知环境: {bad}（可选 {list(ENVS)}）")

    dest = Path(args.out)
    dest.mkdir(parents=True, exist_ok=True)

    total = 0
    for name in names:
        lines = freeze(name)
        total += len(lines)
        req = dest / f"req_env_{name}.txt"
        req.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[{name}] {len(lines)} 个依赖 -> {req.name}")
        if args.list:
            for line in lines:
                print("   ", line)
        else:
            download(name, req, dest, lines)

    size = sum(f.stat().st_size for f in dest.glob("*.whl")) / (1 << 30)
    print(f"\n仓库: {dest}")
    print(f"依赖条目 {total} 个，wheel 合计 {size:.2f} GB")
    if not args.list and len(names) == len(ENVS) and size < 1:
        print("! 体积异常偏小（全量应 10 GB 以上），检查上面是否有下载失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
