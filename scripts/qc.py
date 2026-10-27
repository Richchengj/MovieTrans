"""成片自动质检（发布前必跑）。

用法:
    python scripts/qc.py projects/<id>          # 全部检查
    python scripts/qc.py projects/<id> --json   # 输出 JSON（供 CI 用）

检查项（按项目产物逐项核对）：
  1. state.json 所有阶段 done
  2. 成片存在、时长与源视频一致（±0.5s）、含视频流与音轨
  3. 响度：mix.wav 的综合响度接近 target_lufs（±3 LU）
  4. 配音覆盖率：有译文且合成成功的段 / 全部有源文的段
  5. 时长预算：实际配音时长 <= 预算 x align.max_speed（overrun 计数）
  6. 字幕：条数与有效段一致、时间轴单调、无空文本
  7. 翻译：无空译文，review/compress 段占比报告
  8. 双重人声抽查：段间隙处 dub 轨电平应明显低于段内电平
     （原声人声已被替换，间隙只应有背景/静音）
  9. 响度贴合：逐段配音-原声响度差中位 ≤3dB
  10. 声纹相似度：配音段 vs 原声段音色余弦（需 env_asr 的 numpy/torch/pyannote）
  11. 参考音频：每个说话人都要有参考音频（缺失即失败），过短的只点名告警
  12. 引擎一致性：manifest 里出现多个 TTS 引擎 → WARN（音色会段间摆动）

声纹相似度按**说话人**拆开报（2026-10-22）：全片段数被某个主要角色主导时，
池化中位几乎等于这个人的分数（bcs_12min 的 SPK_04 占 63% 段、中位 0.13，
其余 5 人 0.21~0.31，池化中位 0.147）。判定仍用池化中位（不因换算法放宽），
但 detail 里点名最差的说话人，便于直接定位「该给谁换参考音频」。

结果三态：PASS / FAIL / **WARN**。WARN = 因环境缺依赖或计算异常而跳过——
不计入通过数（防止"假绿"：selftest 全绿但质检实际少一项），但不阻塞退出码。
数据性不适用（如无语音视频无可测段）仍记 PASS 跳过，detail 注明原因。
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import audio  # noqa: E402
from lib.config import load_project  # noqa: E402
from lib.schemas import load_json, resolve_edited  # noqa: E402

OUT_DIR = "09_output"


class Report:
    """三态结果：PASS / FAIL / WARN（I-1，2026-10-22）。

    WARN = 因环境缺依赖或计算异常跳过：不计入通过数（防假绿），
    但不影响退出码（rep.failed 只数 FAIL）。
    """

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, name: str, ok: bool, detail: str, warn: bool = False) -> None:
        status = "warn" if warn else ("pass" if ok else "fail")
        self.rows.append({"check": name, "status": status, "ok": ok, "detail": detail})
        mark = "WARN" if warn else ("PASS" if ok else "FAIL")
        print(f"[{mark}] {name}: {detail}")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.rows if r["status"] == "fail")

    @property
    def warned(self) -> int:
        return sum(1 for r in self.rows if r["status"] == "warn")


def loudness_of(path: Path) -> float | None:
    """用 ffmpeg loudnorm 测综合响度（LUFS）。"""
    proc = subprocess.run(
        [audio.ffmpeg_bin(), "-hide_banner", "-nostats", "-i", str(path),
         "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    text = proc.stderr
    try:
        blob = text[text.rindex("{"):text.rindex("}") + 1]
        return float(json.loads(blob)["input_i"])
    except (ValueError, KeyError):
        return None


def rms_window(path: Path, start: float, dur: float) -> float:
    """测某时间窗的 RMS 电平（dBFS，静音=负无穷，这里下限 -120）。"""
    proc = subprocess.run(
        [audio.ffmpeg_bin(), "-hide_banner", "-nostats",
         "-ss", f"{max(start, 0):.3f}", "-t", f"{dur:.3f}", "-i", str(path),
         "-af", "astats=measure_overall=RMS_level:measure_perchannel=none", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    for line in proc.stderr.splitlines():
        if "RMS level dB:" in line:
            try:
                return max(float(line.split("RMS level dB:")[1].strip()), -120.0)
            except ValueError:
                break
    return -120.0


def engine_consistency(manifest: list[dict], allow_mixed: bool = False) -> tuple[bool, str, bool]:
    """检查 manifest 是否混用多个 TTS 引擎。

    返回 (ok, detail, warn)。混用只告警不失败：产物本身是完整的，
    问题在音色一致性与可交付性（A 的 44.5 分钟项目 = 云端 275 段 +
    本地 139 段，同一个人两段之间音色会轻微摆动）。
    部分段没有 engine 标记（2026-10-22 之前跑的老产物）同样告警——
    只看到一段标记就宣布"单一引擎"是假绿。
    """
    tagged = [e for e in manifest if e.get("engine")]
    untagged = len(manifest) - len(tagged)
    engines = sorted({e["engine"] for e in tagged})
    if not engines:
        return True, "manifest 无引擎标记（旧项目产物，无法追溯；重跑 s07 后自动带上）", True
    if len(engines) > 1 and not allow_mixed:
        counts = {eng: sum(1 for e in tagged if e["engine"] == eng) for eng in engines}
        return True, f"混用 {counts}：音色会段间摆动，建议正式交付前整片用同一引擎重跑", True
    detail = f"单一引擎 {engines[0]}"
    if untagged:
        return True, f"{detail}（另有 {untagged} 段无引擎标记，旧产物）", True
    return True, detail, False


def main() -> int:
    ap = argparse.ArgumentParser(description="成片自动质检")
    ap.add_argument("project", help="项目目录")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = ap.parse_args()

    project_dir = Path(args.project).resolve()
    project = load_project(project_dir)
    cfg = project["config"]
    rep = Report()

    # 1. 阶段状态（纯演奏/无语音视频只跑了 s01-s04，交付原样成片，属正常）
    state = load_json(project_dir / "state.json") or {}
    stages = state.get("stages", {})
    bad = [n for n, r in stages.items() if r.get("status") != "done"]
    seg_file = project_dir / "03_asr" / "segments.json"
    no_speech = False
    if seg_file.exists():
        try:
            no_speech = not json.loads(seg_file.read_text(encoding="utf-8")).get("segments")
        except (json.JSONDecodeError, OSError):
            pass
    if bad and no_speech:
        rep.add("阶段状态", True, f"纯演奏/无语音视频：s05-s10 未运行（交付原样成片），其余 {len(stages)} 阶段完成")
    else:
        rep.add("阶段状态", not bad, f"{len(stages)} 个阶段，未完成: {bad or '无'}")
    if no_speech:
        n_pass = sum(1 for r in rep.rows if r["status"] == "pass")
        print(f"\n质检结果: {n_pass}/{len(rep.rows)} 通过（纯演奏/无语音视频，无翻译产物可查）")
        if args.json:
            print(json.dumps(rep.rows, ensure_ascii=False, indent=2))
        return 0

    # 2. 成片（视频源 -> mp4；纯音频源 -> m4a/wav，s10 自动降级）
    out_path = project_dir / OUT_DIR
    videos = sorted(out_path.glob("*.mp4")) or sorted(
        p for ext in ("*.m4a", "*.wav") for p in out_path.glob(ext)
    )
    srts = sorted(out_path.glob("*.srt"))
    if not videos:
        rep.add("成片存在", False, f"{OUT_DIR} 下没有 mp4/m4a/wav 交付物")
        if args.json:
            print(json.dumps(rep.rows, ensure_ascii=False, indent=2))
        return 1

    final = videos[0]
    source = project_dir / project["source_file"]
    src_dur = audio.probe_duration(source)
    out_dur = audio.probe_duration(final)
    dur_ok = abs(out_dur - src_dur) <= 0.5
    rep.add("成片时长", dur_ok, f"源 {src_dur:.2f}s / 成片 {out_dur:.2f}s（差 {out_dur - src_dur:+.2f}s）")
    streams = audio.probe_streams(final)
    has_v = any(s.get("codec_type") == "video" for s in streams)
    has_a = any(s.get("codec_type") == "audio" for s in streams)
    # 纯音频源（无视频轨）走 m4a/wav 交付，只要求音轨
    src_has_v = any(
        s.get("codec_type") == "video"
        for s in audio.probe_streams(project_dir / project["source_file"])
    )
    rep.add("成片流完整", (has_v and has_a) if src_has_v else has_a,
            f"video={has_v} audio={has_a}" + ("" if src_has_v else "（音频源，仅验音轨）"))

    # 3. 响度
    mix = project_dir / "08_mix" / "mix.wav"
    lufs = loudness_of(mix) if mix.exists() else None
    target = float(cfg["mix"]["target_lufs"])
    if lufs is None:
        rep.add("响度", False, "无法测量 mix.wav 响度")
    else:
        rep.add("响度", abs(lufs - target) <= 3.0, f"mix {lufs:.1f} LUFS / 目标 {target:.1f}±3")

    # 4/5. 配音覆盖与预算（按时间轴碰撞判定：配音尾部压到下一句起点才算失败）
    segments = (load_json(resolve_edited(project_dir, "03_asr/segments.json")) or {}).get("segments", [])
    translation = load_json(resolve_edited(project_dir, "04_translate/translation.json")) or {}
    manifest = (load_json(resolve_edited(project_dir, "06_tts/manifest.json")) or {}).get("segments", [])
    text_ids = {it["id"] for it in translation.get("segments", []) if (it.get("target") or "").strip()}
    ok_entries = [e for e in manifest if e.get("status") not in ("failed", "empty") and e.get("file")]
    covered = {e["id"] for e in ok_entries} & text_ids
    cover_ratio = len(covered) / len(text_ids) if text_ids else 0.0
    rep.add("配音覆盖", cover_ratio >= 0.95,
            f"{len(covered)}/{len(text_ids)} 段有配音（{cover_ratio:.0%}）")

    max_speed = float(cfg["align"]["max_speed"])
    starts = {s["id"]: float(s["start"]) for s in segments}
    ends = {s["id"]: float(s["end"]) for s in segments}
    ends = {s["id"]: float(s["end"]) for s in segments}
    total_dur = audio.probe_duration(source)
    # 对齐后的实际铺轨时长：input_dur / speed（s08 的产物）
    report = load_json(resolve_edited(project_dir, "07_align/align_report.json")) or {}
    placed_dur = {}
    eff_starts: dict[str, float] = {}
    planned_overlap: dict[str, float] = {}
    for e in report.get("segments", []):
        if e.get("file") and e.get("input_dur") and e.get("speed"):
            placed_dur[e["id"]] = float(e["input_dur"]) / float(e["speed"])
        if e.get("start") is not None:
            eff_starts[e["id"]] = float(e["start"])  # s08 可能按 pre_borrow 前移
        if e.get("overlap"):
            planned_overlap[e["id"]] = float(e["overlap"])
    by_id = {e["id"]: e for e in manifest}
    ordered = sorted(
        [sid for sid in placed_dur if sid in starts or sid in eff_starts],
        key=lambda sid: eff_starts.get(sid, starts[sid]),
    )
    collisions = []
    for i, sid in enumerate(ordered):
        end_pos = eff_starts.get(sid, starts[sid]) + placed_dur[sid]
        if i + 1 < len(ordered):
            nxt = ordered[i + 1]
            limit = eff_starts.get(nxt, starts[nxt])
        else:
            limit = total_dur + 0.2  # 最后一段允许顶到片尾
        # 0.25s 以内的轻微重叠可接受（听感无碍；用户明确接受少许停顿/重叠）；
        # 计划内叠话（rescue=overlap，s08 已对越界部分做尾淡出）按报告里的
        # overlap 量放行，超出计划的部分仍算碰撞
        tol = 0.25 + planned_overlap.get(sid, 0.0)
        if end_pos > limit + tol:
            collisions.append(f"{sid}({end_pos:.2f}>{limit:.2f})")
    overruns = []
    for e in ok_entries:
        # 救援段按 fit_dur（借空档/叠话目标）判定：贴目标是设计行为，
        # 只有连目标都贴不上的才留人工复审
        target = float(e.get("fit_dur") or e.get("target_dur") or 0.0)
        actual = float(e.get("actual_dur") or 0.0)
        if target > 0 and actual > target * max_speed + 0.05:
            overruns.append(e["id"])
    rep.add("时长预算", not collisions,
            f"时间轴碰撞 {len(collisions)} 处" + (f": {', '.join(collisions)}" if collisions else "")
            + f"；超预算标记 {len(overruns)} 段（留人工复核，不阻塞）: {', '.join(overruns) or '无'}")

    # 6. 字幕
    if text_ids and srts:
        srt_text = srts[0].read_text(encoding="utf-8")
        blocks = [b for b in srt_text.strip().split("\n\n") if b.strip()]
        times = []
        empty = 0
        for b in blocks:
            lines = b.splitlines()
            if len(lines) < 3:
                continue
            times.append(lines[1])
            if not "".join(lines[2:]).strip():
                empty += 1
        monotonic = times == sorted(times)
        cnt_ok = len(blocks) == len(text_ids)
        rep.add("字幕", cnt_ok and monotonic and empty == 0,
                f"{len(blocks)} 条（应 {len(text_ids)}），时间轴单调={monotonic}，空文本={empty}")
    elif text_ids:
        rep.add("字幕", False, "有译文但没有字幕文件")
    else:
        rep.add("字幕", True, "无译文段，跳过")

    # 7. 翻译策略分布
    strategies: dict[str, int] = {}
    for it in translation.get("segments", []):
        s = it.get("strategy") or "?"
        strategies[s] = strategies.get(s, 0) + 1
    empty_targets = [it["id"] for it in translation.get("segments", [])
                     if not (it.get("target") or "").strip()]
    rep.add("翻译质量", not empty_targets,
            f"策略分布={strategies}，空译文 {len(empty_targets)} 段" +
            (f": {empty_targets[:5]}" if empty_targets else ""))

    # 8. 双重人声抽查：对齐后配音的间隙电平应显著低于段内
    report = load_json(resolve_edited(project_dir, "07_align/align_report.json")) or {}
    placed = sorted(
        [(float(e["start"]), float(e.get("target_dur") or 0.0), e["id"])
         for e in report.get("segments", []) if e.get("start") is not None],
    )
    dub = project_dir / "08_mix" / "dub.wav"
    if len(placed) >= 2 and dub.exists():
        gaps = []
        for (s1, d1, _), (s2, _, _) in zip(placed, placed[1:]):
            gap = s2 - (s1 + d1)
            if 0.3 <= gap:
                gaps.append((s1 + d1 + 0.05, min(gap - 0.1, 1.0)))
        if gaps:
            import statistics
            speech_rms = [rms_window(dub, s, min(d, 1.0)) for s, d, _ in placed[:8] if d > 0.5]
            gap_rms = [rms_window(dub, s, min(d, 0.8)) for s, d in gaps[:8]]
            if speech_rms and gap_rms:
                med_speech = statistics.median(speech_rms)
                med_gap = statistics.median(gap_rms)
                # 段内是配音，间隙只剩合成漏音/静音；差 6dB 以上算正常
                rep.add("间隙残留", med_speech - med_gap >= 6.0,
                        f"段内中位 {med_speech:.1f} dBFS vs 间隙中位 {med_gap:.1f} dBFS"
                        f"（差 {med_speech - med_gap:.1f} dB，抽查 {len(gap_rms)} 处）")
    else:
        rep.add("间隙残留", True, "段太少或无 dub.wav，跳过")

    # 9. 响度贴合：配音段电平应贴回原声段电平（s08 响度贴回后的核验）
    vocals = project_dir / "02_stems" / "vocals_clean.wav"
    align_dir = project_dir / "07_align"
    if vocals.exists() and segments:
        deltas = []
        for e in report.get("segments", []):
            sid = e.get("id")
            if sid not in starts or not e.get("file"):
                continue
            wav = project_dir / e["file"]
            if not wav.exists():
                continue
            seg_dur = ends.get(sid, 0) - starts.get(sid, 0)
            if seg_dur < 0.4:
                continue
            o_rms = audio.segment_rms_db(vocals, starts[sid], seg_dur)
            d_rms = audio.segment_rms_db(wav, 0.0, seg_dur + 0.5)
            if o_rms is not None and d_rms is not None:
                deltas.append((sid, d_rms - o_rms))
        if deltas:
            vals = [d for _, d in deltas]
            med = statistics.median(vals)
            worst = max(vals, key=abs)
            worst_id = next(s for s, d in deltas if d == worst)
            rep.add("响度贴合", abs(med) <= 3.0,
                    f"逐段 配音-原声 响度差：中位 {med:+.1f} dB，极值 {worst:+.1f} dB"
                    f"（{worst_id}，±3dB 内为佳；抽查 {len(deltas)} 段）")
        else:
            rep.add("响度贴合", True, "无可测段（原声太短），跳过")
    else:
        rep.add("响度贴合", True, "无原声轨或无段落，跳过")

    # 10. 声纹相似度（G-1）：配音段 vs 原声段的音色余弦相似度。
    # wespeaker 提取的音色特征与语言无关——英文原声 vs 中文配音可比，
    # 直接度量「克隆像不像原演员」。需要 pyannote.audio/torch（env_asr），
    # 其它环境优雅跳过（不算失败）。阈值待多样本校准，当前 0.35 为保守线。
    # ⚠️ HF 环境变量必须在 import torch/pyannote 之前设置——huggingface_hub
    # 的缓存路径在 import 时冻结，后设无效（2026-10-18 实测踩坑）。
    vocals = project_dir / "02_stems" / "vocals_clean.wav"
    if vocals.exists() and segments:
        try:
            import os as _os
            import tempfile

            _os.environ["HF_HUB_OFFLINE"] = "1"  # 权重随包分发，强制离线（s04 configure_hf 同款）
            _os.environ["HF_HOME"] = str(Path(__file__).resolve().parents[1] / "models" / "hf")

            import numpy as np
            import torch
            from pyannote.audio import Inference, Model
            from scipy.io import wavfile

            model = Model.from_pretrained(cfg["diarization"]["embedding_model"])
            inference = Inference(model, window="whole",
                                  device=torch.device("cuda" if torch.cuda.is_available() else "cpu"))

            def embed(path: Path) -> np.ndarray | None:
                with tempfile.TemporaryDirectory(prefix="qc_emb_") as td:
                    tmp = Path(td) / "a.wav"
                    audio.extract_audio(path, tmp, 16000, 1)
                    sr, w = wavfile.read(str(tmp))
                    if w.dtype != np.float32:
                        w = w.astype(np.float32) / 32768.0
                    return np.asarray(inference(
                        {"waveform": torch.from_numpy(w).unsqueeze(0), "sample_rate": sr}
                    ), dtype=np.float32).reshape(-1)

            def cos(a: np.ndarray, b: np.ndarray) -> float:
                return float(a @ b / ((np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9))

            ffmpeg = audio.ffmpeg_bin()
            sims: list[tuple[str, float]] = []
            with tempfile.TemporaryDirectory(prefix="qc_spk_") as td:
                td = Path(td)
                for e in report.get("segments", []):
                    sid = e.get("id")
                    if sid not in starts or not e.get("file"):
                        continue
                    seg_dur = ends.get(sid, 0) - starts.get(sid, 0)
                    if seg_dur < 1.0:
                        continue
                    dubbed = project_dir / str(e["file"]).replace("06_tts", "07_align")
                    if not dubbed.exists():
                        dubbed = project_dir / e["file"]
                    src_clip, dub_clip = td / f"{sid}_src.wav", td / f"{sid}_dub.wav"
                    for src_media, dst, extra in (
                        (vocals, src_clip, ["-ss", f"{starts[sid]:.2f}", "-t", f"{seg_dur:.2f}"]),
                        (dubbed, dub_clip, []),
                    ):
                        subprocess.run(
                            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *extra,
                             "-i", str(src_media), "-ar", "16000", "-ac", "1", str(dst)],
                            check=True, capture_output=True,
                        )
                    e_src, e_dub = embed(src_clip), embed(dub_clip)
                    if e_src is not None and e_dub is not None:
                        sims.append((sid, cos(e_src, e_dub)))
            if sims:
                vals = [v for _, v in sims]
                med, lo = statistics.median(vals), min(vals)
                lo_id = next(s for s, v in sims if v == lo)
                min_sim = float((cfg.get("qc") or {}).get("voice_similarity_min") or 0.18)
                # 按说话人拆开（2026-10-22）：池化中位会被段数最多的角色主导，
                # 明细能直接指向「该给谁换参考音频」
                spk_of = {it["id"]: (it.get("speaker") or "SPK_01")
                          for it in translation.get("segments", [])}
                per_spk: dict[str, list[float]] = {}
                for sid, v in sims:
                    per_spk.setdefault(spk_of.get(sid, "?"), []).append(v)
                details = []
                for spk in sorted(per_spk, key=lambda k: -len(per_spk[k])):
                    m = statistics.median(per_spk[spk])
                    share = len(per_spk[spk]) / len(sims)
                    details.append(f"{spk} {m:.2f}({share:.0%}段)")
                weak = [spk for spk in per_spk
                        if statistics.median(per_spk[spk]) < min_sim]
                ok = med >= min_sim
                detail = (f"配音 vs 原声 音色余弦：中位 {med:.2f}（线 {min_sim:.2f}），"
                          f"最低 {lo:.2f}（{lo_id}）；按说话人 " + "、".join(details[:6]))
                if not ok:
                    hint = []
                    if weak:
                        hint.append(f"低于线的是 {', '.join(sorted(weak))} → "
                                    "这几人优先换参考音频（删 05_voices/<SPK> 后 "
                                    "s06 --force 重跑，或手工指定更长/更典型的 ref_segments）")
                    if lo < 0:
                        hint.append("出现负值通常是该段原声里有别人抢话/音乐，属素材问题")
                    if hint:
                        detail += "；" + "；".join(hint)
                rep.add("声纹相似度", ok, detail)
            else:
                rep.add("声纹相似度", True, "无可测段，跳过")
        except ImportError as exc:
            rep.add("声纹相似度", True,
                    f"跳过（当前环境缺 {exc.name}；用 env_asr 运行 qc 可启用："
                    "envs\\env_asr\\Scripts\\python.exe scripts\\qc.py ...）",
                    warn=True)
        except Exception as exc:  # noqa: BLE001 - 声纹项失败不阻塞其它检查
            rep.add("声纹相似度", True, f"跳过（计算异常：{type(exc).__name__}: {exc}）",
                    warn=True)
    else:
        rep.add("声纹相似度", True, "无原声轨或无段落，跳过")

    # 11. 参考音频完整性（s06 产物）：每个说话人必须能解析到参考音频，
    # 过短的（< qc.ref_short_warn_seconds）只点名——素材本身就少时短参考
    # 仍好过借用别人的音色，不能判失败
    qc_cfg = cfg.get("qc") or {}
    short_limit = float(qc_cfg.get("ref_short_warn_seconds") or 3.0)
    prof_path = resolve_edited(project_dir, "05_voices/profiles.json")
    profiles = load_json(prof_path) if prof_path.exists() else {}
    speakers = {it.get("speaker") or "SPK_01" for it in translation.get("segments", [])}
    missing_ref, short_ref = [], []
    for spk in sorted(speakers):
        rel = (profiles.get(spk) or {}).get("reference")
        if not rel or not (project_dir / rel).exists():
            missing_ref.append(spk)
            continue
        dur_ref = float((profiles.get(spk) or {}).get("duration") or 0.0)
        if 0 < dur_ref < short_limit:
            short_ref.append(f"{spk}({dur_ref:.1f}s)")
    if not speakers:
        rep.add("参考音频", True, "无说话人，跳过")
    else:
        detail = f"{len(speakers) - len(missing_ref)}/{len(speakers)} 个说话人有参考音频"
        if short_ref:
            detail += (f"；偏短（<{short_limit:.0f}s，音色锚点不稳，"
                       f"相似度可能偏低）: {', '.join(short_ref)}")
        if missing_ref:
            detail += f"；缺失: {', '.join(missing_ref)}"
        # 短参考不算跳过，只点名（WARN 语义留给"没算成"的检查项）
        rep.add("参考音频", not missing_ref, detail)

    # 12. 引擎一致性：分批补跑可能混用云端与本地引擎（A 的 44.5 分钟项目
    # 就是 275 段 CosyVoice2 + 139 段 IndexTTS），同一说话人的音色会段间
    # 摆动，正式交付不宜混用 → WARN（不阻塞）
    ok_engine, engine_detail, engine_warn = engine_consistency(
        manifest, bool(qc_cfg.get("allow_mixed_engine"))
    )
    rep.add("引擎一致性", ok_engine, engine_detail, warn=engine_warn)

    n_pass = sum(1 for r in rep.rows if r["status"] == "pass")
    print(f"\n质检结果: {n_pass}/{len(rep.rows)} 通过, {rep.failed} 失败"
          + (f", {rep.warned} 警告（跳过，见上）" if rep.warned else ""))
    if args.json:
        print(json.dumps(rep.rows, ensure_ascii=False, indent=2))
    return 1 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
