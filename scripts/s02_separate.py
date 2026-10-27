"""阶段2：对白 / 背景分离（Demucs）。

产物:
    02_stems/vocals.wav      对白（人声）
    02_stems/no_vocals.wav   背景（音乐 / 环境 / 音效）

Demucs 模型原生采样率为 44.1k，落盘时统一重采样到 48k（见 SPEC 第 6 节音频约定）。

用法:
    python scripts/s02_separate.py projects/20261008_demo_clip [--force]
"""
from __future__ import annotations

import argparse
import os
import tempfile
import wave
from pathlib import Path

from lib import audio, envs
from lib.config import load_project
from lib.state import run_stage

STAGE = "s02_separate"
INPUT_REL = "01_audio/audio_master.wav"
VOCALS_REL = "02_stems/vocals.wav"
BACKGROUND_REL = "02_stems/no_vocals.wav"

# demucs 权重随包分发；通过 envs.torch_home 优先走 ASCII junction，
# 无缓存时维持默认 ~/.cache/torch，行为不变
_torch_home = envs.torch_home(Path(__file__).resolve().parents[1])
if _torch_home:
    os.environ.setdefault("TORCH_HOME", _torch_home)


def _speech_activity(wav: Path) -> float:
    """粗测语音活动占比（高于峰值-35dB 且持续 ≥0.3s 的时间比例）。需要 numpy。"""
    import wave

    import numpy as np

    with wave.open(str(wav), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        data = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32)
        if w.getnchannels() == 2:
            data = data.reshape(-1, 2).mean(axis=1)
    win = int(0.02 * sr)
    frames = len(data) // win
    if not frames:
        return 0.0
    rms = np.sqrt((data[: frames * win].reshape(frames, win) ** 2).mean(axis=1))
    db = 20 * np.log10(rms / 32768.0 + 1e-9)
    return float((db > db.max() - 35).sum()) * 0.02 / (n / sr)


def check_separation(master: Path, vocals: Path) -> bool:
    """分离质量自检：master 声音活动明显而 vocals 几乎为空 → 分离失败。

    实测踩过：讲解+吉他混合紧的乐句视频，htdemucs_ft 把人声几乎全分到背景轨
    （master 活动 96% vs vocals 1%），下游 ASR 只剩一句 0.3s，成片没有配音。
    返回 True 表示需要回退。
    """
    try:
        mas = _speech_activity(master)
        voc = _speech_activity(vocals)
    except Exception as exc:  # noqa: BLE001 - 自检失败不该中断流程
        print(f"[separate] 活动检测失败（忽略自检）：{type(exc).__name__}: {exc}")
        return False
    if mas > 0.3 and voc < 0.15 * mas:
        print(
            f"[separate] 警告：人声轨几乎为空（master 活动 {mas:.0%} vs vocals {voc:.0%}），"
            "Demucs 可能把语音错分到了背景。回退：人声轨改用原始混音，"
            "配音将与原声重叠（混音闪避会压制原声）。"
        )
        return True
    return False


