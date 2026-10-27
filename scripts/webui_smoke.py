#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WebUI 冒烟回归（2026-10-22 固化，替代一次性 Playwright 脚本）。

分两层，默认只跑第一层（零依赖，约 5 秒）：

  ① API/页面层（纯标准库）：起一个临时服务 → 校验页面关键元素、项目详情
     字段、配置校验（坏引擎 400）、错误目录 404、路径穿越拒绝
  ② 浏览器层（`--ui`，需 Node + playwright）：真实 Edge 走一遍「选项目 →
     配置页切三种 TTS 引擎 → 看设备行显隐 → 对照页有数据 → 交付页有质检
     面板」，并断言零 JS 错误

用法：
    python scripts/webui_smoke.py                     # ①，自动挑最新有产出的项目
    python scripts/webui_smoke.py --ui                # ①+②
    python scripts/webui_smoke.py --project 20261021_bcs_zero2 --port 8798
    python scripts/webui_smoke.py --keep              # 跑完不杀服务（调试用）

退出码：0 = 全过；1 = 有失败（逐项打印 PASS/FAIL）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
PROJECTS_DIR = ROOT / "projects"

# 页面里必须存在的关键元素（删了/改 id 会让前端静默失效，值得钉住）
REQUIRED_IDS = [
    ("项目下拉", 'id="project_select"'),
    ("翻译徽标", 'id="cfg_backend"'),
    ("TTS 徽标", 'id="cfg_tts_backend"'),
    ("TTS 引擎选择", 'id="tts_engine"'),
    ("设备行", 'id="tts_device_row"'),
    ("SF Key 行", 'id="sf_key_row"'),
    ("SF 测试按钮", 'id="sf_test_btn"'),
    ("质检面板", 'id="out_qc"'),
    ("质检按钮", 'id="out_qc_btn"'),
    ("参考表体", 'id="tr_body"'),
    ("说话人表体", 'id="spk_body"'),
]

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
    return ok


# ---------------------------------------------------------------- HTTP（绕代理）


def _opener():
    # 本机 shell 常带 HTTP_PROXY，会把 127.0.0.1 也送去代理 → 必须显式空代理
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(url: str, method: str = "GET", body: dict | None = None, timeout: float = 20):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener().open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def http_json(url: str, method: str = "GET", body: dict | None = None):
    code, raw = http(url, method, body)
    try:
        return code, json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - 非 JSON 响应也允许（404 页等）
        return code, None


# ---------------------------------------------------------------- 环境探测


def bootstrap_python() -> str:
    exe = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    for cand in (
        ROOT / "envs" / "env_base" / exe,
        Path("C:/dub_envs/env_base/Scripts/python.exe"),
    ):
        if cand.exists():
            return str(cand)
    return sys.executable


