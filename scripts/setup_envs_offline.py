"""从包内 wheels/ 离线重建四个 Python 环境（整合包专用，全程不联网）。

为什么不用「直接打包 venv」：Windows 的 venv 记着创建时的绝对路径
（`pyvenv.cfg` 的 home / executable），换台机器或换个目录就失效。
所以整合包只带 wheel 仓库，安装时在这台机器上现建。

用法（一般由 `安装_离线.bat` 调用）:
    py -3.12 scripts\\setup_envs_offline.py
    py -3.12 scripts\\setup_envs_offline.py --check     # 只体检
    py -3.12 scripts\\setup_envs_offline.py --base sep  # 只装指定环境

前置：同目录上一级的 wheels/ 里有 req_env_*.txt 与全部 .whl
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WHEELS = ROOT / "wheels"
ENVS = ROOT / "envs"

# env 名 -> (venv 目录, 需要的主版本)
PLAN = {
    "base": ("env_base", "3.12"),
    "sep": ("env_sep", "3.12"),
    "asr": ("env_asr", "3.12"),
    "tts": ("env_tts", "3.11"),
}


def find_py(ver: str) -> Path | None:
    try:
        r = subprocess.run(["py", f"-{ver}", "-c", "import sys;print(sys.executable)"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            p = Path(r.stdout.strip())
            if p.exists():
                return p
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    w = shutil.which(f"python{ver}")
    return Path(w) if w else None


def env_python(name: str) -> Path:
    return ENVS / PLAN[name][0] / "Scripts" / "python.exe"


def ready(name: str) -> bool:
    """venv 能启动且关键包在位。"""
    py = env_python(name)
    if not py.exists():
        return False
    probe = "import torch" if name in ("sep", "asr", "tts") else "import openai"
    r = subprocess.run([str(py), "-c", probe], capture_output=True, text=True)
    return r.returncode == 0


def install(name: str) -> bool:
    req = WHEELS / f"req_env_{name}.txt"
    if not req.exists():
        print(f"  ! 缺 {req.name}，跳过 {name}")
        return False
    if ready(name):
        print(f"  {name}: 已就绪，跳过")
        return True

    ver = PLAN[name][1]
    base_py = find_py(ver)
    if not base_py:
        print(f"  X 需要 Python {ver} 来建 env_{name}，本机没找到")
        print(f"    装一个官方版（勾 py launcher）后重跑本脚本")
        return False

    target = ENVS / PLAN[name][0]
    if target.exists():
        # 半成品（比如上次中断）：挪开而不是删，避免误删用户数据
        broken = ENVS / f"_broken_{PLAN[name][0]}"
        if broken.exists():
            shutil.rmtree(broken, ignore_errors=True)
        target.rename(broken)
        print(f"  隔离半成品: {target.name} -> {broken.name}")

    print(f"  [{name}] 建 venv（Python {ver}）")
    r = subprocess.run([str(base_py), "-m", "venv", str(target)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  X venv 创建失败: {(r.stderr or '').strip()[-300:]}")
        return False

    py = env_python(name)
    print(f"  [{name}] 从 wheels 装依赖（不联网）")
    cmd = [str(py), "-m", "pip", "install", "--no-index",
           "--find-links", str(WHEELS), "-r", str(req), "--disable-pip-version-check"]
    r = subprocess.run(cmd, text=True)
    if r.returncode != 0:
        print(f"  X 依赖安装失败（上面最后几行是原因）")
        return False
    return ready(name)


def main() -> int:
    ap = argparse.ArgumentParser(description="从包内 wheels 离线重建环境")
    ap.add_argument("envs", nargs="*", default=[], help="base sep asr tts（默认全装）")
    ap.add_argument("--check", action="store_true", help="只体检不安装")
    args = ap.parse_args()

    if not WHEELS.exists():
        print(f"X 找不到 {WHEELS}，这个包不完整（缺离线 wheel 仓库）")
        return 2

    names = args.envs or list(PLAN)
    bad = [n for n in names if n not in PLAN]
    if bad:
        print(f"X 未知环境: {bad}（可选 {list(PLAN)}）")
        return 2

    if args.check:
        print("环境体检：")
        for n in names:
            print(f"  {n:5s} {'OK' if ready(n) else '未就绪'}")
        return 0

    ok = 0
    for n in names:
        print(f"\n=== {n} ===")
        if install(n):
            ok += 1

    print(f"\n完成 {ok}/{len(names)} 个环境")
    if ok == len(names):
        print("\n下一步：双击 2_启动.bat")
        print("建议再跑一次自检：py -3.12 scripts\\selftest.py（预期 67 通过 / 0 失败）")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
