"""阶段8：时间轴微调（变速不变调）。

产物:
    07_align/align_report.json           对齐报告
    07_align/<SPK>/<seg_XXXX>.wav        变速后的配音

对每段：按 SPEC 5.1 的预算算变速比 `actual_dur / budget`，
只加速不放慢（上限 `max_speed`），用 rubberband 变速不变调。
超出上限（配音偏长）记 `overrun` 交人工复审（R4）；
偏短保持合成语速，尾部留白自然成为与下一句的间隔。

变速比在容差内时不做处理，只统一转码到目标采样率，保证下游格式一致。

极端段救援承接（2026-10-21）：manifest 带 `fit_dur`/`rescue` 的段不贴
预算，贴 s07 定的窗口/叠话目标——`rescue="gap"` 按 `pre_borrow` 前移
起点后铺轨；`rescue="overlap"` 跳过碰撞消解（叠话是故意的），并对伸进
下一句的部分做 ≤0.8s 尾淡出（下一句清晰、本句自然收尾）。报告记
`rescue`/`overlap` 字段，qc 放行计划内叠话，WebUI 语速列标注救援方式。

用法:
    python scripts/s08_align.py projects/20261008_demo_clip [--force] [--seg seg_0001]
"""
from __future__ import annotations

import argparse
import statistics
from pathlib import Path

from lib import audio
from lib.budget import plan_speed
from lib.config import load_project
from lib.schemas import load_json, resolve_edited, save_json
from lib.state import run_stage

STAGE = "s08_align"
MANIFEST_REL = "06_tts/manifest.json"
TRANSLATION_REL = "04_translate/translation.json"
SEGMENTS_REL = "03_asr/segments.json"
REPORT_REL = "07_align/align_report.json"
VOCALS_REL = "02_stems/vocals_clean.wav"

SPEED_TOLERANCE = 0.005
COLLISION_TOLERANCE = 0.15   # 配音尾部压到下一句起点 0.15s 内可接受（听感无碍）
COLLISION_SPEED_CAP = 1.30   # 为消除碰撞允许的额外变速上限（仅短句略快，自然）
LOUDNESS_GAIN_CAP = 8.0      # 逐段响度贴回的增益上限（dB），防过度放大暴露底噪
LOUDNESS_APPLY_MIN = 0.3     # 小于该增益不做二次编码（听感无差，省一遍转码）
GAP_FILL_MAX_RATIO = 0.80    # 配音 < 0.80×原声时长才触发留白填补（不算短不动作）
GAP_FILL_SPEED_FLOOR = 0.85  # 留白填补的放慢下限（最多慢 ~18%，再慢有电子感）


