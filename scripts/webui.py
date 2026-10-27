"""本地 Web 界面：用浏览器操作整条配音流水线。

零第三方依赖（仅 Python 标准库 + 本仓库自带的 lib/），随仓库分发即可用：

    python scripts/webui.py                # http://127.0.0.1:8765，自动打开浏览器
    python scripts/webui.py --no-browser --port 9000

功能：
- 新建项目（可浏览磁盘选源文件）/ 编辑配置
- 翻译后端一键切换：本地 MarianMT（离线）或 DeepSeek / OpenAI / 自定义兼容 API
- TTS 引擎切换：本机 IndexTTS-2.5 / GPT-SoVITS / 硅基流动 CosyVoice2（云端备选），
  云端引擎可在页面上直接测 Key 与余额（不产生合成费用）
- 一键跑流水线：全流程 / 从某阶段起 / 仅某阶段 / 强制重跑 / 单段调试
- 实时控制台与各阶段日志、断点续跑状态
- 人工复核 R1–R4：说话人表、译文表编辑，保存为 *.edited.json 编辑层（脚本永不覆盖）
- 试听参考音频 / TTS 分段 / 混音，预览与下载交付物

默认只监听 127.0.0.1；给局域网其他设备访问需显式 --host 0.0.0.0（接口无鉴权，自担风险）。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
PROJECTS_DIR = PROJECT_ROOT / "projects"
WEB_DIR = SCRIPTS_DIR / "webui"

sys.path.insert(0, str(SCRIPTS_DIR))
from lib import envs as envs_mod  # noqa: E402
from lib.config import DEFAULT_CONFIG, load_project, save_project  # noqa: E402
from lib.schemas import resolve_edited  # noqa: E402

HOST_DEFAULT = "127.0.0.1"
PORT_DEFAULT = 8765
MAX_TAIL = 128 * 1024  # 控制台 / 日志回读上限

STAGES = [
    "s01_extract_audio", "s02_separate", "s03_denoise", "s04_asr",
    "s05_translate", "s06_voice_profile", "s07_tts", "s08_align",
    "s09_mix", "s10_assemble",
]

# 复核用产物：名称 -> 机器产物路径（edited 层由 resolve_edited 推导）
ARTIFACTS = {
    "segments": "03_asr/segments.json",
    "speakers": "03_asr/speakers.json",
    "translation": "04_translate/translation.json",
    "profiles": "05_voices/profiles.json",
    "manifest": "06_tts/manifest.json",
    "align_report": "07_align/align_report.json",
}

# 浏览器可直接播放的音频前缀（限制在项目目录内）；
# work/ 放行是为了播放云端引擎连通性测试产出的 work/tts_test.wav
AUDIO_PREFIXES = ("01_audio/", "02_stems/", "05_voices/", "06_tts/", "07_align/",
                  "08_mix/", "work/")
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac"}

MIME_MAP = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".mov": "video/quicktime",
    ".webm": "video/webm", ".srt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8", ".json": "application/json; charset=utf-8",
}

API_PROVIDERS = {"deepseek", "openai", "openai_compatible"}
API_KEY_ENV = ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "DUB_LLM_API_KEY")
DEFAULT_BASE_URLS = {
    "deepseek": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
}

# TTS 引擎白名单（s07_tts.load_engine 的分发目标）；siliconflow 为云端备选
TTS_ENGINES = {"indextts2", "gpt_sovits", "siliconflow"}
SF_KEY_ENV = "SILICONFLOW_API_KEY"
SF_DEFAULT_BASE = "https://api.siliconflow.cn/v1"


class HttpError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def tail_text(path: Path, limit: int = MAX_TAIL) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > limit:
            f.seek(-limit, os.SEEK_END)
        return f.read().decode("utf-8", "replace")


def read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def merge_config(partial: dict) -> dict:
    """把提交的配置合到默认值上（省略的字段回落默认，不丢段）。"""
    def merge(base: dict, over: dict) -> dict:
        out = dict(base)
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = merge(out[k], v)
            else:
                out[k] = v
        return out

    return merge(DEFAULT_CONFIG, partial)


def effective_backend(cfg: dict) -> dict:
    """告诉前端翻译实际会走哪条路（没配 Key 时 s05 会退回本地）。"""
    tr = cfg.get("translation", {})
    provider = tr.get("provider")
    if provider == "local":
        return {"backend": "local", "label": "本地 MarianMT（离线）"}
    if provider in API_PROVIDERS:
        key = tr.get("api_key") or next(
            (os.environ[e] for e in API_KEY_ENV if os.environ.get(e)), None
        )
        if key:
            return {"backend": "api", "label": f"{provider} API"}
        if tr.get("fallback_local"):
            return {"backend": "local-fallback", "label": f"{provider}（未配 Key，将退回本地 MarianMT）"}
        return {"backend": "missing", "label": f"{provider}（未配 Key 且禁用本地兜底，运行会报错）"}
    return {"backend": "unknown", "label": str(provider)}


def effective_tts(cfg: dict) -> dict:
    """告诉前端 TTS 实际会走哪条路（云端引擎没配 Key 时运行会直接报错）。"""
    tts = cfg.get("tts", {})
    engine = str(tts.get("engine") or "indextts2")
    if engine == "siliconflow":
        sf = tts.get("siliconflow") or {}
        key = (
            sf.get("api_key")
            or (cfg.get("translation") or {}).get("api_key")
            or os.environ.get(SF_KEY_ENV)
        )
        if key:
            return {"engine": engine, "backend": "cloud",
                    "label": "硅基流动 CosyVoice2（云端，已配 Key）"}
        return {"engine": engine, "backend": "missing",
                "label": "硅基流动（未配 Key：填下方 Key / 翻译 Key / 环境变量 "
                         "SILICONFLOW_API_KEY 任一即可）"}
    if engine == "gpt_sovits":
        home = Path(str(tts.get("gpt_sovits_home") or ""))
        venv = home / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if venv.exists():
            return {"engine": engine, "backend": "local",
                    "label": f"GPT-SoVITS（{home}）"}
        return {"engine": engine, "backend": "missing",
                "label": f"GPT-SoVITS（未找到 venv：{venv}）"}
    if engine == "indextts2":
        return {"engine": engine, "backend": "local",
                "label": f"IndexTTS-2.5（本机 {tts.get('device') or 'cuda'}）"}
    return {"engine": engine, "backend": "unknown", "label": engine}


def bootstrap_python() -> str:
    """pipeline.py / 各阶段脚本自己会按需切 env_*，这里只需一个能跑标准库的解释器。"""
    exe = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    for cand in (
        PROJECT_ROOT / "envs" / "env_base" / exe,
        Path("C:/dub_envs/env_base/Scripts/python.exe"),
    ):
        if cand.exists():
            return str(cand)
    py = shutil.which("py")
    if py:
        return py
    return sys.executable


def qc_python() -> str:
    """质检解释器：优先 env_asr（含 numpy/torch/pyannote，声纹相似度项可跑）；
    缺失回退 env_base（该项记 WARN 跳过，其余 9 项不受影响——I-1 三态语义）。"""
    try:
        cand = envs_mod.env_python("env_asr")
        if cand.exists():
            return str(cand)
    except Exception:
        pass
    return bootstrap_python()


QC_LOG_DIR = PROJECT_ROOT / "work" / "qc"


def parse_qc_log(text: str) -> dict:
    """从质检日志解析结果：{"rows": [...], "summary": str|None}。

    qc.py --json 的 JSON 数组整体打印（indent=2，"[" 单独一行；
    每行又被 Runner._pump 加了 [HH:MM:SS] 前缀，先剥再定位）。
    注意不能简单找首个 "[" 开头的行——[PASS]/[WARN] 检查行也是 "[" 开头。
    日志被截断（JSON 不完整）时返回空 rows，前端显示"解析不到结果"。
    """
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\[\d{2}:\d{2}:\d{2}\] ", "", line)
        lines.append(line)
    summary = next((ln for ln in lines if ln.startswith("质检结果:")), None)
    rows: list[dict] | None = None
    start = next(
        (i for i, ln in enumerate(lines) if ln.strip() == "[" or ln.lstrip().startswith("[{")),
        None,
    )
    if start is not None:
        blob = "\n".join(lines[start:])
        try:
            obj, _ = json.JSONDecoder().raw_decode(blob)
            if isinstance(obj, list):
                rows = obj
        except json.JSONDecodeError:
            rows = None
    return {"rows": rows, "summary": summary}


def parse_console_stages(text: str) -> dict:
    """从控制台尾部解析各阶段最近一次事件：running / skipped / done / failed。

    兼容带与不带 [HH:MM:SS] 前缀的行（旧日志无前缀）。
    """
    pre = r"(?:\[\d{2}:\d{2}:\d{2}\] )?"
    pats = [
        (re.compile(pre + r"=== (s\d\d_[a-z_]+)"), "running"),
        (re.compile(pre + r"\[skip\] (s\d\d_[a-z_]+)"), "skipped"),
        (re.compile(pre + r"\[done\] (s\d\d_[a-z_]+)"), "done"),
        (re.compile(pre + r"\[fail\] (s\d\d_[a-z_]+)"), "failed"),
    ]
    events: dict[str, str] = {}
    for line in text.splitlines():
        for pat, ev in pats:
            m = pat.match(line)
            if m:
                events[m.group(1)] = ev
                break
    return events


def log_started_at(project_dir: Path, stage: str) -> str | None:
    """从阶段日志头部取上次真实执行的开始时间（# started 行）。"""
    p = project_dir / "logs" / f"{stage}.log"
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(6):
                line = f.readline()
                if not line:
                    break
                m = re.match(r"^# started (\S+)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def build_stage_rows(project_dir: Path, state: dict, running: bool, now: datetime) -> list[dict]:
    """给概览页汇总每个阶段：状态 + 开始/结束时间 + 耗时/已运行时长。

    状态优先级：正在跑的阶段 > 失败 > 本次运行跳过 > 完成 > 上次中断 > 未运行。
    跳过的阶段仍显示上一次真实执行的时间。
    """
    events = parse_console_stages(tail_text(project_dir / "work" / "webui_run.log"))
    rows = []
    for s in STAGES:
        rec = state.get("stages", {}).get(s) or {}
        ev = events.get(s)
        if running and ev == "running":
            status = "running"
        elif rec.get("status") == "failed" or ev == "failed":
            status = "failed"
        elif rec.get("status") == "partial":
            status = "partial"
        elif rec.get("status") == "done":
            status = "skipped" if ev == "skipped" else "done"
        elif ev == "skipped":
            status = "skipped"
        elif ev == "done":
            status = "done"
        elif ev == "running":
            status = "interrupted"
        else:
            status = "pending"

        started = log_started_at(project_dir, s)
        finished = rec.get("finished")
        duration = elapsed = None
        try:
            t0 = datetime.fromisoformat(started) if started else None
            if status == "running" and t0 is not None:
                elapsed = max(0.0, (now - t0).total_seconds())
            elif t0 is not None and finished:
                duration = max(0.0, (datetime.fromisoformat(finished) - t0).total_seconds())
        except ValueError:
            pass
        rows.append({
            "id": s,
            "status": status,
            "started": started,
            "finished": finished,
            "duration": duration,
            "elapsed": elapsed,
            "message": rec.get("message"),
            "has_log": (project_dir / "logs" / f"{s}.log").exists(),
        })
    return rows


class Runner:
    """同一时间只允许一条流水线在跑（GPU / 磁盘都吃不消并发）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.project_id: str | None = None
        self.kind: str = "pipeline"  # pipeline | selftest，前端据此区分互斥提示
        self.exit_code: int | str | None = None
        self._log_file = None
        self.started_at: float | None = None
        self.args: list[str] = []

    def _start(self, cmd: list[str], log: Path, label: str, kind: str) -> None:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                raise HttpError(409, f"「{self.project_id}」的任务正在运行，请先停止或等待完成")
            log.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(log, "w", encoding="utf-8", errors="replace")
            # 子进程所有输出经由管道由 _pump 线程加 [HH:MM:SS] 前缀落盘；
            # PYTHONUNBUFFERED 保证子进程逐行实时到达，界面才能看到实时进度
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=str(PROJECT_ROOT), env=env,
            )
            self.project_id = label
            self.kind = kind
            self.exit_code = None
            self.started_at = time.time()
            self.args = []
            threading.Thread(target=self._pump, daemon=True).start()

    def start(self, project_dir: Path, extra_args: list[str]) -> None:
        self._start(
            [bootstrap_python(), str(SCRIPTS_DIR / "pipeline.py"), str(project_dir), *extra_args],
            project_dir / "work" / "webui_run.log", project_dir.name, "pipeline",
        )
        self.args = list(extra_args)
        print(f"[webui] 启动流水线: {' '.join(extra_args)} -> {project_dir.name} (pid {self.proc.pid})")

    def start_selftest(self) -> None:
        self._start(
            [bootstrap_python(), str(SCRIPTS_DIR / "selftest.py")],
            PROJECT_ROOT / "work" / "selftest.log", "环境自检", "selftest",
        )
        print(f"[webui] 启动自检 (pid {self.proc.pid})")

    def start_qc(self, project_dir: Path) -> None:
        self._start(
            [qc_python(), str(SCRIPTS_DIR / "qc.py"), str(project_dir), "--json"],
            QC_LOG_DIR / f"{project_dir.name}.log", project_dir.name, "qc",
        )
        print(f"[webui] 启动质检: {project_dir.name} (pid {self.proc.pid})")

    def start_batch(self, src: str, inherit: str | None, boost: float) -> None:
        args = ["--src", src, "--boost", str(boost)]
        if inherit:
            args += ["--inherit", inherit]
        self._start(
            [bootstrap_python(), str(SCRIPTS_DIR / "batch_class.py"), *args],
            PROJECT_ROOT / "work" / "batch" / "batch_webui.log", f"批量: {Path(src).name}", "batch",
        )
        print(f"[webui] 启动批量 (pid {self.proc.pid}): {src}")

    def _pump(self) -> None:
        f, proc = self._log_file, self.proc
        if not f or not proc or not proc.stdout:
            return
        ts_re = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]")
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            # 脚本自带时间戳（如批量 log）就不重复包一层
            prefixed = line if ts_re.match(line) else f"[{time.strftime('%H:%M:%S')}] {line}"
            try:
                f.write(prefixed + "\n")
                f.flush()
            except (ValueError, OSError):
                return  # 日志文件已被 _close_log 关闭
        try:
            proc.stdout.close()
        except OSError:
            pass

    def stop(self) -> bool:
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                return False
            pid = self.proc.pid
            if os.name == "nt":
                # 必须杀进程树：只 terminate 的话，正在跑的 s0x 子进程会变成孤儿继续占 GPU
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            else:
                try:
                    import signal
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except Exception:
                    self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                pass
            self._close_log()
            self.proc = None
            self.exit_code = "stopped"
            print(f"[webui] 已停止流水线 (pid {pid})")
            return True

    def status(self) -> dict:
        with self._lock:
            running = self.proc is not None and self.proc.poll() is None
            if not running and self.proc is not None:
                if self.exit_code is None:
                    self.exit_code = self.proc.returncode
                self._close_log()
                self.proc = None
            out = {
                "running": running,
                "project": self.project_id,
                "kind": self.kind,
                "exit_code": self.exit_code,
                "started_at": self.started_at,
                "args": self.args,
            }
            if running and self.started_at is not None:
                out["elapsed"] = max(0.0, time.time() - self.started_at)
            return out

    def _close_log(self):
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None


GET_ROUTES = [
    (re.compile(r"^/api/projects$"), "list_projects"),
    (re.compile(r"^/api/batch$"), "get_batch"),
    (re.compile(r"^/api/project/([^/]+)$"), "get_project"),
    (re.compile(r"^/api/project/([^/]+)/console$"), "get_console"),
    (re.compile(r"^/api/project/([^/]+)/log/([A-Za-z0-9_]+)$"), "get_stage_log"),
    (re.compile(r"^/api/project/([^/]+)/artifact/([a-z_]+)$"), "get_artifact"),
    (re.compile(r"^/api/project/([^/]+)/audio/(.+)$"), "get_audio"),
    (re.compile(r"^/api/project/([^/]+)/output$"), "list_output"),
    (re.compile(r"^/api/project/([^/]+)/output/([^/]+)$"), "get_output"),
    (re.compile(r"^/api/project/([^/]+)/qc$"), "get_qc"),
    (re.compile(r"^/api/browse$"), "browse"),
    (re.compile(r"^/api/selftest$"), "get_selftest"),
]

POST_ROUTES = [
    (re.compile(r"^/api/projects$"), "create_project"),
    (re.compile(r"^/api/batch/start$"), "post_batch_start"),
    (re.compile(r"^/api/batch/stop$"), "post_batch_stop"),
    (re.compile(r"^/api/project/([^/]+)/config$"), "post_config"),
    (re.compile(r"^/api/project/([^/]+)/tts_test$"), "post_tts_test"),
    (re.compile(r"^/api/project/([^/]+)/qc$"), "post_qc"),
    (re.compile(r"^/api/project/([^/]+)/run$"), "post_run"),
    (re.compile(r"^/api/project/([^/]+)/stop$"), "post_stop"),
    (re.compile(r"^/api/project/([^/]+)/edited/([a-z_]+)$"), "post_edited"),
    (re.compile(r"^/api/selftest$"), "post_selftest"),
]


class DubServer(ThreadingHTTPServer):
    """客户端中途断开（关页面 / 拖进度条）是常态，静默即可，不打日志也不崩。"""

    def handle_error(self, request, client_address):
        import sys

        exc = sys.exception()
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "DubWebUI/1.0"
    protocol_version = "HTTP/1.1"
    runner = Runner()

    # ---------- 基础 ----------

    def log_message(self, fmt, *args):  # 控制台不刷请求日志
        pass

    def _json(self, obj, status: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            raise HttpError(400, "请求体不是合法 JSON")
        if not isinstance(obj, dict):
            raise HttpError(400, "请求体必须是 JSON 对象")
        return obj

    def _safe_project(self, pid: str) -> Path:
        d = (PROJECTS_DIR / pid).resolve()
        if not str(d).startswith(str(PROJECTS_DIR.resolve())) or not (d / "project.json").exists():
            raise HttpError(404, f"项目不存在: {pid}")
        return d

    def _safe_inside(self, root: Path, rel: str) -> Path:
        p = (root / rel).resolve()
        if not str(p).startswith(str(root.resolve())):
            raise HttpError(403, "路径越界")
        return p

    def _serve_file(self, path: Path, download: bool = False):
        if not path.exists() or not path.is_file():
            raise HttpError(404, f"文件不存在: {path.name}")
        size = path.stat().st_size
        mime = (
            MIME_MAP.get(path.suffix.lower())
            or mimetypes.guess_type(path.name)[0]
            or "application/octet-stream"
        )

        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                elif m.group(2):  # bytes=-N 后缀形式
                    start = max(0, size - int(m.group(2)))
                    end = size - 1
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                status = 206

        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Last-Modified", self.http_date(path.stat().st_mtime))
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1024 * 512, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                    # 客户端断开（关页面 / 拖动进度条）是常态，静默收尾
                    self.close_connection = True
                    return
                remaining -= len(chunk)

    @staticmethod
    def http_date(ts: float) -> str:
        import email.utils
        return email.utils.formatdate(ts, usegmt=True)

    # ---------- 路由 ----------

    def _safe_json(self, payload: dict, status: int) -> None:
        """兜底响应也要防二次异常：连接已断时写 socket 会再抛。"""
        try:
            self._json(payload, status)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path in ("/", "/index.html"):
                self._serve_file(WEB_DIR / "index.html")
                return
            if path == "/i18n.js":
                self._serve_file(WEB_DIR / "i18n.js")
                return
            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path.startswith("/api/"):
                for pattern, name in GET_ROUTES:
                    m = pattern.match(path)
                    if m:
                        getattr(self, name)(*m.groups())
                        return
                raise HttpError(404, f"未知接口: {path}")
            raise HttpError(404, "未找到")
        except HttpError as e:
            self._safe_json({"error": str(e)}, e.status)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True
        except Exception as e:  # noqa: BLE001
            self._safe_json({"error": f"服务器内部错误: {e}"}, 500)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            for pattern, name in POST_ROUTES:
                m = pattern.match(path)
                if m:
                    getattr(self, name)(*m.groups())
                    return
            raise HttpError(404, "未知接口")
        except HttpError as e:
            self._safe_json({"error": str(e)}, e.status)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True
        except Exception as e:  # noqa: BLE001
            self._safe_json({"error": f"服务器内部错误: {e}"}, 500)

    def do_HEAD(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path.startswith("/api/project/"):
                for pattern, name in GET_ROUTES:
                    m = pattern.match(path)
                    if m:
                        getattr(self, name)(*m.groups())
                        return
            raise HttpError(404, "未找到")
        except HttpError as e:
            self._json({"error": str(e)}, e.status)

    # ---------- 项目 ----------

    def list_projects(self):
        items = []
        if PROJECTS_DIR.exists():
            for d in sorted(PROJECTS_DIR.iterdir()):
                if d.name == "_template" or not (d / "project.json").exists():
                    continue
                meta = read_json(d / "project.json") or {}
                items.append({
                    "id": d.name,
                    "created": meta.get("created"),
                    "source_file": meta.get("source_file"),
                })
        self._json({"projects": items})

    def create_project(self):
        body = self._body()
        pid = str(body.get("project_id") or "").strip()
        source = str(body.get("source_path") or "").strip()
        if not re.match(r"^[^\\/:*?\"<>|]{1,64}$", pid):
            raise HttpError(400, "项目ID不合法：1-64 字符，不含 \\ / : * ? \" < > |")
        if Path(source).suffix.lower() not in VIDEO_EXTS | AUDIO_EXTS:
            raise HttpError(400, f"源文件不是媒体文件: {source}")
        if not Path(source).exists():
            raise HttpError(400, f"源文件不存在: {source}")
        if (PROJECTS_DIR / pid).exists():
            raise HttpError(409, f"项目已存在: {pid}")
        # 复制源文件可能很慢（几 GB 的片子），放宽到 1 小时；前端有进度提示
        result = subprocess.run(
            [bootstrap_python(), str(SCRIPTS_DIR / "new_project.py"), pid, source],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=3600,
        )
        if result.returncode != 0:
            msg = (result.stderr or result.stdout or "创建失败").strip()
            raise HttpError(500, f"new_project.py 失败: {msg[-500:]}")
        d = PROJECTS_DIR / pid
        project = load_project(d)
        cfg = project.get("config", {})

        inherit = str(body.get("inherit_from") or "").strip()
        if inherit and inherit != pid:
            src_dir = self._safe_project(inherit)
            src_cfg = load_project(src_dir).get("config", {})
            if src_cfg.get("translation"):
                cfg["translation"] = src_cfg["translation"]

        device = str(body.get("device") or "").strip()
        if device in ("cpu", "cuda"):
            for sec in ("separation", "asr", "tts"):
                if isinstance(cfg.get(sec), dict):
                    cfg[sec]["device"] = device

        # 单主播模式：全片一人，s04 跳过说话人分离，克隆一次全程复用
        if "single_speaker" in body:
            cfg["single_speaker"] = bool(body["single_speaker"])

        project["config"] = cfg
        save_project(d, project)
        self._json({"ok": True, "id": pid, "backend": effective_backend(cfg)})

    def get_project(self, pid):
        d = self._safe_project(pid)
        project = read_json(d / "project.json") or {}
        state = read_json(d / "state.json") or {"stages": {}}
        run = self.runner.status()
        cfg = load_project(d)["config"]
        logs = sorted(p.stem for p in (d / "logs").glob("*.log")) if (d / "logs").exists() else []
        self._json({
            "id": d.name,
            "project": {
                "project_id": project.get("project_id"),
                "created": project.get("created"),
                "source_file": project.get("source_file"),
            },
            "config": cfg,
            "state": state,
            "stage_rows": build_stage_rows(d, state, bool(run.get("running")), datetime.now()),
            "run": run,
            "backend": effective_backend(cfg),
            "tts_backend": effective_tts(cfg),
            "logs": logs,
            "has_output": (d / "09_output").exists(),
        })

    # ---------- 配置 ----------

    def post_config(self, pid):
        d = self._safe_project(pid)
        body = self._body()
        cfg_in = body.get("config")
        if not isinstance(cfg_in, dict):
            raise HttpError(400, "缺少 config 字段")
        merged = merge_config(cfg_in)
        provider = merged["translation"].get("provider")
        if provider not in API_PROVIDERS | {"local"}:
            raise HttpError(400, f"不支持的 translation.provider: {provider}")
        if provider == "openai_compatible" and not merged["translation"].get("base_url"):
            raise HttpError(400, "自定义兼容 API 需要填写 base_url")
        engine = str(merged["tts"].get("engine") or "indextts2")
        if engine not in TTS_ENGINES:
            raise HttpError(
                400, f"不支持的 tts.engine: {engine}（可选：{', '.join(sorted(TTS_ENGINES))}）")
        if engine == "gpt_sovits" and not merged["tts"].get("gpt_sovits_home"):
            raise HttpError(400, "tts.engine=gpt_sovits 需要填写 gpt_sovits_home")
        project = load_project(d)
        project["config"] = merged
        save_project(d, project)
        self._json({"ok": True, "config": merged,
                    "backend": effective_backend(merged),
                    "tts_backend": effective_tts(merged)})

    # ---------- 运行 ----------

    def post_run(self, pid):
        d = self._safe_project(pid)
        body = self._body()
        mode = body.get("mode", "all")
        extra: list[str] = []
        if mode in ("from", "only"):
            stage = body.get("stage")
            if stage not in STAGES:
                raise HttpError(400, f"非法阶段: {stage}")
            extra += ["--from" if mode == "from" else "--only", stage]
        elif mode != "all":
            raise HttpError(400, f"非法 mode: {mode}")
        if body.get("force"):
            extra.append("--force")
        seg = str(body.get("seg") or "").strip()
        if seg:
            if not re.match(r"^[\w,\-]+$", seg):
                raise HttpError(400, f"非法 seg: {seg}")
            extra += ["--seg", seg]
        self.runner.start(d, extra)
        self._json({"ok": True})

    def post_stop(self, pid):
        self._safe_project(pid)
        self._body()  # 消费请求体，避免 keep-alive 下污染后续请求
        stopped = self.runner.stop()
        self._json({"ok": True, "stopped": stopped})

    def post_tts_test(self, pid):
        """TTS 引擎连通性测试（硅基流动云端备选）：端到端小样合成。

        优先用项目自己的参考音频合成一句 2 字段名（同时验证音色上传与合成
        端点，费用可忽略；音色 URI 缓存正式跑 s07 时直接复用）；s06 未跑
        （无参考音频）时退化为 chat 端点验 Key + 余额。
        """
        self._safe_project(pid)
        body = self._body()
        d = PROJECTS_DIR / pid
        cfg = load_project(d)["config"]
        tts = cfg.get("tts", {})
        engine = str(body.get("engine") or tts.get("engine") or "indextts2")
        if engine != "siliconflow":
            self._json({"ok": False,
                        "error": f"暂不支持测试 {engine}（本地引擎跑一次 s07 即验证）"})
            return
        sf = tts.get("siliconflow") or {}
        # 表单里刚填但未保存的 Key 优先（先测再保存）
        form_key = str(body.get("api_key") or "").strip()
        api_key = form_key or sf.get("api_key") or (
            (cfg.get("translation") or {}).get("api_key")
        ) or os.environ.get(SF_KEY_ENV)
        if not api_key:
            self._json({"ok": False,
                        "error": "未配置 API Key（tts.siliconflow.api_key / 翻译 Key / "
                                 "环境变量 SILICONFLOW_API_KEY 任一即可）"})
            return
        if form_key:
            cfg["tts"]["siliconflow"] = {**sf, "api_key": api_key}

        ref = None
        voices_dir = d / "05_voices"
        if voices_dir.exists():
            for cand in sorted(voices_dir.glob("*/reference.wav")):
                ref = cand
                break
        if ref is not None:
            try:
                import s07_tts_sf  # 纯标准库实现，env_base 可直接导入
                eng = s07_tts_sf.load_engine(cfg)
                out = d / "work" / "tts_test.wav"
                eng.infer(spk_audio_prompt=str(ref), text="测试",
                          output_path=str(out), duration_factor=1.0)
                self._json({"ok": True, "mode": "tts",
                            "message": f"云端合成成功（{ref.parent.name} 音色），Key 可用",
                            "audio": f"/api/project/{pid}/audio/work/tts_test.wav"})
            except Exception as e:  # noqa: BLE001 - 云端错误原样透出
                self._json({"ok": False, "mode": "tts", "error": f"云端合成失败: {e}"})
            return

        # 无参考音频：同一账户的 chat 端点验 Key + 余额（不产生合成）
        base = str(sf.get("base_url") or SF_DEFAULT_BASE).rstrip("/")
        tr = cfg.get("translation") or {}
        # 翻译后端就在硅基流动时才复用它的模型；本地 MarianMT 等模型名云端不存在
        model = tr.get("model") or "deepseek-ai/DeepSeek-V3"
        if "siliconflow" not in str(tr.get("base_url") or ""):
            model = "deepseek-ai/DeepSeek-V3"
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1,
            }).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                json.loads(r.read().decode("utf-8"))
            self._json({"ok": True, "mode": "key",
                        "message": "Key 可用（chat 端点验证通过；本项目未跑 s06，"
                                   "无法验证合成，正式跑前会先建音色）"})
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            self._json({"ok": False, "mode": "key", "error": f"HTTP {exc.code}: {detail}"})
        except urllib.error.URLError as exc:
            self._json({"ok": False, "mode": "key", "error": f"网络错误: {exc}"})

    def post_selftest(self):
        self._body()  # keep-alive 下必须消费请求体，否则残留在流里污染下一个请求
        self.runner.start_selftest()
        self._json({"ok": True})

    # ---------- 批量 ----------

    def _batch_status_payload(self, run: dict) -> dict:
        st_path = PROJECT_ROOT / "work" / "batch" / "status.json"
        data = read_json(st_path) or {}
        return {
            "run": run,
            "status": data,
            "log": tail_text(PROJECT_ROOT / "work" / "batch" / "batch_webui.log"),
        }

    def get_batch(self):
        run = self.runner.status()
        mine = run.get("kind") == "batch"
        run = {**run, "running": bool(run.get("running")) and mine}
        if not mine:
            run["project"] = run.get("project") if mine else None
        self._json(self._batch_status_payload(run))

    def post_batch_start(self):
        body = self._body()
        src = str(body.get("src") or "").strip()
        if not src or not Path(src).exists():
            raise HttpError(400, f"视频目录不存在: {src}")
        inherit = str(body.get("inherit") or "").strip() or None
        if inherit:
            # UI 传的是项目 id，也可能是完整路径，两种都认
            cand = Path(inherit)
            if not (cand / "project.json").exists() and (PROJECTS_DIR / inherit / "project.json").exists():
                cand = PROJECTS_DIR / inherit
            if not (cand / "project.json").exists():
                raise HttpError(400, f"继承项目无效（缺 project.json）: {inherit}")
            inherit = str(cand)
        boost = body.get("boost") or 1.15
        try:
            boost = max(1.0, min(1.5, float(boost)))
        except (TypeError, ValueError):
            raise HttpError(400, f"非法 boost: {boost}")
        self.runner.start_batch(src, inherit, boost)
        self._json({"ok": True})

    def post_batch_stop(self):
        self._body()
        run = self.runner.status()
        if run.get("kind") != "batch":
            self._json({"ok": True, "stopped": False})
            return
        stopped = self.runner.stop()
        self._json({"ok": True, "stopped": stopped})

    def get_selftest(self):
        st = self.runner.status()
        mine = st.get("kind") == "selftest"
        self._json({
            "running": bool(st.get("running")) and mine,
            "exit_code": st.get("exit_code") if mine else None,
            "log": tail_text(PROJECT_ROOT / "work" / "selftest.log"),
        })

    def get_console(self, pid):
        d = self._safe_project(pid)
        run = self.runner.status()
        self._json({
            "tail": tail_text(d / "work" / "webui_run.log"),
            "run": run,
        })

    def get_stage_log(self, pid, stage):
        d = self._safe_project(pid)
        if stage not in STAGES:
            raise HttpError(400, f"非法阶段: {stage}")
        self._json({"stage": stage, "tail": tail_text(d / "logs" / f"{stage}.log")})

    # ---------- 复核 ----------

    def get_artifact(self, pid, name):
        d = self._safe_project(pid)
        rel = ARTIFACTS.get(name)
        if rel is None:
            raise HttpError(404, f"未知产物: {name}")
        path = resolve_edited(d, rel)
        data = read_json(path)
        self._json({
            "file": str(path.relative_to(d)).replace("\\", "/"),
            "edited": path.name.endswith(".edited.json"),
            "data": data,
        })

    def post_edited(self, pid, name):
        d = self._safe_project(pid)
        rel = ARTIFACTS.get(name)
        if rel is None:
            raise HttpError(404, f"未知产物: {name}")
        body = self._body()
        data = body.get("data")
        if not isinstance(data, dict):
            raise HttpError(400, "缺少 data 字段")
        edited_path = Path(str(d / rel).removesuffix(".json") + ".edited.json")
        edited_path.parent.mkdir(parents=True, exist_ok=True)
        edited_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self._json({
            "ok": True,
            "file": str(edited_path.relative_to(d)).replace("\\", "/"),
            "note": "编辑层已保存，下游优先读取它；脚本永不覆盖",
        })

    # ---------- 文件 ----------

    def get_audio(self, pid, rel):
        d = self._safe_project(pid)
        rel = rel.replace("\\", "/")
        if not rel.startswith(AUDIO_PREFIXES):
            raise HttpError(403, f"不在允许的目录内: {rel}")
        path = self._safe_inside(d, rel)
        if path.suffix.lower() not in AUDIO_EXTS:
            raise HttpError(403, "仅允许音频文件")
        self._serve_file(path)

    def list_output(self, pid):
        d = self._safe_project(pid)
        out_dir = d / "09_output"
        files = []
        if out_dir.exists():
            for f in sorted(out_dir.iterdir()):
                if f.is_file():
                    files.append({
                        "name": f.name,
                        "size": f.stat().st_size,
                        "mtime": f.stat().st_mtime,
                    })
        self._json({"files": files})

    def get_output(self, pid, name):
        d = self._safe_project(pid)
        path = self._safe_inside(d / "09_output", name)
        if path.suffix.lower() not in MIME_MAP:
            raise HttpError(403, "文件类型不允许")
        download = (parse_qs(urlparse(self.path).query).get("download") or [""])[0] == "1"
        self._serve_file(path, download=download)

    # ---------- 质检（I-2：交付页一键跑 qc.py，异步启动 + 轮询日志）----------

    def post_qc(self, pid):
        d = self._safe_project(pid)
        self._body()  # 消费请求体，避免 keep-alive 下污染后续请求
        out_dir = d / "09_output"
        has_output = out_dir.exists() and any(
            f.suffix.lower() in (".mp4", ".m4a", ".wav") for f in out_dir.iterdir() if f.is_file()
        )
        if not has_output:
            raise HttpError(400, "09_output 下还没有交付物，先跑完流水线再质检")
        self.runner.start_qc(d)  # 正在跑流水线/自检时抛 409，直接透出
        self._json({"ok": True})

    def get_qc(self, pid):
        d = self._safe_project(pid)
        st = self.runner.status()
        mine = st.get("kind") == "qc" and st.get("project") == pid
        running = bool(st.get("running")) and mine
        log_path = QC_LOG_DIR / f"{pid}.log"
        text = tail_text(log_path) if log_path.exists() else ""
        parsed = parse_qc_log(text)
        payload = {
            "running": running,
            "exit_code": st.get("exit_code") if mine else None,
            "rows": parsed["rows"],
            "summary": parsed["summary"],
            "log": text,
        }
        # 没在跑时 rows/summary 来自上次日志（历史结果），前端可直接渲染
        self._json(payload)

    # ---------- 浏览磁盘（新建项目选源文件用）----------

    def browse(self):
        query = parse_qs(urlparse(self.path).query)
        raw = (query.get("path") or [""])[0]
        if not raw:
            entries = []
            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                drive = f"{letter}:\\"
                if Path(drive).exists():
                    entries.append({"name": drive, "type": "dir"})
            self._json({"path": "", "parent": "", "entries": entries})
            return
        root = Path(raw)
        if not root.exists():
            raise HttpError(400, f"目录不存在: {raw}")
        entries = []
        try:
            for p in sorted(root.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
                if p.is_dir():
                    entries.append({"name": p.name, "type": "dir"})
                elif p.is_file() and p.suffix.lower() in VIDEO_EXTS | AUDIO_EXTS:
                    entries.append({"name": p.name, "type": "media", "size": p.stat().st_size})
        except PermissionError:
            raise HttpError(403, f"无权限访问: {raw}")
        parent = str(root.parent) if str(root.parent) != str(root) else ""
        self._json({"path": str(root), "parent": parent, "entries": entries[:800]})


def main():
    global PROJECTS_DIR
    ap = argparse.ArgumentParser(description="配音流水线 Web 界面（零依赖，仅标准库）")
    ap.add_argument("--host", default=HOST_DEFAULT)
    ap.add_argument("--port", type=int, default=PORT_DEFAULT)
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--projects-dir", help="项目根目录（默认 <仓库>/projects/；仓库盘空间紧张时可指到大盘）")
    args = ap.parse_args()
    if args.projects_dir:
        PROJECTS_DIR = Path(args.projects_dir).resolve()
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    srv = DubServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"[webui] 服务已启动: {url}")
    print(f"[webui] 项目根目录: {PROJECT_ROOT}")
    print(f"[webui] 项目存放目录: {PROJECTS_DIR}")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[webui] 已退出")


if __name__ == "__main__":
    main()
