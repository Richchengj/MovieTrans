"""下载跨项目共享的模型权重到 `models/`。

`models/` 是各阶段共用的权重目录。只依赖 Python 标准库（urllib 下载），
无需 huggingface_hub / modelscope 包。

下载通道（2026-10-19 新机实测速度）：
  1. modelscope.cn 直链（~3.7 MB/s，主仓库均有官方搬运）—— 首选
  2. hf-mirror.com 直链（CDN 不稳，只用于魔搭没有的小文件）
  3. dl.fbaipublicfiles.com / codeload.github.com（demucs、wav2vec2、silero 原始源）

布局约定（s04/s07 代码硬依赖，改前先读 s04_asr.py / s07_tts.py）：
  models/hf/hub/models--<org>--<name>/{refs/main, snapshots/<rev>/...}
    ↑ HF_HUB_OFFLINE=1 下 huggingface_hub 按此布局解析（快照内放真实文件，
      兼容 Windows 无符号链接环境）
  models/pyannote/speaker-diarization-3.1/{config.yaml, segmentation-3.0/...}
  models/indextts2.5/{gpt.pth,...} + hf_cache/{w2v-bert-2.0, bigvgan, ...}
  models/torch/hub/{checkpoints/*.th, snakers4_silero-vad_master}

用法:
    python scripts/fetch_models.py                 # 全部
    python scripts/fetch_models.py whisper         # 只下指定项
    python scripts/fetch_models.py --list          # 看清单
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = PROJECT_ROOT / "models"
HF_HUB_DIR = MODELS_DIR / "hf" / "hub"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

INDEXTTS_DIR = "indextts2.5"
INDEXTTS_CACHE = f"{INDEXTTS_DIR}/hf_cache"

# demucs htdemucs_ft 的 4 个权重（torch.hub checkpoints 缓存名；2026-10-19 经
# files.txt/多个 HF 镜像仓核对——旧文档里的 e57c4876/83042ffb/fbc95bd9 是错名）
DEMUCS_TH = [
    "f7e0c4bc-ba3fe64a.th",
    "d12395a8-e57c48e6.th",
    "92cfc3b6-ef3bcb9c.th",
    "04573f0d-f3cf25b2.th",
]
DEMUCS_BASE = "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer"
DEMUCS_HF_MIRROR = "dokodesuka/htdemucs_ft"  # AWS 源被墙/限流时的镜像
W2V2_ASR_URL = ("https://dl.fbaipublicfiles.com/fairseq/s2/"
                "wav2vec2_fairseq_base_ls960_asr_ls960.pth")
SILERO_TARBALL = "https://codeload.github.com/snakers4/silero-vad/tar.gz/refs/heads/master"

# opus-mt 只需推理必需文件（整仓 1.5GB，其中 1.2GB 是 tf/flax/rust 冗余）
OPUS_MT_FILES = ["config.json", "generation_config.json", "pytorch_model.bin",
                 "sentencepiece.bpe.model", "tokenizer_config.json", "vocab.json"]
# w2v-bert-2.0 只取 safetensors 一份（conformer_shaw.pt 是 fairseq 原始格式，不用）
W2V_BERT_FILES = ["config.json", "model.safetensors", "preprocessor_config.json"]

DESCRIPTIONS = {
    "whisper": "ASR 主模型 faster-whisper large-v3（HF 缓存布局）",
    "indextts": "TTS 主模型 IndexTTS-2.5",
    "indextts-aux": "IndexTTS 辅助模型（w2v-bert 单份 / BigVGAN / campplus / semantic codec）",
    "speaker": "声纹嵌入 wespeaker（HF 缓存布局，s04 聚类与预判）",
    "pyannote": "说话人分离魔搭本地管线（config + segmentation-3.0，免 token）",
    "hub": "torch.hub 缓存（demucs 4×.th + wav2vec2 对齐 + silero-vad）",
    "mt": "本地兜底翻译 opus-mt-en-zh 单文件版（有 LLM API Key 可跳过）",
}

# 每项 = 一串动作；类型见 _run_action
TARGETS: dict[str, list[tuple]] = {
    "whisper": [
        ("hf_cache", "Systran/faster-whisper-large-v3", "gpustack/faster-whisper-large-v3",
         ["model.bin", "config.json", "preprocessor_config.json",
          "tokenizer.json", "vocabulary.json"]),
    ],
    "indextts": [
        ("ms_repo", "IndexTeam/IndexTTS-2.5", INDEXTTS_DIR),
    ],
    "indextts-aux": [
        ("ms_files", "AI-ModelScope/w2v-bert-2.0", W2V_BERT_FILES,
         f"{INDEXTTS_CACHE}/w2v-bert-2.0"),
        ("hf_file", "nvidia/bigvgan_v2_22khz_80band_256x", "config.json",
         f"{INDEXTTS_CACHE}/bigvgan/config.json"),
        ("hf_file", "nvidia/bigvgan_v2_22khz_80band_256x", "bigvgan_generator.pt",
         f"{INDEXTTS_CACHE}/bigvgan/bigvgan_generator.pt"),
        ("hf_file", "funasr/campplus", "campplus_cn_common.bin",
         f"{INDEXTTS_CACHE}/campplus_cn_common.bin"),
        ("hf_file", "amphion/MaskGCT", "semantic_codec/model.safetensors",
         f"{INDEXTTS_CACHE}/semantic_codec_model.safetensors"),
    ],
    "speaker": [
        ("hf_cache", "pyannote/wespeaker-voxceleb-resnet34-LM",
         "pyannote/wespeaker-voxceleb-resnet34-LM",
         ["config.yaml", "pytorch_model.bin"]),
    ],
    "pyannote": [
        ("ms_files", "pyannote/speaker-diarization-3.1", ["config.yaml"],
         "pyannote/speaker-diarization-3.1"),
        ("ms_files", "pyannote/segmentation-3.0", ["config.yaml", "pytorch_model.bin"],
         "pyannote/speaker-diarization-3.1/segmentation-3.0"),
    ],
    "hub": [
        ("url_file", f"{DEMUCS_BASE}/{name}", f"torch/hub/checkpoints/{name}")
        for name in DEMUCS_TH
    ] + [
        ("hf_file", DEMUCS_HF_MIRROR, "htdemucs_ft.yaml",
         "torch/hub/checkpoints/htdemucs_ft.yaml"),
        ("url_file", W2V2_ASR_URL, "torch/hub/checkpoints/wav2vec2_fairseq_base_ls960_asr_ls960.pth"),
        ("silero", SILERO_TARBALL, "torch/hub/snakers4_silero-vad_master"),
    ],
    "mt": [
        ("hf_files", "Helsinki-NLP/opus-mt-en-zh", OPUS_MT_FILES, "hf/opus-mt-en-zh"),
    ],
}


# ---------------------------------------------------------------- 下载工具

def _get(url: str, timeout: int = 60) -> urllib.request.urlopen:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout)


def _download(url: str, dest: Path, label: str = "") -> Path:
    """流式下载到 dest（先写 .part）。已存在且大小一致则跳过。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    part = dest.with_suffix(dest.suffix + ".part")
    t0, last = time.time(), 0.0
    with _get(url) as resp, open(part, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got = f.tell()
            if got - last >= 50 * 1024 * 1024 or (total and got >= total):
                dt = time.time() - t0
                print(f"    [{label}] {got/1e6:7.0f} MB"
                      f"{f' / {total/1e6:.0f}' if total else ''}"
                      f"  {got/dt/1e6:.1f} MB/s", flush=True)
                last = got
    part.replace(dest)
    return dest


def ms_file_list(repo: str) -> dict[str, int]:
    url = f"https://modelscope.cn/api/v1/models/{repo}/repo/files?Recursive=true"
    with _get(url) as resp:
        d = json.load(resp)
    files = (d.get("Data") or {}).get("Files") or []
    return {f["Path"]: (f.get("Size") or 0)
            for f in files if f.get("Type") == "blob"}


def ms_url(repo: str, path: str) -> str:
    return f"https://modelscope.cn/models/{repo}/resolve/master/{urllib.request.quote(path)}"


def hf_url(repo: str, path: str) -> str:
    return f"https://hf-mirror.com/{repo}/resolve/main/{urllib.request.quote(path)}"


# ---------------------------------------------------------------- 动作实现

def _run_action(action: tuple) -> list[str]:
    kind = action[0]
    done: list[str] = []

    if kind == "ms_repo":  # 魔搭整仓 → 平铺目录
        _, repo, dest = action
        listing = ms_file_list(repo)
        for path, size in sorted(listing.items()):
            if size == 0 and path.split("/")[-1].startswith("."):
                continue
            p = _download(ms_url(repo, path), MODELS_DIR / dest / path, path.split("/")[-1])
            done.append(str(p))

    elif kind == "ms_files":  # 魔搭指定文件 → 目录
        _, repo, files, dest = action
        for path in files:
            p = _download(ms_url(repo, path), MODELS_DIR / dest / path, path.split("/")[-1])
            done.append(str(p))

    elif kind == "hf_cache":  # 魔搭文件 → HF 离线缓存布局（refs/main + snapshots）
        _, repo_id, ms_repo, files = action
        org, name = repo_id.split("/")
        base = HF_HUB_DIR / f"models--{org}--{name}"
        rev = "modelscope"
        snap = base / "snapshots" / rev
        (base / "refs").mkdir(parents=True, exist_ok=True)
        (base / "refs" / "main").write_text(rev, encoding="ascii")
        for path in files:
            p = _download(ms_url(ms_repo, path), snap / path, path.split("/")[-1])
            done.append(str(p))

    elif kind == "hf_file":  # hf-mirror 单文件（魔搭无搬运的小文件）
        _, repo, remote, local_rel = action
        p = _download(hf_url(repo, remote), MODELS_DIR / local_rel, remote.split("/")[-1])
        done.append(str(p))

    elif kind == "hf_files":  # hf-mirror 指定文件 → 目录
        _, repo, files, dest = action
        for path in files:
            p = _download(hf_url(repo, path), MODELS_DIR / dest / path, path.split("/")[-1])
            done.append(str(p))

    elif kind == "url_file":  # 任意 URL → models/ 下相对路径
        _, url, local_rel = action
        name = local_rel.split("/")[-1]
        p = _download(url, MODELS_DIR / local_rel, name)
        done.append(str(p))

    elif kind == "silero":  # github tarball → 解压成 torch.hub 缓存目录名
        _, url, dest_rel = action
        dest = MODELS_DIR / dest_rel
        if dest.exists():
            done.append(f"{dest} (已存在)")
            return done
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with _get(url) as resp:
            data = io.BytesIO(resp.read())
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.parent / f".silero_extract_{os.getpid()}"
        if tmp.exists():
            shutil.rmtree(tmp)
        with tarfile.open(fileobj=data, mode="r:gz") as tf:
            tf.extractall(tmp)
        extracted = tmp / "silero-vad-master"
        extracted.replace(dest)
        shutil.rmtree(tmp, ignore_errors=True)
        done.append(str(dest))

    else:
        raise ValueError(f"未知动作类型: {kind}")
    return done


def fetch(name: str) -> None:
    print(f"[fetch] {name}: {DESCRIPTIONS.get(name, '')}", flush=True)
    for action in TARGETS[name]:
        for path in _run_action(action):
            print(f"  [ok] {path}", flush=True)
    if name == "whisper":
        # gpustack/faster-whisper-large-v3 的 config.json 缺 num_mel_bins，
        # faster-whisper 会按 80 mel 算特征导致 (5,80,3000) vs (5,128,3000) 报错；
        # 官方 Systran 仓库该键为 128，这里补齐（2026-10-19 实测踩坑）
        cfg = (MODELS_DIR / "hf" / "hub" / "models--Systran--faster-whisper-large-v3"
               / "snapshots" / "modelscope" / "config.json")
        if cfg.exists():
            d = json.loads(cfg.read_text(encoding="utf-8"))
            if d.get("num_mel_bins") != 128:
                d["num_mel_bins"] = 128
                cfg.write_text(json.dumps(d, indent=2), encoding="utf-8")
                print("  [patch] config.json 补 num_mel_bins=128")
    print(f"[done] {name}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="下载共享模型权重")
    ap.add_argument("names", nargs="*", help="要下载的模型名，留空表示全部")
    ap.add_argument("--list", action="store_true", help="列出可用模型名")
    args = ap.parse_args()

    if args.list:
        for name, actions in TARGETS.items():
            print(f"  {name:14s} {len(actions):2d} 个动作  {DESCRIPTIONS.get(name, '')}")
        return 0

    names = args.names or list(TARGETS)
    unknown = [n for n in names if n not in TARGETS]
    if unknown:
        raise SystemExit(f"未知模型名: {', '.join(unknown)}（用 --list 查看）")

    for name in names:
        try:
            fetch(name)
        except Exception as exc:  # noqa: BLE001 - 逐个报告，不中断其余下载
            print(f"[fail] {name}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