def main() -> None:
    ap = argparse.ArgumentParser(description="阶段8：时长对齐与变速")
    ap.add_argument("project", help="项目目录")
    ap.add_argument("--force", action="store_true", help="强制重跑")
    ap.add_argument("--seg", action="append", help="只对齐指定段落（可重复）")
    args = ap.parse_args()

    audio.use_bundled_ffmpeg()

    project_dir = Path(args.project).resolve()
    project = load_project(project_dir)
    cfg = project["config"]
    sample_rate = int(cfg["tts"]["sample_rate"])

    manifest_path = resolve_edited(project_dir, MANIFEST_REL)
    if not manifest_path.exists():
        raise SystemExit(f"缺少 {MANIFEST_REL}，请先运行 s07_tts")
    manifest = load_json(manifest_path)

    translation_path = resolve_edited(project_dir, TRANSLATION_REL)
    if not translation_path.exists():
        raise SystemExit(f"缺少 {TRANSLATION_REL}，请先运行 s05_translate")
    budgets = {
        it["id"]: float(it.get("duration_budget") or 0.0)
        for it in load_json(translation_path).get("segments", [])
    }

    segments_path = resolve_edited(project_dir, SEGMENTS_REL)
    base_durations: dict[str, float] = {}
    starts: dict[str, float] = {}
    ends: dict[str, float] = {}
    if segments_path.exists():
        for seg in load_json(segments_path).get("segments", []):
            base_durations[seg["id"]] = round(float(seg["end"]) - float(seg["start"]), 3)
            starts[seg["id"]] = round(float(seg["start"]), 3)
            ends[seg["id"]] = round(float(seg["end"]), 3)

    align_dir = project_dir / "07_align"
    report_path = project_dir / REPORT_REL

    selected = set(args.seg) if args.seg else None
    existing: dict[str, dict] = {}
    if selected is not None and report_path.exists():
        for entry in load_json(report_path).get("segments", []):
            existing[entry["id"]] = entry

    todo = [
        e for e in manifest.get("segments", [])
        if e.get("file") and e.get("status") not in ("failed", "empty")
        and (selected is None or e["id"] in selected)
    ]

    # 全时间轴邻接表：碰撞检测/叠话淡出都必须看**全部**段的真实邻接，
    # 不能只含本次待处理段——--seg 部分重跑时选集中的"下一句"不是真邻居，
    # 会误判碰撞、漏做叠话淡出（2026-10-21 实测踩坑）
    ordered_ids = sorted(starts, key=lambda s: starts[s])

    def resolve_collision(seg_id: str, speed: float, input_dur: float,
                          eff_start: float | None = None) -> float:
        """按当前速度铺轨会压到下一句起点 0.15s 以上时，在 1.30 上限内再提速。

        eff_start 为实际铺轨起点（救援段按 pre_borrow 前移过）——必须按真实
        起点算，否则前借的段会被误判碰撞、把救援成果又压回去。
        """
        if seg_id not in ordered_ids:
            return speed
        i = ordered_ids.index(seg_id)
        if i + 1 >= len(ordered_ids):
            return speed
        seg_start = eff_start if eff_start is not None else starts[seg_id]
        next_start = starts[ordered_ids[i + 1]]
        end_at_speed = seg_start + input_dur / speed
        if end_at_speed <= next_start + COLLISION_TOLERANCE:
            return speed
        room = max(next_start - 0.05 - seg_start, 0.2)
        harder = min(input_dur / room, COLLISION_SPEED_CAP)
        return max(speed, harder)

    def fill_pause(seg_id: str, speed: float, input_dur: float) -> float:
        """大留白填补：译文明显短于原声（铺轨 < 0.80×原声时长）且下一段起点
        之前有充足空隙时，适度放慢（下限 0.85，目标 0.92×原声时长）。

        「只加速不放慢」原则的窄例外：不为凑时长，而是填掉"一句秒说完 +
        数秒死寂"的突兀停顿——译文过短时 tail 留白比轻微慢速更伤听感。
        放慢前校验不越过下一段起点（留 0.10s 缓冲），绝不产生新碰撞。
        """
        if seg_id not in ordered_ids or seg_id not in base_durations:
            return speed
        if speed < 1.0 - SPEED_TOLERANCE:
            return speed  # 已因碰撞放慢过的极端情形不再动
        orig = base_durations[seg_id]
        final = input_dur / speed
        if final >= orig * GAP_FILL_MAX_RATIO or input_dur < 0.6:
            return speed  # 不算短，或太短（放慢意义不大且失真明显）
        target = orig * 0.92
        i = ordered_ids.index(seg_id)
        if i + 1 < len(ordered_ids):
            room = starts[ordered_ids[i + 1]] - 0.10 - starts[seg_id]
            if room <= input_dur:
                return speed  # 后面没有空隙，放慢会撞下一句
            target = min(target, room)
        slower = max(input_dur / target, GAP_FILL_SPEED_FLOOR)
        return min(slower, speed)  # speed=1.0 时只放慢

    def produce() -> list[str]:
        entries: list[dict] = []
        outputs = [REPORT_REL]
        vocals = project_dir / VOCALS_REL
        vocals_ok = vocals.exists()

        # 第一遍：变速产出 + 测量（配音 RMS / 原声段 RMS），供第二遍响度贴回
        measured: dict[str, tuple[float | None, float | None]] = {}
        for entry in todo:
            seg_id = entry["id"]
            speaker = entry.get("speaker") or "SPK_01"
            src = project_dir / entry["file"]
            if not src.exists():
                print(f"[align] {seg_id} 源文件缺失，跳过：{entry['file']}")
                continue

            input_dur = float(entry.get("actual_dur") or audio.probe_duration(src))
            budget = budgets.get(seg_id, input_dur)
            # 极端段救援元数据（s07 写入）：fit_dur=窗口/叠话目标，pre_borrow=
            # 起点前移量，rescue=gap|overlap。老 manifest 无这些字段时全走原逻辑
            rescue = entry.get("rescue")
            pre_borrow = float(entry.get("pre_borrow") or 0.0)
            fit = float(entry.get("fit_dur") or 0.0) or budget
            speed, flags = plan_speed(input_dur, fit, cfg)
            if rescue == "overlap":
                # 叠话救援：故意伸进下一句（事实对白常有抢话），不做碰撞消解
                # ——那会抵消救援；越界部分的可懂度由下面的尾淡出保证
                pass
            else:
                eff_start = starts[seg_id] - pre_borrow if seg_id in starts else None
                fixed = resolve_collision(seg_id, speed, input_dur, eff_start)
                if fixed > speed + SPEED_TOLERANCE:
                    speed = fixed
                    # 碰撞自动修复不算 overrun：人工无需复审，QC 按最终铺轨判定
                    flags = [f for f in flags if f != "overrun"]
                elif not rescue:
                    slower = fill_pause(seg_id, speed, input_dur)
                    if slower < speed - SPEED_TOLERANCE:
                        speed = slower
                        flags = flags + ["gap_fill"]

            out_rel = f"07_align/{speaker}/{seg_id}.wav"
            out_path = project_dir / out_rel
            if abs(speed - 1.0) < SPEED_TOLERANCE:
                audio.extract_audio(src, out_path, sample_rate, 1)
            else:
                audio.change_speed(src, out_path, speed, sample_rate, 1)

            # 叠话段尾淡出：伸进下一句的部分渐隐（≤0.8s），下一句保持清晰，
            # 本句像自然收尾一样淡出——"抢话"听感而非"两句话一样响"
            overlap_tail = 0.0
            if rescue == "overlap" and seg_id in ordered_ids:
                i = ordered_ids.index(seg_id)
                if i + 1 < len(ordered_ids):
                    rel_next = starts[ordered_ids[i + 1]] - (starts[seg_id] - pre_borrow)
                    tail = input_dur / speed - rel_next
                    if tail > 0.05:
                        fade_d = min(max(tail, 0.15), 0.8)
                        st = max(input_dur / speed - fade_d, 0.0)
                        tmp = out_path.with_suffix(".fade.wav")
                        audio.run(["-y", "-i", str(out_path),
                                   "-af", f"afade=t=out:st={st:.3f}:d={fade_d:.3f}",
                                   "-ar", str(sample_rate), "-ac", "1",
                                   "-c:a", "pcm_s16le", str(tmp)])
                        tmp.replace(out_path)
                        overlap_tail = round(tail, 3)
                        print(f"[align] {seg_id} 叠话 {tail:.2f}s → 尾淡出 {fade_d:.2f}s")

            dub_rms = audio.segment_rms_db(out_path, 0.0, input_dur / speed)
            orig_rms = None
            if vocals_ok and seg_id in starts and seg_id in ends:
                orig_rms = audio.segment_rms_db(vocals, starts[seg_id], ends[seg_id] - starts[seg_id])
            measured[seg_id] = (dub_rms, orig_rms)

            borrowed = round(budget - base_durations.get(seg_id, input_dur), 3)
            out_entry = {
                "id": seg_id, "speaker": speaker, "file": out_rel,
                "start": round(starts[seg_id] - pre_borrow, 3) if seg_id in starts else None,
                "target_dur": round(budget, 3),
                "input_dur": round(input_dur, 3), "speed": speed,
                "borrowed": borrowed, "flags": flags,
            }
            if rescue:
                out_entry["rescue"] = rescue
                out_entry["fit_dur"] = round(fit, 3)
                if overlap_tail:
                    out_entry["overlap"] = overlap_tail
            entries.append(out_entry)
            outputs.append(out_rel)
            tag = f" [{', '.join(flags)}]" if flags else ""
            print(f"[align] {seg_id}: {input_dur:.2f}s -> x{speed:.3f}{tag}")

        # 第二遍：逐段响度贴回原声（段级匹配，保留段内动态）。
        # gain = 原声段 RMS - 配音段 RMS，夹 ±8dB；不可测的段用全体增益中位数兜底。
        gains = [
            round(o - d, 2)
            for d, o in measured.values() if d is not None and o is not None
        ]
        fallback = round(statistics.median(gains), 2) if gains else 0.0
        applied = 0
        if vocals_ok and entries:
            for e in entries:
                seg_id = e["id"]
                d_rms, o_rms = measured.get(seg_id, (None, None))
                if d_rms is None or o_rms is None:
                    gain = fallback
                else:
                    gain = max(-LOUDNESS_GAIN_CAP, min(LOUDNESS_GAIN_CAP, round(o_rms - d_rms, 2)))
                e["loudness_gain"] = gain
                if abs(gain) < LOUDNESS_APPLY_MIN:
                    continue
                path = project_dir / e["file"]
                tmp = path.with_suffix(".gain.wav")
                audio.run(["-y", "-i", str(path),
                           "-af", f"volume={gain:.2f}dB",
                           "-ar", str(sample_rate), "-ac", "1", "-c:a", "pcm_s16le", str(tmp)])
                tmp.replace(path)
                applied += 1
            print(f"[align] 响度贴回: {applied}/{len(entries)} 段应用增益"
                  f"（中位兜底 {fallback:+.2f}dB，夹位 ±{LOUDNESS_GAIN_CAP:.0f}dB）")

        for entry in existing.values():
            if entry["id"] not in {e["id"] for e in entries}:
                entries.append(entry)

        entries.sort(key=lambda e: e["id"])
        save_json(report_path, {"segments": entries})

        flagged = sum(1 for e in entries if e.get("flags"))
        print(f"[align] 共 {len(entries)} 段，需人工复审 {flagged} 段")
        return outputs

    input_rels = [
        str(manifest_path.relative_to(project_dir)).replace("\\", "/"),
        str(translation_path.relative_to(project_dir)).replace("\\", "/"),
    ]

    ran = run_stage(
        project_dir, STAGE, input_rels, cfg, produce,
        config_keys=["align", "tts"], force=args.force,
        partial=bool(args.seg),
    )

    if ran or report_path.exists():
        print("提示：若 align_report.json 里有 overrun，请回 R2 精简译文后重跑")


if __name__ == "__main__":
    main()
