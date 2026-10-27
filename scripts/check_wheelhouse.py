"""校验离线 wheel 仓库能否完整解析四个环境（出整合包前的必检项）。

为什么不用「文件名比对」：仓库里同时存在 cp311 和 cp312 两个 torch，
按名字+版本比对会把 py3.11 的 tts 误判成"已满足"，直到在目标机器上
安装时才炸。这里改成让 pip 自己解析：`--dry-run --ignore-installed
--no-index --find-links`，退出码 0 才算真的装得上。

用法:
    python scripts/check_wheelhouse.py                    # 检查 work/wheelhouse
    python scripts/check_wheelhouse.py --wheels <目录>
    python scripts/check_wheelhouse.py --envs base tts
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import envs as envs_mod  # noqa: E402

ENVS = ["base", "sep", "asr", "tts"]


def check(name: str, wheels: Path) -> tuple[bool, str]:
    req = wheels / f"req_env_{name}.txt"
    if not req.exists():
        return False, f"缺 {req.name}"
    py = envs_mod.env_python(f"env_{name}")
    if not py.exists():
        return False, f"venv 不存在（{py}）"
    cmd = [str(py), "-m", "pip", "install", "--dry-run", "--ignore-installed",
           "--no-index", "--find-links", str(wheels), "-r", str(req),
           "--disable-pip-version-check"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode == 0:
        return True, "可离线解析"
    errs = [l for l in (r.stderr or "").splitlines()
            if "ERROR" in l or "Could not find" in l]
    return False, (errs[0] if errs else f"退出码 {r.returncode}")[:120]


def main() -> int:
    ap = argparse.ArgumentParser(description="校验离线 wheel 仓库完整性")
    ap.add_argument("--wheels", default=str(ROOT / "work" / "wheelhouse"))
    ap.add_argument("--envs", nargs="*", default=[], help="base sep asr tts（默认全部）")
    args = ap.parse_args()

    wheels = Path(args.wheels)
    if not wheels.is_dir():
        print(f"X 目录不存在: {wheels}")
        return 2

    files = list(wheels.glob("*.whl")) + list(wheels.glob("*.tar.gz"))
    size = sum(f.stat().st_size for f in files) / (1 << 30)
    print(f"仓库: {wheels}  {len(files)} 个文件  {size:.2f} GB\n")

    names = args.envs or ENVS
    ok = 0
    for n in names:
        good, detail = check(n, wheels)
        print(f"  {n:5s} {'OK  ' if good else 'FAIL'} {detail}")
        ok += good

    print(f"\n{ok}/{len(names)} 个环境可离线安装")
    return 0 if ok == len(names) else 1


if __name__ == "__main__":
    raise SystemExit(main())
