"""阶段6：建立每角色音色档案（参考音频）。

产物:
    05_voices/profiles.json              音色档案索引
    05_voices/<SPK>/reference.wav        供 TTS 克隆用的参考音频
    05_voices/<SPK>/preview.wav          供人工试听（R1/R3 卡点）
    05_voices/<SPK>/parts/*.wav          拼参考音频用的原始片段

参考片段优先取 `03_asr/speakers.json` 里标注的 ref_segments（人工可改），
不够或未标注时自动挑该说话人最长的几段。总时长取 `tts.ref_seconds`。

纯 ffmpeg，不依赖任何 Python 环境。

用法:
    python scripts/s06_voice_profile.py projects/20261008_demo_clip [--force]
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from lib import audio
from lib.config import load_project
from lib.schemas import load_json, resolve_edited, save_json
from lib.state import run_stage

STAGE = "s06_voice_profile"
TRANSCRIPT_REL = "03_asr/segments.json"
SPEAKERS_REL = "03_asr/speakers.json"
PROFILES_REL = "05_voices/profiles.json"
VOCALS_REL = "02_stems/vocals_clean.wav"


class _Seg:
    """把 dict 段落包成带 duration / confidence 的小对象。"""

    def __init__(self, data: dict):
        self.id = data["id"]
        self.start = float(data["start"])
        self.end = float(data["end"])
        self.confidence = float(data.get("confidence") or 0.5)

    @property
    def duration(self) -> float:
        return self.end - self.start


def _quality_check(seg: _Seg, vocals: Path, no_vocals: Path | None,
                   sample_rate: int, min_conf: float, max_bg: float) -> dict:
    """单段参考音频质量预检（H-4）：ASR 置信度 + BGM 能量占比。

    BGM 占比 = no_vocals 干声 RMS / vocals 干声 RMS（同一窗口，线性比）。
    用原始 stems 而非 vocals_clean 度量——那是「场景 BGM 多重」的客观性质，
    与降噪是否开启无关。返回 {"qualified", "confidence", "bg_ratio"}。
    """
    bg_ratio: float | None = None
    if no_vocals is not None:
        try:
            voc_db = audio.segment_rms_db(str(vocals), seg.start, seg.duration)
            bg_db = audio.segment_rms_db(str(no_vocals), seg.start, seg.duration)
            if voc_db is not None and bg_db is not None:
                bg_ratio = 10.0 ** ((bg_db - voc_db) / 20.0)
        except Exception:  # noqa: BLE001 - 度量失败不阻塞，只少一项指标
            bg_ratio = None
    qualified = seg.confidence >= min_conf and (
        bg_ratio is None or bg_ratio <= max_bg
    )
    return {
        "qualified": bool(qualified),
        "confidence": round(seg.confidence, 3),
        "bg_ratio": round(bg_ratio, 2) if bg_ratio is not None else None,
    }


def _spread_order(pool: list, rank, bins: int) -> list:
    """把候选段按时间轴分成 `bins` 段后轮流取，段内按质量排。

    2026-10-22 实测动机：bcs_12min 的 SPK_04 占 63% 的段，参考音频全落在
    277~565s（该人 127~715s 的中段），片尾 150s 一个参考都没有；相似度只有
    0.13，而其余 5 人 0.21~0.31。参考音频应覆盖同一人在不同场景的嗓音。

    bins 取「预计要几段才凑够 ref_seconds」（`_bins_for` 算），这样在预算
    只够 2 段时也能保证「最前 + 最后」各取一段，而不是前两段就吃满预算。
    """
    ordered = sorted(pool, key=lambda s: (s.start, s.end))
    if bins <= 1 or len(ordered) < bins * 2:
        return sorted(pool, key=rank)
    # 按段序等分（用取整边界，避免 12 段 / 5 段这类除不尽时出现空桶）
    parts = [ordered[(i * len(ordered)) // bins: ((i + 1) * len(ordered)) // bins]
             for i in range(bins)]
    parts = [sorted(p, key=rank) for p in parts if p]
    out: list = []
    for i in range(max(len(p) for p in parts)):
        for part in parts:
            if i < len(part):
                out.append(part[i])
    return out


def _bins_for(pool: list, ref_seconds: float) -> int:
    """预计需要几段才凑够 ref_seconds（用中位段长估，夹到 1~6）。"""
    if not pool:
        return 1
    durs = sorted(s.duration for s in pool)
    med = durs[len(durs) // 2] or 1.0
    return max(1, min(6, int(round(ref_seconds / max(med, 1.0)))))


def choose_segments(segments, ref_ids, ref_seconds, min_seconds, quality: dict | None = None,
                    spread: bool = False):
    """挑选参考片段：先人工指定的，再按时长补齐，直到凑够 ref_seconds。

    有质量预检结果时（quality: {id: check dict}），自动补齐按
    「合格优先 → 置信度优先 → 时长优先」排序；人工指定的段始终尊重
    （不合格只警告不替换——人工标注是权威）。

    spread=True 时自动补齐改为「跨时间轴分散抽样」（见 _spread_order），
    避免同一说话人的参考音频全部落在同一个场景里。
    """
    by_id = {s.id: s for s in segments}
    chosen = []
    seen = set()

    def _manual(seg: _Seg, check: dict | None) -> None:
        chosen.append(seg)
        seen.add(seg.id)
        if check and not check["qualified"]:
            why = []
            if check["confidence"] < 0.30:
                why.append(f"置信度 {check['confidence']:.2f} 偏低")
            if check["bg_ratio"] is not None and check["bg_ratio"] > 2.5:
                why.append(f"BGM 占比 {check['bg_ratio']:.1f}× 偏高")
            print(f"[voice] 警告：人工指定参考段 {seg.id} 质量欠佳（{'；'.join(why)}），"
                  "GSV 音色锚点可能受影响，建议试听 preview.wav 确认")

    for sid in ref_ids:
        seg = by_id.get(sid)
        if seg and seg.duration >= min_seconds and sid not in seen:
            _manual(seg, quality.get(sid) if quality else None)

    total = sum(s.duration for s in chosen)
    if total < ref_seconds:
        pool = [s for s in segments if s.id not in seen and s.duration >= min_seconds]
        if quality:
            rank = lambda s: (  # noqa: E731 - 局部排序键
                not quality.get(s.id, {}).get("qualified", True),
                -quality.get(s.id, {}).get("confidence", 0.5),
                -s.duration,
            )
        else:
            rank = lambda s: (-s.duration,)  # noqa: E731
        ordered_pool = (_spread_order(pool, rank, _bins_for(pool, ref_seconds))
                        if spread else sorted(pool, key=rank))
        for seg in ordered_pool:
            if total >= ref_seconds:
                break
            chosen.append(seg)
            seen.add(seg.id)
            total += seg.duration

    return chosen


def main() -> None:
    ap = argparse.ArgumentParser(description="阶段6：音色与参考音频准备")
    ap.add_argument("project", help="项目目录")
    ap.add_argument("--force", action="store_true", help="强制重跑")
    ap.add_argument("--seg", action="append", help="（本阶段忽略，仅为与调度器参数对齐）")
    args = ap.parse_args()

    project_dir = Path(args.project).resolve()
    project = load_project(project_dir)
    cfg = project["config"]

    transcript_path = resolve_edited(project_dir, TRANSCRIPT_REL)
    if not transcript_path.exists():
        raise SystemExit(f"缺少 {TRANSCRIPT_REL}，请先运行 s04_asr")
    transcript = load_json(transcript_path)
    segments = transcript.get("segments", [])
    if not segments:
        raise SystemExit("没有可用段落")

    speakers_path = resolve_edited(project_dir, SPEAKERS_REL)
    speakers_meta = load_json(speakers_path) if speakers_path.exists() else {}

    vocals = project_dir / VOCALS_REL
    if not vocals.exists():
        raise SystemExit(f"缺少 {VOCALS_REL}，请先运行 s03_denoise")

    sample_rate = int(cfg["tts"]["sample_rate"])
    ref_seconds = float(cfg["tts"]["ref_seconds"])
    min_seconds = float(cfg["tts"]["min_ref_seconds"])
    min_conf = float(cfg["tts"].get("ref_min_confidence") or 0.0)
    max_bg = float(cfg["tts"].get("ref_max_bg_ratio") or 0.0)
    spread = bool(cfg["tts"].get("ref_spread", True))
    short_warn = float(cfg["tts"].get("ref_short_warn_seconds") or 0.0)

    no_vocals = project_dir / "02_stems/no_vocals.wav"
    bg_ref = no_vocals if (min_conf > 0 and max_bg > 0 and no_vocals.exists()) else None

    # 按说话人分组
    by_speaker: dict[str, list] = {}
    for seg in segments:
        by_speaker.setdefault(seg.get("speaker") or "SPK_01", []).append(_Seg(seg))

    voices_dir = project_dir / "05_voices"

    def produce() -> list[str]:
        profiles: dict[str, dict] = {}
        outputs = [PROFILES_REL]

        for speaker in sorted(by_speaker):
            meta = speakers_meta.get(speaker, {})
            ref_ids = list(meta.get("ref_segments", []))
            quality = (
                {s.id: _quality_check(s, vocals, bg_ref, sample_rate, min_conf, max_bg)
                 for s in by_speaker[speaker]}
                if min_conf > 0 or max_bg > 0 else {}
            )
            chosen = choose_segments(by_speaker[speaker], ref_ids, ref_seconds,
                                     min_seconds, quality or None, spread=spread)

            spk_dir = voices_dir / speaker
            parts_dir = spk_dir / "parts"
            if spk_dir.exists():
                shutil.rmtree(spk_dir)
            parts_dir.mkdir(parents=True, exist_ok=True)

            if not chosen:
                print(f"[voice] {speaker}: 没有足够长的片段（>={min_seconds}s），跳过参考音频")
                profiles[speaker] = {
                    "character": meta.get("character", ""),
                    "reference": None,
                    "preview": None,
                    "ref_segments": [],
                    "duration": 0.0,
                }
                continue

            part_files = []
            used = 0.0
            for seg in chosen:
                remaining = ref_seconds - used
                if remaining <= 0.05:
                    break
                dur = min(seg.duration, remaining)
                if part_files and dur < min_seconds:
                    break
                part = parts_dir / f"{seg.id}.wav"
                audio.cut(vocals, part, seg.start, seg.start + dur, sample_rate, 1)
                part_files.append(part)
                used += dur

            reference = spk_dir / "reference.wav"
            audio.concat(part_files, reference, sample_rate, 1)
            preview = spk_dir / "preview.wav"
            shutil.copyfile(reference, preview)

            duration = audio.probe_duration(reference)
            checks = [quality.get(s.id) for s in chosen if quality.get(s.id)]
            short = bool(short_warn and duration < short_warn)
            profiles[speaker] = {
                "character": meta.get("character", ""),
                "reference": f"05_voices/{speaker}/reference.wav",
                "preview": f"05_voices/{speaker}/preview.wav",
                "ref_segments": [s.id for s in chosen],
                "duration": round(duration, 3),
                "ref_short": short,
                "ref_quality": {
                    "min_confidence": min((c["confidence"] for c in checks), default=None),
                    "max_bg_ratio": max((c["bg_ratio"] for c in checks
                                         if c["bg_ratio"] is not None), default=None),
                    "all_qualified": all(c["qualified"] for c in checks) if checks else None,
                },
            }
            outputs += [
                f"05_voices/{speaker}/reference.wav",
                f"05_voices/{speaker}/preview.wav",
            ]
            print(f"[voice] {speaker}: 参考音频 {duration:.2f}s（{len(chosen)} 段，"
                  f"跨 {len({round(s.start / 60) for s in chosen})} 分钟档）")
            if short:
                print(f"[voice] ⚠ {speaker}: 参考音频仅 {duration:.2f}s（< {short_warn:.0f}s），"
                      "音色锚点不稳，克隆相似度可能明显偏低（GSV 要求 3~10s）；"
                      "该说话人素材不足，或人工在 03_asr/speakers.json 指定更长的 ref_segments")

        save_json(project_dir / PROFILES_REL, profiles)
        return outputs

    input_rels = [
        str(transcript_path.relative_to(project_dir)).replace("\\", "/"),
        VOCALS_REL,
    ]
    if speakers_path.exists():
        input_rels.append(str(speakers_path.relative_to(project_dir)).replace("\\", "/"))
    if bg_ref is not None:
        input_rels.append("02_stems/no_vocals.wav")

    ran = run_stage(
        project_dir, STAGE, input_rels, cfg, produce,
        config_keys=["tts", "audio"], force=args.force,
    )

    if ran or (project_dir / PROFILES_REL).exists():
        print(f"提示：请试听 05_voices/*/preview.wav，确认同一说话人未被拆成多人（R1 卡点）")


if __name__ == "__main__":
    main()
