"""流水线状态与断点续跑。"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from lib import config as config_mod
from lib import logs


def file_fingerprint(path) -> str:
    p = Path(path)
    if not p.exists():
        return "missing"
    st = p.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def fingerprints(project_dir, rel_paths: Iterable[str]) -> dict:
    return {rel: file_fingerprint(Path(project_dir) / rel) for rel in rel_paths}


class PipelineState:
    def __init__(self, project_dir):
        self.project_dir = Path(project_dir)
        self.path = self.project_dir / "state.json"
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"project_id": self.project_dir.name, "stages": {}}
        self.data.setdefault("stages", {})

    def get(self, stage: str) -> dict | None:
        return self.data["stages"].get(stage)

    def should_skip(self, stage: str, input_fps: dict, cfg_hash: str) -> bool:
        rec = self.get(stage)
        if not rec or rec.get("status") != "done":
            return False
        if rec.get("input_fingerprints") != input_fps:
            return False
        if rec.get("config_hash") != cfg_hash:
            return False
        return all((self.project_dir / rel).exists() for rel in rec.get("outputs", []))

    def mark_done(
        self,
        stage: str,
        outputs: list[str],
        input_fps: dict,
        cfg_hash: str,
        config_keys: Iterable[str] = (),
        started_at: str | None = None,
        duration: float | None = None,
    ) -> None:
        self.data["stages"][stage] = {
            "status": "done",
            "started": started_at,
            "finished": datetime.now().isoformat(timespec="seconds"),
            "duration": round(duration, 1) if duration is not None else None,
            "input_fingerprints": input_fps,
            "config_hash": cfg_hash,
            "config_keys": list(config_keys),
            "outputs": list(outputs),
        }
        self.save()

    def mark_failed(self, stage: str, message: str) -> None:
        rec = self.data["stages"].get(stage, {})
        rec.update(
            {
                "status": "failed",
                "message": message,
                "finished": datetime.now().isoformat(timespec="seconds"),
            }
        )
        self.data["stages"][stage] = rec
        self.save()

    def mark_partial(self, stage: str) -> None:
        """--seg 单段调试后标记：产物只覆盖部分段落，完整运行时必须整体重算。"""
        rec = self.data["stages"].get(stage, {})
        rec.update(
            {
                "status": "partial",
                "finished": datetime.now().isoformat(timespec="seconds"),
            }
        )
        self.data["stages"][stage] = rec
        self.save()

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def run_stage(
    project_dir,
    stage: str,
    inputs: Iterable[str],
    config: dict,
    produce: Callable[[], list[str]],
    *,
    config_keys: Iterable[str],
    force: bool = False,
    partial: bool | Callable[[], bool] = False,
) -> bool:
    """执行一个阶段：输入未变则跳过，否则运行并记录状态。

    `config_keys` 声明本阶段依赖哪些顶层配置段，只有这些段变化才会触发重跑。
    必须显式声明：若整份配置一起哈希，改动任一配置都会让整条链路重算。

    `partial=True` 用于 --seg 单段调试：产出文件但**不**更新阶段指纹，
    保证下次完整运行时该阶段整体重算，不会把调试段与旧配置段拼进同一份产物。
    也可传**无参函数**，在 produce() 之后再判定（补跑是否已覆盖全量要到
    产物落盘才知道——s07 用它实现「分批补跑跑满了就标 done」，见 2026-10-22）。

    返回 True 表示实际执行了，False 表示被跳过。
    """
    state = PipelineState(project_dir)
    cfg_hash = config_mod.config_hash(config_mod.config_subset(config, config_keys))
    input_fps = fingerprints(project_dir, inputs)

    def _is_partial() -> bool:
        return bool(partial() if callable(partial) else partial)

    # partial（--seg）是用户显式指定的重做目标，即使指纹未变也必须执行
    if not force and not (callable(partial) or partial) \
            and state.should_skip(stage, input_fps, cfg_hash):
        print(f"[skip] {stage}：输入与配置未变化，跳过")
        return False

    try:
        log_path = logs.install(project_dir, stage)
        print(f"[log] {log_path.relative_to(Path(project_dir))}")
        t0 = time.monotonic()
        started_at = datetime.now().isoformat(timespec="seconds")
        outputs = produce()
        duration = time.monotonic() - t0
    except Exception as exc:  # noqa: BLE001 - 记录失败原因后继续抛出
        state.mark_failed(stage, f"{type(exc).__name__}: {exc}")
        raise

    if _is_partial():
        # 单段重跑不算阶段完成，但也不该把既有的 done 打回去——否则 qc 会把
        # 已完整跑过的阶段误报为未完成（局部修补 ≠ 阶段失效）
        was_done = (state.get(stage) or {}).get("status") == "done"
        if not was_done:
            state.mark_partial(stage)
        print(f"[done-partial] {stage}：单段产物已写入"
              + ("（阶段状态保持 done）" if was_done else
                "；下次完整运行时本阶段将整体重算"))
        return True

    state.mark_done(stage, outputs, input_fps, cfg_hash, config_keys,
                    started_at=started_at, duration=duration)
    print(f"[done] {stage}（{duration:.1f}s）-> {', '.join(outputs)}")
    return True
