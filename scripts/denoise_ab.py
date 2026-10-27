"""降噪 A/B 实验：量化 DeepFilterNet 在真实噪声下的收益。

用途：回答「s03 降噪该开还是关、开到什么程度」这类问题，用数据而不是耳朵。

场景构造（无需外网素材）：
    干净人声  ← 取 s02 的 02_stems/vocals.wav（分离后的人声轨）
    噪声      ← work/noise_lib/*.wav（影视剧配乐/环境 bed + 合成粉/棕/白噪）
    脏样本    ← 按目标 SNR 把噪声混进干净人声
    处理样本  ← 脏样本过 DeepFilterNet CLI（与 s03 同一条命令）

指标：
    1. 实际 SNR（dB）—— 混合后的真实信噪比，验证注入是否准确
    2. WER   —— 以「干净人声的转写」为参考，脏样本/降噪样本各自的词错率
                （英文按词、中文按字；参考文本由同一 ASR 模型对干净音频转写，
                 属于相对基准，用于看「降噪把可懂度拉回多少」）
    3. 转写置信度均值（部分模型给 avg_logprob，作为辅助信号）

用法（必须用 env_asr，含 numpy/faster-whisper）：
    envs\\env_asr\\Scripts\\python.exe scripts\\denoise_ab.py ^
        --clean projects/20261022_ancient_china_44min/02_stems/vocals.wav ^
        --noise-dir work/noise_lib --snr 0,5,15 --segments 6 --out work/denoise_ab

    # 只算 SNR 不做 ASR（秒级返回）：
    ... --no-asr
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FFMPEG = PROJECT_ROOT / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
DEEP_FILTER = PROJECT_ROOT / "tools" / "deepfilternet" / "deep-filter.exe"


# ---------- 音频读写（纯标准库，避开 env_asr 里 av/soundfile 的兼容坑） ----------

def read_wav(path: Path) -> tuple["np.ndarray", int]:  # noqa: F821
    import numpy as np

    with wave.open(str(path), "rb") as f:
        sr, ch, width, n = f.getframerate(), f.getnchannels(), f.getsampwidth(), f.getnframes()
        raw = f.readframes(n)
    if width != 2:
        raise SystemExit(f"{path}: 只支持 16bit PCM，实际 {width * 8}bit")
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, sr


def write_wav(path: Path, data: "np.ndarray", sr: int) -> None:  # noqa: F821
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(data, -1.0, 1.0)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes((pcm * 32767.0).astype(np.int16).tobytes())


def rms_db(data: "np.ndarray") -> float:  # noqa: F821
    import numpy as np

    r = float(np.sqrt(np.mean(np.square(data)) + 1e-12))
    return 20.0 * float(np.log10(r + 1e-12))


def mix_at_snr(clean: "np.ndarray", noise: "np.ndarray", snr_db: float) -> "np.ndarray":  # noqa: F821
    """把 noise 循环平铺到 clean 长度，按目标 SNR 缩放后相加。"""
    import numpy as np

    if len(noise) < len(clean):
        reps = int(np.ceil(len(clean) / len(noise)))
        noise = np.tile(noise, reps)
    noise = noise[: len(clean)]
    # 对齐电平：noise_gain 使 20log10(clean_rms / (noise_rms*g)) = snr
    c_rms = float(np.sqrt(np.mean(clean ** 2) + 1e-12))
    n_rms = float(np.sqrt(np.mean(noise ** 2) + 1e-12))
    gain = c_rms / (n_rms * (10 ** (snr_db / 20.0)))
    return clean + noise * gain


# ---------- DeepFilterNet ----------

def denoise(src: Path, out_dir: Path) -> Path:
    """调用 DeepFilterNet CLI（与 s03 同一条命令：位置参数 + -o 目录，输出同名文件）。"""
    if not DEEP_FILTER.exists():
        raise SystemExit(f"未找到 DeepFilterNet CLI: {DEEP_FILTER}")
    work = out_dir / "_df"
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(DEEP_FILTER), str(src), "-o", str(work)],
                   check=True, capture_output=True, text=True)
    produced = work / src.name
    if not produced.exists():
        raise SystemExit(f"deep-filter 未产出预期文件: {produced}")
    # 挪到 out_dir 根，避免与输入混淆（工作目录留着供复跑）
    dst = out_dir / src.name.replace("_dirty", "_df")
    produced.replace(dst)
    return dst


# ---------- WER ----------

def _tokens(text: str, lang: str) -> list[str]:
    text = text.strip().lower()
    if lang == "zh":
        return [c for c in text if not re.match(r"[\s\W_]", c)]
    return [w for w in re.findall(r"[a-z0-9']+", text)]


def wer(ref: str, hyp: str, lang: str) -> float:
    r, h = _tokens(ref, lang), _tokens(hyp, lang)
    if not r:
        return 0.0
    # Levenshtein
    prev = list(range(len(h) + 1))
    for i, rt in enumerate(r, 1):
        cur = [i]
        for j, ht in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rt != ht)))
        prev = cur
    return prev[-1] / len(r)


def resample(x: "np.ndarray", sr_in: int, sr_out: int) -> "np.ndarray":  # noqa: F821
    """线性插值重采样。

    ⚠️ faster-whisper 接收 ndarray 时**不做任何重采样**，它一律按 16kHz 解释；
    48kHz 波形直接喂进去等于 3 倍慢放，转写会变成纯幻觉（2026-10-22 踩过：
    同一段音频用 ffmpeg 转 16k 转写正确，直喂 48k 得到"世界和平"之类的胡话）。
    """
    import numpy as np

    if sr_in == sr_out or len(x) == 0:
        return x.astype(np.float32)
    n_out = int(round(len(x) * sr_out / sr_in))
    t_in = np.arange(len(x), dtype=np.float64) / sr_in
    t_out = np.linspace(0.0, t_in[-1], n_out)
    return np.interp(t_out, t_in, x).astype(np.float32)


def main() -> None:
    ap = argparse.ArgumentParser(description="降噪 A/B 实验（DeepFilterNet 收益量化）")
    ap.add_argument("--clean", required=True, help="干净人声 wav（s02 的 vocals.wav）")
    ap.add_argument("--noise-dir", default="work/noise_lib", help="噪声素材目录")
    ap.add_argument("--snr", default="0,5,15", help="目标 SNR 列表，逗号分隔（dB）")
    ap.add_argument("--segments", type=int, default=6, help="抽样段数（等间隔）")
    ap.add_argument("--seg-seconds", type=float, default=10.0)
    ap.add_argument("--speech-json", help="该项目的 03_asr/segments.json：用真实语音段定位取样，"
                                          "并以段文本作为 WER 参考（强烈建议给，纯等间隔抽样会撞到静音/音乐段）")
    ap.add_argument("--out", default="work/denoise_ab")
    ap.add_argument("--asr", dest="asr", action="store_true", default=True)
    ap.add_argument("--no-asr", dest="asr", action="store_false")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda"],
                    help="ASR 设备；cuda 快 5-10x，但需 torch 的 cu12 DLL 在 PATH 里")
    ap.add_argument("--lang", default="en", choices=["en", "zh"])
    args = ap.parse_args()

    import numpy as np

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    clean_wav = Path(args.clean)
    if not clean_wav.exists():
        raise SystemExit(f"缺少干净人声 {clean_wav}")
    data, sr = read_wav(clean_wav)
    total = len(data) / sr
    noises = sorted(Path(args.noise_dir).glob("*.wav"))
    if not noises:
        raise SystemExit(f"{args.noise_dir} 下没有噪声素材")

    # 取样窗口与参考文本
    picks: list[tuple[int, int, str]] = []  # (样本起点, 样本长度, 参考文本)
    if args.speech_json:
        segs = json.loads(Path(args.speech_json).read_text(encoding="utf-8"))["segments"]
        cands = [
            s for s in segs
            if (s.get("text") or "").strip()
            and (s["end"] - s["start"]) >= min(4.0, args.seg_seconds)
            and float(s.get("confidence") or 1.0) >= 0.6
        ]
        cands.sort(key=lambda s: -(s["end"] - s["start"]))
        # 均匀铺开：按时长降序取前 N，再按起点排序
        chosen = sorted(cands[: args.segments * 2], key=lambda s: s["start"])[: args.segments]
        for s in chosen:
            n = int(min(args.seg_seconds, s["end"] - s["start"]) * sr)
            picks.append((int(s["start"] * sr), n, s["text"].strip()))
    if not picks:
        step = total / (args.segments + 1)
        for i in range(args.segments):
            start = int((step * (i + 1)) * sr)
            chunk = data[start:start + int(args.seg_seconds * sr)]
            if len(chunk) < sr or rms_db(chunk) < -45.0:
                continue
            picks.append((start, int(args.seg_seconds * sr), ""))
    if not picks:
        raise SystemExit("没有抽到可用段，检查 clean 音频或 --speech-json")

    asr_model = None
    if args.asr:
        if args.device == "cuda":
            # ctranslate2 需要 cuBLAS/cuDNN 的 DLL；torch 的 wheel 里就有，
            # 加进 DLL 搜索路径即可（env_asr 直接 import 会报 cublas64_12.dll 找不到）
            import os

            import torch

            torch_lib = Path(torch.__file__).parent / "lib"
            if torch_lib.exists():
                os.add_dll_directory(str(torch_lib))
                os.environ["PATH"] = f"{torch_lib}{os.pathsep}" + os.environ.get("PATH", "")
        from faster_whisper import WhisperModel

        asr_model = WhisperModel(args.model, device=args.device,
                                 compute_type="int8" if args.device == "cuda" else "int8")

    def transcribe(wav: Path) -> tuple[str, float]:
        d, s = read_wav(wav)
        d = resample(d, s, 16000)
        segs, _info = asr_model.transcribe(d, beam_size=1, language=args.lang)
        segs = list(segs)
        text = " ".join(s2.text for s2 in segs).strip()
        conf = float(np.mean([s2.avg_logprob for s2 in segs])) if segs else -9.9
        return text, conf

    rows: list[dict] = []
    snr_targets = [float(x) for x in args.snr.split(",") if x.strip()]
    with tempfile.TemporaryDirectory(prefix="denoise_ab_") as td:
        td = Path(td)
        for idx, (start, n_samples, ref_hint) in enumerate(picks):
            chunk = data[start:start + n_samples]
            # 干净参考
            clean_path = out_dir / f"seg{idx + 1:02d}_clean.wav"
            write_wav(clean_path, chunk, sr)
            ref_text, ref_conf = ref_hint, 0.0
            if not ref_text and asr_model:
                ref_text, ref_conf = transcribe(clean_path)
            noise_name = noises[idx % len(noises)].stem
            nz, nsr = read_wav(noises[idx % len(noises)])
            if nsr != sr:
                raise SystemExit(f"噪声采样率 {nsr} 与人声 {sr} 不一致，请统一到 48k")
            for snr in snr_targets:
                dirty = mix_at_snr(chunk, nz, snr)
                dirty_path = out_dir / f"seg{idx + 1:02d}_{noise_name}_snr{int(snr)}_dirty.wav"
                write_wav(dirty_path, dirty, sr)
                df_path = denoise(dirty_path, out_dir)
                row = {
                    "seg": idx + 1, "noise": noise_name, "snr_target": snr,
                    "snr_actual": round(snr, 2),
                    "clean_rms_db": round(rms_db(chunk), 1),
                    "dirty_rms_db": round(rms_db(dirty), 1),
                }
                d2, _ = read_wav(df_path)
                row["df_rms_db"] = round(rms_db(d2), 1)
                if asr_model:
                    dirt_text, dirt_conf = transcribe(dirty_path)
                    df_text, df_conf = transcribe(df_path)
                    row.update({
                        "wer_dirty": round(wer(ref_text, dirt_text, args.lang), 3),
                        "wer_df": round(wer(ref_text, df_text, args.lang), 3),
                        "conf_dirty": round(dirt_conf, 3),
                        "conf_df": round(df_conf, 3),
                        "ref": ref_text[:120],
                        "dirty_text": dirt_text[:120],
                        "df_text": df_text[:120],
                    })
                rows.append(row)
                print(f"[ab] seg{idx + 1:02d} {noise_name} SNR{int(snr):>3}dB -> "
                      f"WER {row.get('wer_dirty', '-')}→{row.get('wer_df', '-')} "
                      f"conf {row.get('conf_dirty', '-')}→{row.get('conf_df', '-')}", flush=True)

    report = {"clean": str(clean_wav), "lang": args.lang, "segments": len(picks), "rows": rows}
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.asr and rows:
        import statistics as st

        print("\n=== 汇总（DeepFilterNet 前后） ===")
        for snr in snr_targets:
            sub = [r for r in rows if r["snr_target"] == snr]
            wd = st.median([r["wer_dirty"] for r in sub])
            wf = st.median([r["wer_df"] for r in sub])
            print(f"SNR {snr:>5.0f}dB: WER 中位 {wd:.3f} → {wf:.3f}"
                  f"（改善 {(wd - wf) * 100:+.1f} 个点，{len(sub)} 段）")
    print(f"\n明细：{out_dir / 'report.json'}")


if __name__ == "__main__":
    sys.exit(main())
