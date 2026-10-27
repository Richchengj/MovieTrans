"""GPU 显存探测与自动降级（E-2，2026-10-18）。

各阶段（s02/s04/s07）在用 `cfg[...]["device"]` 前先过 `effective_device()`：
    - 配置为 cpu → 直接 cpu
    - 配置为 cuda → 探测空闲显存：无卡或低于阶段需求 → 自动降级 cpu + 警告
    - 降级只改**内存中的 cfg**，不写回 project.json、不影响断点指纹

环境变量：
    DUB_FORCE_CPU=1        强制 CPU（显式关 GPU）
    DUB_ALLOW_LOW_VRAM=1   显存不足也强制 GPU（自担 OOM 风险）

纯标准库（nvidia-smi 为外部命令），符合 scripts/lib 纪律。
"""
from __future__ import annotations

import os
import subprocess

_warned = False  # 每进程只警告一次（一个阶段脚本一个进程）


def free_vram_gb() -> float | None:
    """nvidia-smi 查最大空闲显存（GB）；无 NVIDIA GPU / 命令失败返回 None。"""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        vals = [int(x.strip()) for x in r.stdout.strip().splitlines() if x.strip().isdigit()]
        return max(vals) / 1024.0 if vals else None
    except Exception:
        return None


def effective_device(configured: str, need_gb: float, stage: str) -> str:
    """返回本阶段实际可用的 device（cuda/cpu），不足时降级并警告。"""
    global _warned
    configured = str(configured or "cpu").lower()
    if configured != "cuda":
        return "cpu"

    if os.environ.get("DUB_FORCE_CPU") == "1":
        print(f"[gpu] DUB_FORCE_CPU=1：{stage} 强制 CPU")
        return "cpu"

    free = free_vram_gb()
    if free is None:
        if not _warned:
            print(f"[gpu] 未检测到 NVIDIA GPU：{stage} 降级 CPU")
            _warned = True
        return "cpu"

    if free >= need_gb or os.environ.get("DUB_ALLOW_LOW_VRAM") == "1":
        return "cuda"

    if not _warned:
        print(f"[gpu] ⚠️ GPU 空闲显存 {free:.1f}GB < {stage} 需求 ~{need_gb}GB，自动降级 CPU")
        print("[gpu]    TTS 阶段将慢约 25 倍；关闭占显存的程序（如 ComfyUI）后重跑可恢复 GPU，")
        print("[gpu]    或设 DUB_ALLOW_LOW_VRAM=1 强制 GPU（自担 OOM 风险）")
        _warned = True
    return "cpu"
