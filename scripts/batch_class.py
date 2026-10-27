r"""批量转换：顺序跑完一个目录（或清单）里的所有视频，失败不阻塞，断点友好。

用法:
    # 扫描目录模式（UI「批量」页即此模式）：递归找 *.mp4，短→长排序
    python scripts/batch_class.py --src "E:\课程视频" --inherit E:\dub_projects\某已有项目

    # 清单模式：自定义 (项目id, 源视频) 列表
    python scripts/batch_class.py --playlist work\batch\playlist.json --inherit ...

选项:
    --out DIR        项目父目录（默认 E:\dub_projects）
    --inherit DIR    继承某项目的翻译/设备等配置（推荐；否则用默认值+本地翻译）
    --boost 1.15     TTS 语速提升系数（默认 1.15）
    --dry-run        只打印计划

设计:
    - 项目 id = 日期 + 文件名净化（ASCII），同一视频重跑自动跳过已完成项目
    - 每个视频: new_project → 写配置 → pipeline → qc；失败记录后继续
    - 进度实时写 work/batch/status.json（UI 批量页读它）
    - 防重入锁（pid + python 映像名校验），两个实例并行会写坏项目
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT_DEFAULT = Path(r"E:\dub_projects")
BATCH_DIR = ROOT / "work" / "batch"

ALL_STAGES = [
    "s01_extract_audio", "s02_separate", "s03_denoise", "s04_asr",
    "s05_translate", "s06_voice_profile", "s07_tts", "s08_align",
    "s09_mix", "s10_assemble",
]

# 配置继承时只抄这些段（来源 project.json 的 config）
INHERIT_KEYS = ("translation", "tts", "asr", "separation", "denoise", "mix")


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with (BATCH_DIR / "batch.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def sanitize_id(name: str) -> str:
    """文件名 -> 项目 id 片段：去路径非法字符压成下划线（中文保留，可读性优先）。"""
    keep = re.sub(r"[^\w\u4e00-\u9fff]+", "_", name, flags=re.UNICODE).strip("_")
    return keep[:40] or "video"


def scan_src(src_dir: Path) -> list[tuple[str, str]]:
    """递归扫描目录下的视频，按文件大小（≈时长）短→长排序，先生成短的。"""
    exts = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
    found = [p for p in src_dir.rglob("*") if p.suffix.lower() in exts]
    found.sort(key=lambda p: p.stat().st_size)
    tag = date.today().strftime("%y%m%d")
    return [(f"{tag}_{sanitize_id(p.stem)}", str(p)) for p in found]


def load_playlist(path: Path) -> list[tuple[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = [(it["id"], it["src"]) for it in data]
    if not items:
        raise SystemExit("清单为空")
    return items


def acquire_lock() -> bool:
    """防重入锁：已有批量实例活着时拒绝启动（两个实例并行会写坏同一项目）。

    探测不只看 pid 存活（Windows pid 会复用），还要确认映像名是 python。
    """
    lock = BATCH_DIR / "batch.pid"
    if lock.exists():
        try:
            old_pid = int(lock.read_text().strip())
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, old_pid)
            if handle:
                buf = ctypes.create_unicode_buffer(512)
                size = ctypes.c_ulong(512)
                ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
                kernel32.CloseHandle(handle)
                if ok and ("python" in buf.value.lower()):
                    print(f"已有批量实例在运行 (pid={old_pid})，退出。", flush=True)
                    return False
        except (ValueError, OSError):
            pass
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return True


def project_done(pid: str) -> bool:
    # 纯演奏视频（无语音）的交付物是原样成片，也算完成
    out_dir = OUT_ROOT / pid / "09_output"
    if out_dir.exists() and any(out_dir.glob("*_zh.*")):
        return True
    state = OUT_ROOT / pid / "state.json"
    if not state.exists():
        return False
    try:
        stages = json.loads(state.read_text(encoding="utf-8")).get("stages", {})
        return all(stages.get(s, {}).get("status") == "done" for s in ALL_STAGES)
    except (json.JSONDecodeError, OSError):
        return False


def run(cmd: list[str], log_file: Path) -> int:
    with log_file.open("a", encoding="utf-8") as f:
        return subprocess.call(
            [sys.executable, "-X", "utf8"] + cmd,
            stdout=f, stderr=subprocess.STDOUT, cwd=str(ROOT),
        )


def build_config(inherit_dir: Path | None, boost: float) -> dict:
    """默认配置 + 可选继承。绝不硬编码 API Key（开源分发）。"""
    cfg: dict = {
        "translation": {
            "provider": "local",
            "max_length_ratio": 1.15, "chars_per_second": 4.5,
            "batch_size": 20, "timeout": 120, "fallback_local": True,
            "local_model": "Helsinki-NLP/opus-mt-en-zh",
        },
        "tts": {"device": "cuda", "tempo_boost": boost},
        "asr": {"device": "cuda"},
        "separation": {"device": "cuda"},
    }
    if inherit_dir is not None:
        pj = inherit_dir / "project.json"
        if not pj.exists():
            raise SystemExit(f"--inherit 项目不存在: {inherit_dir}")
        data = json.loads(pj.read_text(encoding="utf-8-sig"))
        for key in INHERIT_KEYS:
            if isinstance(data.get("config", {}).get(key), dict):
                cfg.setdefault(key, {}).update(data["config"][key])
    # --boost 显式覆盖
    cfg["tts"]["tempo_boost"] = boost
    return cfg


OUT_ROOT = OUT_ROOT_DEFAULT  # main() 里按 --out 覆盖


def main() -> int:
    global OUT_ROOT
    ap = argparse.ArgumentParser(description="批量转换")
    ap.add_argument("--src", help="递归扫描该目录下的视频文件")
    ap.add_argument("--playlist", help="自定义清单 json：[{id, src}, ...]")
    ap.add_argument("--out", help="项目父目录（默认 E:\\dub_projects）")
    ap.add_argument("--inherit", help="继承某项目的翻译/设备配置（推荐）")
    ap.add_argument("--boost", type=float, default=1.15, help="TTS 语速提升（默认 1.15）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.out:
        OUT_ROOT = Path(args.out).resolve()
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    if not acquire_lock():
        return 2

    if args.src:
        items = scan_src(Path(args.src).resolve())
    elif args.playlist:
        items = load_playlist(Path(args.playlist))
    else:
        ap.error("--src 或 --playlist 至少给一个（默认大师课清单已移至 work/batch/playlist.json）")

    cfg = build_config(Path(args.inherit).resolve() if args.inherit else None, args.boost)
    inherit_note = f"继承 {args.inherit}" if args.inherit else "默认配置（本地翻译）"
    log(f"===== 批量开始: {len(items)} 个视频, 输出 {OUT_ROOT}, {inherit_note}")

    status = {
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "src": args.src or args.playlist,
        "items": {},
    }
    save = lambda: (BATCH_DIR / "status.json").write_text(  # noqa: E731
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

    for pid, src in items:
        entry = {"src": src, "state": "pending", "qc_exit": None, "minutes": None}
        status["items"][pid] = entry
        save()
        log(f"===== {pid} <- {src}")
        if project_done(pid):
            entry.update(state="skip", note="已完成")
            log("  skip: 已完成")
            save()
            continue
        if args.dry_run:
            entry.update(state="dry-run")
            save()
            continue
        src_path = Path(src)
        if not src_path.exists():
            entry.update(state="missing")
            log(f"  FAIL: 源不存在 {src}")
            save()
            continue

        t0 = time.time()
        entry.update(state="running")
        save()
        try:
            if not (OUT_ROOT / pid / "project.json").exists():
                code = run(["scripts/new_project.py", pid, str(src_path), "--out", str(OUT_ROOT)],
                           BATCH_DIR / f"{pid}_new.log")
                if code != 0:
                    raise RuntimeError(f"new_project 退出码 {code}")

            pjson = OUT_ROOT / pid / "project.json"
            data = json.loads(pjson.read_text(encoding="utf-8-sig"))
            for k, v in cfg.items():
                if isinstance(v, dict):
                    data["config"].setdefault(k, {}).update(v)
                else:
                    data["config"][k] = v
            pjson.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            code = run(["scripts/pipeline.py", str(OUT_ROOT / pid)], BATCH_DIR / f"{pid}_pipeline.log")
            if code != 0:
                raise RuntimeError(f"pipeline 退出码 {code}")

            qc = run(["scripts/qc.py", str(OUT_ROOT / pid), "--json"], BATCH_DIR / f"{pid}_qc.log")
            entry.update(state="ok" if qc == 0 else "qc-fail", qc_exit=qc)
            log(f"  done in {(time.time()-t0)/60:.1f} min, qc_exit={qc}")

            # 质检通过即自动归拢到「成片汇总」（幂等；失败不影响批量）
            try:
                r = run(["scripts/collect_outputs.py", "--projects-root", str(OUT_ROOT),
                         "--project", pid], BATCH_DIR / f"{pid}_collect.log")
                if r == 0:
                    log("  已归拢到 成片汇总/")
            except Exception as exc:  # noqa: BLE001
                log(f"  归拢失败（不阻塞）: {exc}")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            entry.update(state="fail", note=f"{type(e).__name__}: {e}"[:200])
            log(f"  FAIL: {e}")
        entry["minutes"] = round((time.time() - t0) / 60, 1)
        save()

    status["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save()
    log(f"===== 批量结束: {sum(1 for v in status['items'].values() if v['state'] in ('ok','skip'))} 完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
