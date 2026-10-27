"""离线运行加固：把模型加载钉死在本地缓存，杜绝静默联网。

两个联网陷阱（2026-10 实测踩过）：

1. ``torch.hub.load('snakers4/silero-vad')`` —— torch 2.x 的 ``_parse_repo_info``
   在**查缓存之前**会先访问 ``https://api.github.com`` 解析默认分支，
   本地有缓存也照样联网，断网直接 RemoteDisconnected。
   ``patch_torch_hub_local`` 把已知仓库改写为本地缓存目录 + ``source='local'``，
   从缓存目录里的 hubconf.py 加载，全程不碰网络。

2. HuggingFace 系（faster-whisper / pyannote / transformers）——
   联网时优先发 HEAD 请求校验远端版本，网络一抖就白等超时甚至失败。
   ``force_hf_offline`` 置 ``HF_HUB_OFFLINE=1`` / ``TRANSFORMERS_OFFLINE=1``，
   让所有加载直接走本地缓存。模型权重随包分发（fetch_models.py 负责
   预下载），流水线运行期本就不该再下载任何东西。
"""
from __future__ import annotations

import os
from pathlib import Path

# torch.hub 仓库名 -> 缓存目录名（位于 <TORCH_HOME>/hub 下）
_HUB_REPO_DIRS = {
    "snakers4/silero-vad": "snakers4_silero-vad_master",
}


def force_hf_offline() -> None:
    """让 HuggingFace 系库只认本地缓存。必须在 import 它们之前调用。"""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


def patch_torch_hub_local() -> None:
    """把 torch.hub.load 的已知 GitHub 仓库重定向到本地缓存（source='local'）。

    只在缓存目录（含 hubconf.py）确实存在时改写；否则保持原行为，
    不影响需要联网首次下载的场景（fetch_models.py 的事）。
    """
    import torch.hub

    if getattr(torch.hub.load, "_dub_offline_patched", False):
        return

    from lib import envs

    hub_root = None
    # 2026-10-19 换机修复：旧机候选只有 C 盘 junction，本机都没有；
    # 依次认 ① TORCH_HOME 环境变量（各阶段模块级已用项目根设好）
    # ② 无参解析失败时用本文件位置反推项目根再解析一次。
    torch_home = os.environ.get("TORCH_HOME") or envs.torch_home()
    if not torch_home:
        project_root = Path(__file__).resolve().parents[2]
        torch_home = envs.torch_home(project_root)
    if torch_home:
        hub_root = Path(torch_home) / "hub"
    if hub_root is None or not hub_root.is_dir():
        return

    def _repo_dir(repo: str) -> Path | None:
        name = _HUB_REPO_DIRS.get(repo)
        if not name:
            return None
        candidate = hub_root / name
        if (candidate / "hubconf.py").exists():
            return candidate
        return None

    original_load = torch.hub.load

    def load(repo_or_dir, *args, **kwargs):
        local = _repo_dir(repo_or_dir) if isinstance(repo_or_dir, str) else None
        if local is not None:
            print(f"[offline] torch.hub {repo_or_dir} -> 本地缓存加载（{local.name}）")
            kwargs["source"] = "local"
            kwargs["trust_repo"] = True
            return original_load(str(local), *args, **kwargs)
        return original_load(repo_or_dir, *args, **kwargs)

    load._dub_offline_patched = True  # type: ignore[attr-defined]
    torch.hub.load = load