def free_port(preferred: int) -> int:
    for port in (preferred, preferred + 1, preferred + 2, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    return 0


def pick_project(explicit: str | None) -> str | None:
    if explicit:
        return explicit if (PROJECTS_DIR / explicit).is_dir() else None
    cands = []
    for d in PROJECTS_DIR.iterdir():
        if not d.is_dir() or d.name.startswith(("_", ".")):
            continue
        out = d / "09_output"
        if out.is_dir() and any(out.iterdir()):
            cands.append((d.stat().st_mtime, d.name))
    return max(cands)[1] if cands else None


def find_node() -> str | None:
    node = shutil.which("node")
    if node:
        return node
    guess = Path("C:/Users/Admin1/.workbuddy/binaries/node/versions/22.22.2-6/node.exe")
    return str(guess) if guess.exists() else None


def wait_ready(base: str, timeout: float = 40) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            code, _ = http(base + "/api/projects", timeout=3)
            if code == 200:
                return True
        except Exception:  # noqa: BLE001 - 启动期连接失败属正常
            pass
        time.sleep(0.4)
    return False


# ---------------------------------------------------------------- 检查项


def check_layer_api(base: str, project: str) -> None:
    # 1 页面与关键元素
    code, raw = http(base + "/")
    page = raw.decode("utf-8", "replace") if code == 200 else ""
    hard_missing = [
        label for label, needle in REQUIRED_IDS
        if needle not in page
    ]
    record(
        "页面关键元素齐全",
        not hard_missing,
        f"{len(REQUIRED_IDS) - len(hard_missing)}/{len(REQUIRED_IDS)}"
        + (f"，缺 {hard_missing}" if hard_missing else ""),
    )

    # 2 静态资源
    code, raw = http(base + "/i18n.js")
    record("i18n.js 可加载且含词条", code == 200 and "翻译" in raw.decode("utf-8", "replace"),
           f"HTTP {code}")

    # 3 项目列表
    code, data = http_json(base + "/api/projects")
    items = data if isinstance(data, list) else (data or {}).get("projects") or []
    record("项目列表非空", code == 200 and len(items) > 0, f"HTTP {code}，{len(items)} 个")
    if items:
        ids = [p.get("id") for p in items if isinstance(p, dict)]
        record("目标项目在列表中", project in ids, project if project in ids else f"{project} 不在 {ids[:3]}…")

    # 4 项目详情字段（前端靠这三个渲染概览/徽标）
    code, d = http_json(f"{base}/api/project/{project}")
    keys = ("backend", "tts_backend", "stage_rows", "config")
    missing = [k for k in keys if not isinstance(d, dict) or k not in d]
    record("项目详情含 backend/tts_backend/stage_rows", code == 200 and not missing,
           f"HTTP {code}，缺 {missing}" if missing else f"HTTP {code}")

    # 5 交付物列表
    code, d = http_json(f"{base}/api/project/{project}/output")
    items = d if isinstance(d, list) else (d or {}).get("files") or []
    record("交付物列表可读", code == 200 and len(items) > 0, f"HTTP {code}，{len(items)} 个文件")

    # 6 不存在的项目 → 404
    code, _ = http(f"{base}/api/project/__no_such_project__")
    record("未知项目返回 404", code == 404, f"HTTP {code}")

    # 7 坏引擎值 → 400 且不落盘（post_config 校验在保存前）
    code, _ = http_json(f"{base}/api/project/{project}/config", "POST",
                        {"config": {"tts": {"engine": "bogus_engine"}}})
    record("坏 TTS 引擎被拒绝", code == 400, f"HTTP {code}")

    # 8 音频路径穿越 → 拒绝（编码过的 ../ 也要挡住）
    code, _ = http(f"{base}/api/project/{project}/audio/%2e%2e%2f%2e%2e%2fproject.json")
    record("音频路径穿越被拒绝", code in (400, 403, 404), f"HTTP {code}")


def check_layer_ui(base: str, project: str) -> bool:
    node = find_node()
    if not node:
        return record("浏览器层（--ui）", False, "未找到 node，跳过")

    js = SCRIPTS_DIR / "webui_smoke_ui.js"
    if not js.exists():
        return record("浏览器层（--ui）", False, f"缺少 {js.name}")

    npm_root = os.environ.get("WEBUI_SMOKE_NODE_PATH") or os.environ.get("NODE_PATH")
    if not npm_root:
        guess = Path.home() / ".workbuddy/binaries/node/workspace/node_modules"
        npm_root = str(guess) if guess.exists() else ""

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if npm_root:
        env["NODE_PATH"] = npm_root
    proc = subprocess.run(
        [node, str(js), base, project],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=300,
    )
    tail = (proc.stdout or "").strip().splitlines()
    for line in tail:
        print("    " + line)
    if proc.returncode != 0 and proc.stderr:
        print("    " + proc.stderr.strip().replace("\n", "\n    ")[:800])
    return record("浏览器层（--ui）流程断言", proc.returncode == 0,
                  f"退出码 {proc.returncode}")


# ---------------------------------------------------------------- 主流程


def main() -> int:
    ap = argparse.ArgumentParser(description="WebUI 冒烟回归")
    ap.add_argument("--port", type=int, default=8798, help="首选端口（占用时自动换）")
    ap.add_argument("--project", help="指定项目 id；默认挑最新有 09_output 的项目")
    ap.add_argument("--ui", action="store_true", help="额外跑真实浏览器流程（需 playwright）")
    ap.add_argument("--keep", action="store_true", help="跑完不关闭测试服务")
    args = ap.parse_args()

    project = pick_project(args.project)
    if not project:
        print("[FAIL] 找不到可用于冒烟的项目（需要有 09_output 产出）")
        return 1

    port = free_port(args.port)
    base = f"http://127.0.0.1:{port}"
    print(f"冒烟目标：{project} @ {base}（解释器 {bootstrap_python()}）\n")

    proc = subprocess.Popen(
        [bootstrap_python(), str(SCRIPTS_DIR / "webui.py"), "--no-browser",
         "--port", str(port)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    try:
        if not wait_ready(base):
            record("测试服务启动", False, "30~40 秒内未就绪")
            return 1
        record("测试服务启动", True, f"端口 {port}")

        check_layer_api(base, project)
        if args.ui:
            check_layer_ui(base, project)
    finally:
        if args.keep:
            print(f"\n服务保留在 {base}（--keep）")
        else:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()

    failed = [r for r in RESULTS if not r[1]]
    total = len(RESULTS)
    print(f"\n冒烟结果: {total - len(failed)}/{total} 通过" + (f"，{len(failed)} 失败" if failed else ""))
    if failed:
        for name, _, detail in failed:
            print(f"  - {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