def separate_stems(master: Path, raw_dir: Path, model_name: str, device: str,
                   chunk_seconds: float = 300.0) -> tuple[Path, Path]:
    """用 Demucs 分出人声与背景，写进 raw_dir（保持模型原生采样率）。

    长音频必须分块：apply_model 会把 4 条 stems 一次性物化在内存里，
    44 分钟片源峰值要 4×3.8GB（2026-10-22 实测 44.5min 纪录片直接
    DefaultCPUAllocator OOM）。按 chunk_seconds 切块逐块推理、逐块写盘，
    内存占用与片长无关。块边界不重叠——htdemucs 内部本身就是
    split=True/overlap=0.25 的分块推理，边界效应只在几十毫秒级。
    """
    import torch
    from demucs.apply import apply_model
    from demucs.audio import AudioFile, save_audio
    from demucs.pretrained import get_model

    model = get_model(model_name)
    model.to(device)
    model.eval()

    wav = AudioFile(str(master)).read(
        streams=0, samplerate=model.samplerate, channels=model.audio_channels
    )
    names = list(model.sources)
    vi = names.index("vocals")
    bg_idx = [i for i, n in enumerate(names) if n != "vocals"]
    sr = model.samplerate
    total_samples = wav.shape[1]

    vocals_raw = raw_dir / "vocals.wav"
    background_raw = raw_dir / "no_vocals.wav"

    if total_samples <= chunk_seconds * sr:
        # 短音频走原路径（单次推理，向量化更省时）
        ref = wav.mean(0)
        x = (wav - ref.mean()) / (ref.std() + 1e-8)
        with torch.no_grad():
            stems = apply_model(
                model, x[None], device=device, shifts=1, split=True, overlap=0.25, progress=True
            )[0]
        stems = stems * ref.std() + ref.mean()
        save_audio(stems[vi], vocals_raw, samplerate=sr)
        save_audio(stems[bg_idx].sum(0), background_raw, samplerate=sr)
        return vocals_raw, background_raw

    # 长音频：逐块推理 + 逐块写盘
    step = int(chunk_seconds * sr)
    n_chunks = (total_samples + step - 1) // step
    print(f"[separate] 长音频 {total_samples / sr / 60:.1f}min → 分 {n_chunks} 块"
          f"（{chunk_seconds:.0f}s/块）分块推理，避免内存峰值")
    with wave.open(str(vocals_raw), "wb") as vf, wave.open(str(background_raw), "wb") as bf:
        for f in (vf, bf):
            f.setnchannels(model.audio_channels)
            f.setsampwidth(2)
            f.setframerate(sr)
        for ci, start in enumerate(range(0, total_samples, step), 1):
            seg = wav[:, start:start + step]
            ref = seg.mean(0)
            x = (seg - ref.mean()) / (ref.std() + 1e-8)
            with torch.no_grad():
                stems = apply_model(
                    model, x[None], device=device, shifts=1, split=True, overlap=0.25, progress=False
                )[0]
            stems = stems * ref.std() + ref.mean()
            voc = stems[vi]
            bg = stems[bg_idx].sum(0)
            for f, t in ((vf, voc), (bf, bg)):
                pcm = (t.clamp(-1.0, 1.0).transpose(0, 1).cpu().numpy() * 32767.0)
                f.writeframes(pcm.astype("<i2").tobytes())
            if ci % 3 == 0 or ci == n_chunks:
                print(f"[separate] 进度 {ci}/{n_chunks} 块")
            del stems, voc, bg, seg, x
    return vocals_raw, background_raw


def main() -> None:
    ap = argparse.ArgumentParser(description="阶段2：对白/背景分离")
    ap.add_argument("project", help="项目目录")
    ap.add_argument("--force", action="store_true", help="强制重跑")
    ap.add_argument("--seg", help="兼容 pipeline --seg 透传，本阶段忽略")
    args = ap.parse_args()

    envs.ensure_env("env_sep", "demucs")
    audio.use_bundled_ffmpeg()

    # demucs 的 get_model 会先试 HuggingFace hub，而本机直连不通、会白等超时。
    # 置为离线可让它立即回退到本地缓存 / AWS。
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

    project_dir = Path(args.project).resolve()
    project = load_project(project_dir)
    cfg = project["config"]
    from lib import gpu as gpu_mod

    cfg["separation"]["device"] = gpu_mod.effective_device(
        cfg["separation"]["device"], need_gb=4.0, stage="s02 分离"
    )
    separation = cfg["separation"]
    out_sr = cfg["audio"]["master_sample_rate"]

    master = project_dir / INPUT_REL
    if not master.exists():
        raise SystemExit(f"缺少 {INPUT_REL}，请先运行 s01_extract_audio")

    stems_dir = project_dir / "02_stems"
    work_dir = project_dir / "work" / "demucs"

    def produce() -> list[str]:
        stems_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run_", dir=work_dir) as tmp:
            vocals_raw, background_raw = separate_stems(
                master, Path(tmp), separation["model"], separation["device"],
                chunk_seconds=float(separation.get("chunk_seconds") or 300.0),
            )
            if check_separation(master, vocals_raw):
                # 分离失败回退：人声轨直接用原始混音（保证 ASR/配音有内容），
                # 背景轨保留 Demucs 的 no_vocals（混音时闪避压制原声）
                audio.extract_audio(master, stems_dir / "vocals.wav", out_sr, 2)
            else:
                audio.extract_audio(vocals_raw, stems_dir / "vocals.wav", out_sr, 2)
            audio.extract_audio(background_raw, stems_dir / "no_vocals.wav", out_sr, 2)
        return [VOCALS_REL, BACKGROUND_REL]

    ran = run_stage(
        project_dir, STAGE, [INPUT_REL], cfg, produce,
        config_keys=["audio", "separation"], force=args.force,
    )

    if ran or (stems_dir / "vocals.wav").exists():
        for rel in (VOCALS_REL, BACKGROUND_REL):
            print(f"{rel}: {audio.probe_duration(project_dir / rel):.3f}s")


if __name__ == "__main__":
    main()