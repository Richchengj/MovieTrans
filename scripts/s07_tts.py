"""阶段7：克隆 TTS + 时长控制（IndexTTS-2.5）。

产物:
    06_tts/manifest.json               TTS 结果清单
    06_tts/<SPK>/<seg_XXXX>.wav        每段配音（24k 单声道）

按 `tts.engine` 选引擎，目前实现 indextts2。逐段合成，用
`05_voices/<SPK>/reference.wav` 作为音色参考（s06 产出）。

时长控制（SPEC 5.1 的预算）：
    先按 duration_factor=1.0 生成，量出实际时长；
    若明显超出预算（超出变速上限能吸收的范围），
    用 duration_factor = 预算/实际 重生成一次（夹到 [0.5, 1.0]），
    剩下的零头交给 s08 用变速吸收。

极端段救援（2026-10-21，用户提意"借空档/叠话"；下午修订为**借为主、
叠话兜底**——用户实测叠话有明显听感不适）：
    估算字/秒（字数/预算）≥ align.rescue_min_cps 的段，硬压预算会快到
    听不懂，不贴预算——按 lib/budget.timeline_room 的真实时间窗重定目标：
    ① 借空档 gap：目标 = min(窗口, 自然时长, 预算×rescue_max_ratio)，
       向前后静音借时间，起点可前移 ≤ pre_borrow_max；只要总变速
       （dur/target）≤ rescue_speed_max 且实际字/秒 ≤ rescue_max_cps
       就不叠话——窗口内静音全用上，宁可快一点也不叠；
    ② 叠话 overlap：仅当借满窗口总变速仍超 rescue_speed_max 时才伸进
       下一句（事实对白常有抢话），且只伸「让总变速回落到上限」所需的
       最小量（≤ overlap_max 且不超过下一句窗口的 overlap_next_ratio）；
       s08 对越界部分做尾淡出。
    manifest 记 fit_dur / rescue / pre_borrow，s08/qc/WebUI 据此适配。

引擎运行在 env_tts 里；`indextts` 包直接用仓库里的 tools/index-tts，
不需要单独 pip 安装。

接入新 TTS 引擎的触发条件（满足其一再动手，勿提前抽象）：
    ① 显卡被占/无卡，IndexTTS 本地合成不可接受
       → ✅ 已交付：硅基流动 CosyVoice2 API（s07_tts_sf.py，2026-10-21 实测）
    ② IndexTTS2.5 对某些音色效果不满意的批次
       → 评估 GPT-SoVITS / F5-TTS（GPT-SoVITS 已可跑，见 s07_tts_gsv.py）
接入时需实现的最小契约：
    输入 = 参考音频 + 译文 + 时长提示（duration_factor）
    输出 = 24k 单声道 wav（超长部分 s08 变速兜底）
    s05 预算 / s08 碰撞消解与响度贴回均作用在音频产物上，与引擎无关；
    emo_mode（QwenEmotion 语气）为 IndexTTS 专有能力，其它引擎在
    UI 上需隐藏该控件。

用法:
    python scripts/s07_tts.py projects/20261008_demo_clip [--force] [--seg seg_0001]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

from lib import audio, envs
from lib.budget import plan_rescue, timeline_room
from lib.config import load_project
from lib.schemas import load_json, resolve_edited, save_json
from lib.state import run_stage

STAGE = "s07_tts"
TRANSLATION_REL = "04_translate/translation.json"
PROFILES_REL = "05_voices/profiles.json"
MANIFEST_REL = "06_tts/manifest.json"
SEGMENTS_REL = "03_asr/segments.json"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_TTS_DIR = PROJECT_ROOT / "tools" / "index-tts"
MODEL_DIR = PROJECT_ROOT / "models" / "indextts2.5"

MIN_DURATION_FACTOR = 0.5


def _lang(target_lang: str) -> str:
    lang = (target_lang or "zh").lower()
    return lang if lang in ("zh", "en", "ja", "yue") else "zh"


def load_engine(cfg: dict):
    """按 tts.engine 构造引擎（indextts2 本机 / gpt_sovits reexec / siliconflow 云端）。"""
    engine = str(cfg["tts"].get("engine") or "indextts2")
    if engine == "siliconflow":
        # 云端备选（F-1）：不占本机显存，消耗账户余额；2026-10-21 实测通过
        import s07_tts_sf
        return s07_tts_sf.load_engine(cfg)

    if not (MODEL_DIR / "config.yaml").exists():
        raise SystemExit(f"未找到 IndexTTS 模型: {MODEL_DIR}，请先运行 fetch_models.py indextts")
    if str(INDEX_TTS_DIR) not in sys.path:
        sys.path.insert(0, str(INDEX_TTS_DIR))

    import torch
    from indextts.infer_v2_5 import IndexTTS2

    # 该模块在 import 时把 HF_HUB_CACHE 指向了相对路径 ./checkpoints/hf_cache，
    # 这里改回项目内的模型目录，避免把缓存写到当前工作目录。
    os.environ["HF_HUB_CACHE"] = str(MODEL_DIR / "hf_cache")

    tts_cfg = cfg["tts"]
    emo_text_mode = tts_cfg.get("emo_mode") == "text"
    if emo_text_mode:
        qwen_dir = MODEL_DIR / "qwen0.6bemo4-merge" / "model.safetensors"
        if not qwen_dir.exists():
            raise SystemExit(
                f"emo_mode=text 需要 QwenEmotion 情感模型，未找到 {qwen_dir}；"
                "请运行 python scripts/fetch_models.py indextts 下载，或把 tts.emo_mode 改回 ref"
            )
    use_cuda = torch.cuda.is_available() and str(tts_cfg["device"]).startswith("cuda")
    return IndexTTS2(
        cfg_path=str(MODEL_DIR / "config.yaml"),
        model_dir=str(MODEL_DIR),
        use_bf16=use_cuda,
        device=tts_cfg["device"] if use_cuda else "cpu",
        use_qwen_emo=bool(tts_cfg.get("use_qwen_emo")) or emo_text_mode,
    )


def synthesize(
    engine, ref: Path, text: str, out_path: Path, lang: str,
    duration_factor: float, emo_mode: str,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    engine.infer(
        spk_audio_prompt=str(ref),
        text=text,
        output_path=str(out_path),
        lang=lang,
        duration_factor=duration_factor,
        use_emo_text=(emo_mode == "text"),
        verbose=False,
        text_normalization=True,
    )


def resolve_references(project_dir: Path, profiles: dict) -> tuple[dict[str, Path], Path | None]:
    """返回 说话人->参考音频 的映射，以及任意一个可用的兜底参考。"""
    by_speaker: dict[str, Path] = {}
    fallback: Path | None = None
    for speaker, meta in profiles.items():
        ref = meta.get("reference")
        if ref:
            path = project_dir / ref
            if path.exists():
                by_speaker[speaker] = path
                if fallback is None:
                    fallback = path
    return by_speaker, fallback


def classify(dur: float, budget: float, cfg: dict) -> str:
    if budget <= 0:
        return "review"
    if dur <= budget:
        return "ok"
    if dur <= budget * float(cfg["align"]["max_speed"]):
        return "compress"
    return "review"


def _fresh(p: Path) -> Path:
    """返回一个当前不存在的路径。

    IndexTTS 保存输出前会 os.remove 已存在的同名文件（infer_v2_5.py:644/1001），
    "先删后写"在某些环境下会触发删除确认保护并中断合成。换名写入是纯新增，
    零删除；旧临时文件留在 work/tts 无害（不影响产物，全量重跑时会被绕开）。
    """
    if not p.exists():
        return p
    i = 1
    while True:
        q = p.with_name(f"{p.stem}.{i}{p.suffix}")
        if not q.exists():
            return q
        i += 1


def _manifest_coverage(items: list[dict], present_ids, existing_ids) -> tuple[bool, list[str]]:
    """补跑后 manifest 是否覆盖全部「有译文」的段（2026-10-22）。

    分批补跑（--seg）常常是为了把长片跑完：这一段跑完若已覆盖全部有译文
    的段，阶段就该记 done，而不是留 partial 逼着下次整体重算（A 的 44.5 分钟
    项目就踩了这个：275 段云端 + 139 段本地补跑后 state 停在 partial，
    只能手工改 state 才能 --from s08 续跑）。
    """
    translated = {it["id"] for it in items if (it.get("target") or "").strip()}
    have = set(existing_ids) | set(present_ids)
    missing = sorted(translated - have)
    return (not missing), missing


def main() -> None:
    ap = argparse.ArgumentParser(description="阶段7：克隆 TTS")
    ap.add_argument("project", help="项目目录")
    ap.add_argument("--force", action="store_true", help="强制重跑")
    ap.add_argument("--seg", action="append", help="只合成指定段落（可重复）")
    args = ap.parse_args()

    # 引擎分流：gpt_sovits 跑在自己的 venv（torch 版本与 IndexTTS 互斥），
    # 在 ensure_env 之前 reexec 到 s07_tts_gsv.py，后续流程与产物完全一致
    project_dir_resolved = Path(args.project).resolve()
    project = load_project(project_dir_resolved)
    engine = str(project["config"]["tts"].get("engine") or "indextts2")
    if engine == "gpt_sovits" and os.environ.get("DUB_REEXEC_TTS_GSV") != "1":
        import subprocess

        home = Path(str(project["config"]["tts"].get("gpt_sovits_home") or r"D:\GPT-SoVITS"))
        gsv_python = home / "venv" / "Scripts" / "python.exe"
        if not gsv_python.exists():
            raise SystemExit(
                f"tts.engine=gpt_sovits 但未找到其 venv: {gsv_python}\n"
                "请检查 tts.gpt_sovits_home 配置"
            )
        child_env = dict(os.environ, DUB_REEXEC_TTS_GSV="1")
        child_env.pop("PYTHONPATH", None)  # 不带沙箱 shim 进子进程
        # GSV 的 inference_webui 用相对路径找 pretrained_models，子进程 cwd 必须在其部署根；
        # project 参数相应转绝对路径
        child_argv = [str(project_dir_resolved)] + [
            a for i, a in enumerate(sys.argv[1:]) if i != 0
        ]
        print(f"[tts] 引擎=GPT-SoVITS，转交 {gsv_python} 执行")
        raise SystemExit(subprocess.call(
            [str(gsv_python), str(PROJECT_ROOT / "scripts" / "s07_tts_gsv.py"), *child_argv],
            cwd=str(home), env=child_env,
        ))

    envs.ensure_env("env_tts", "modelscope")
    audio.use_bundled_ffmpeg()

    project_dir = Path(args.project).resolve()
    project = load_project(project_dir)
    cfg = project["config"]
    from lib import gpu as gpu_mod

    if str(cfg["tts"].get("engine") or "indextts2") == "siliconflow":
        cfg["tts"]["device"] = "cuda"  # 云端引擎不占本机显存，跳过降级探测
    else:
        cfg["tts"]["device"] = gpu_mod.effective_device(
            cfg["tts"]["device"], need_gb=8.0, stage="s07 TTS"
        )

    translation_path = resolve_edited(project_dir, TRANSLATION_REL)
    if not translation_path.exists():
        raise SystemExit(f"缺少 {TRANSLATION_REL}，请先运行 s05_translate")
    translation = load_json(translation_path)
    items = translation.get("segments", [])
    if not items:
        raise SystemExit("没有可合成的段落")

    profiles_path = resolve_edited(project_dir, PROFILES_REL)
    if not profiles_path.exists():
        raise SystemExit(f"缺少 {PROFILES_REL}，请先运行 s06_voice_profile")
    profiles = load_json(profiles_path)
    refs, fallback_ref = resolve_references(project_dir, profiles)
    if not refs:
        raise SystemExit(
            "没有任何可用的参考音频（05_voices/*/reference.wav）；"
            "请检查 s06 是否生成了足够长的片段。"
        )

    sample_rate = int(cfg["tts"]["sample_rate"])
    lang = _lang(cfg["target_lang"])
    work_dir = project_dir / "work" / "tts"
    manifest_path = project_dir / MANIFEST_REL
    tts_dir = project_dir / "06_tts"

    # 时间轴邻接表：极端段救援（借空档/叠话）需要知道每段前后的真实空隙
    segments_path = resolve_edited(project_dir, SEGMENTS_REL)
    starts: dict[str, float] = {}
    ends: dict[str, float] = {}
    ordered_ids: list[str] = []
    if segments_path.exists():
        seg_list = load_json(segments_path).get("segments", [])
        ordered_ids = [s["id"] for s in sorted(seg_list, key=lambda s: float(s["start"]))]
        for seg in seg_list:
            starts[seg["id"]] = float(seg["start"])
            ends[seg["id"]] = float(seg["end"])
    pos = {sid: i for i, sid in enumerate(ordered_ids)}

    selected = set(args.seg) if args.seg else None

    # 已有清单：--seg 只重做指定段落，其余保留
    existing: dict[str, dict] = {}
    if selected is not None and manifest_path.exists():
        for entry in load_json(manifest_path).get("segments", []):
            existing[entry["id"]] = entry

    # 已在清单里且音频确实存在的段（用于判断本次补跑是否凑齐全量）
    existing_ok = {
        e["id"] for e in existing.values()
        if e.get("file") and e.get("status") not in ("failed", "empty")
        and (project_dir / e["file"]).exists()
    }

    todo = [
        it for it in items
        if (selected is None or it["id"] in selected) and (it.get("target") or "").strip()
    ]
    todo.sort(key=lambda it: (it.get("speaker", ""), it["id"]))
    if not todo and selected is None:
        raise SystemExit("所有段落译文为空，请先在 R2 复核里补译")

    # 分批补跑：跑完是否已凑齐全量？凑齐就标 done（A 的 44.5 分钟项目踩过
    # 这个坑——补跑完 state 停在 partial，--from s08 被判成上游未完成）
    will_cover, still_missing = _manifest_coverage(
        items, [it["id"] for it in todo], existing_ok
    )
    run_info: dict = {"failed": 0}
    if selected is not None:
        if will_cover:
            print(f"[tts] 本次补跑覆盖全部有译文的段（待合成 {len(todo)} 段），"
                  "跑完将标记 s07=done")
        else:
            print(f"[tts] 本次补跑后仍缺 {len(still_missing)} 段配音"
                  f"（如 {', '.join(still_missing[:5])}），阶段记 partial，下次需继续补")

    # 极端段预判（合成前即可算）：估算字/秒 = 字数/预算，超标的段硬压预算
    # 会快到听不懂，走救援（借空档/叠话）而不是压预算
    rescue_min_cps = float(cfg["align"].get("rescue_min_cps", 7.0))

    def _est_cps(it: dict) -> float:
        budget = float(it.get("duration_budget") or 0.0)
        if budget <= 0:
            return 0.0
        return len(re.sub(r"\s", "", it.get("target") or "")) / budget

    extreme_ids = {it["id"] for it in items if _est_cps(it) >= rescue_min_cps}
    if extreme_ids:
        print(f"[tts] 极端段 {len(extreme_ids)} 个（估算 ≥{rescue_min_cps:.1f} 字/秒）："
              f"{', '.join(sorted(extreme_ids))} → 走借空档/叠话救援")

    def produce() -> list[str]:
        work_dir.mkdir(parents=True, exist_ok=True)
        emo_mode = str(cfg["tts"].get("emo_mode") or "ref")
        engine_name = str(cfg["tts"].get("engine") or "indextts2")
        if engine_name == "siliconflow" and emo_mode == "text":
            print("[tts] siliconflow 引擎不支持 emo_mode=text（QwenEmotion 专有），回退 ref")
            emo_mode = "ref"
        # 语速提升：第一遍合成就用 1/boost 的 duration_factor，模型原生生成
        # 电影对白节奏（无后处理变速的机械感）；译文短于原声时尾部自然留白
        boost = float(cfg["tts"].get("tempo_boost") or 1.0)
        boost = max(1.0, min(boost, 1.5))
        base_factor = 1.0 / boost
        engine = load_engine(cfg)
        entries: list[dict] = []
        outputs = [MANIFEST_REL]
        failed = 0
        # 速度监控（2026-10-22）：云端长片会从 2s/段恶化到 20~75s/段，
        # 不给进度与 ETA 就只能靠猜。每 10 段报一次，超阈值额外告警一次
        slow_limit = float(cfg["tts"].get("slow_segment_seconds") or 20.0)
        t_all = time.monotonic()
        warned_slow = False
        done = 0

        for item in todo:
            seg_id = item["id"]
            speaker = item.get("speaker") or "SPK_01"
            text = item["target"].strip()
            budget = float(item.get("duration_budget") or 0.0)
            ref = refs.get(speaker, fallback_ref)
            t_seg = time.monotonic()

            out_rel = f"06_tts/{speaker}/{seg_id}.wav"
            out_path = project_dir / out_rel
            raw = _fresh(work_dir / f"{seg_id}.raw.wav")

            try:
                synthesize(engine, ref, text, raw, lang, base_factor, emo_mode)
                # TTS 输出普遍带 0.3~1s 头尾静音，虚高时长会放大兜底倍率
                # （"真的吗"3 字含静音 2.39s，trim 后 1.0s）——先修剪再测时长
                trimmed = _fresh(work_dir / f"{seg_id}.trim.wav")
                if audio.trim_silence(raw, trimmed):
                    d_trimmed = audio.probe_duration(trimmed)
                    if 0 < d_trimmed < audio.probe_duration(raw) + 0.01:
                        raw = trimmed
                dur = audio.probe_duration(raw)
                factor_used = base_factor
                atempo_used = 1.0

                # 极端段救援（2026-10-21）：估算字/秒超标的段不硬压预算，
                # 改借时间轴真实窗口（前后空档）做拟合目标——能多慢多慢；
                # 借满窗口总变速仍超 rescue_speed_max 的，才允许伸进下一句
                # （叠话有上限，s08 铺轨时对越界部分做尾淡出）
                fit = budget
                rescue = None
                pre_borrow = 0.0
                if seg_id in extreme_ids and budget > 0:
                    i = pos.get(seg_id, -1)
                    prev_extreme = i > 0 and ordered_ids[i - 1] in extreme_ids
                    window, pre_borrow, next_window = timeline_room(
                        seg_id, starts, ends, ordered_ids, cfg, prev_extreme=prev_extreme
                    )
                    target, rescue = plan_rescue(
                        dur, budget, window, next_window, cfg,
                        chars=len(re.sub(r"\s", "", text)),
                    )
                    fit = target
                    win_txt = "片尾" if window == float("inf") else f"{window:.2f}s"
                    print(f"[tts] {seg_id} 救援[{rescue}]：预算 {budget:.2f}s → 目标 "
                          f"{fit:.2f}s（窗口 {win_txt}，实测 {dur:.2f}s）")
                    if fit < dur - 0.05:
                        factor = max(MIN_DURATION_FACTOR, factor_used * fit / dur)
                        if factor < factor_used - 0.005:
                            retry = _fresh(work_dir / f"{seg_id}.retry.wav")
                            synthesize(engine, ref, text, retry, lang, factor, emo_mode)
                            dur_retry = audio.probe_duration(retry)
                            if dur_retry < dur:
                                raw, dur, factor_used = retry, dur_retry, factor
                                print(f"[tts] {seg_id} 救援重生成：df x{factor:.2f} -> {dur:.2f}s")

                # 超预算（含变速上限吸收不了）时压缩重生成；boost 仍然生效
                if rescue is None and budget > 0 and dur > budget * float(cfg["align"]["max_speed"]):
                    factor = max(MIN_DURATION_FACTOR, budget / dur)
                    if factor < factor_used - 0.005:
                        retry = _fresh(work_dir / f"{seg_id}.retry.wav")
                        synthesize(engine, ref, text, retry, lang, factor, emo_mode)
                        dur_retry = audio.probe_duration(retry)
                        if dur_retry < dur:
                            raw, dur, factor_used = retry, dur_retry, factor

                # 语速微调兜底：重生成后仍超目标（或重生成不划算），用 atempo
                # 硬性贴合**目标本身**（普通段=预算；救援段=窗口/叠话目标。
                # 预算已含硬窗截断，贴 1.1 倍余量会重新撞上下一段起点）。
                # 轻微变速失真 < 叠话/错位对成片的伤害；tts.atempo_fallback=false 可关闭
                if fit > 0 and dur > fit and bool(cfg["tts"].get("atempo_fallback", True)):
                    fitted = _fresh(work_dir / f"{seg_id}.fit.wav")
                    tempo = dur / fit
                    if audio.tempo_fit(raw, fitted, tempo):
                        dur_fit = audio.probe_duration(fitted)
                        if dur_fit <= fit + 0.05:
                            raw, dur, atempo_used = fitted, dur_fit, tempo
                            print(f"[tts] {seg_id} atempo 兜底：x{tempo:.2f} -> {dur_fit:.2f}s")

                audio.extract_audio(raw, out_path, sample_rate, 1)
            except Exception as exc:  # noqa: BLE001 - 单段失败不该拖垮整条链路
                failed += 1
                print(f"[tts] {seg_id} 合成失败：{type(exc).__name__}: {exc}")
                entries.append({
                    "id": seg_id, "speaker": speaker, "file": None,
                    "sr": sample_rate, "target_dur": round(budget, 3),
                    "actual_dur": 0.0, "duration_factor": 1.0, "status": "failed",
                    "engine": engine_name,
                    "elapsed": round(time.monotonic() - t_seg, 2),
                })
                done += 1
                continue

            status = classify(dur, fit, cfg)
            entry = {
                "id": seg_id, "speaker": speaker, "file": out_rel,
                "sr": sample_rate, "target_dur": round(budget, 3),
                "actual_dur": round(dur, 3),
                "duration_factor": round(factor_used, 3),
                # atempo 后处理变速比（1.0=未变速）；WebUI 语速列 =
                # (1/duration_factor) × atempo × 摆放变速 = 相对自然语速的倍率
                "atempo": round(atempo_used, 3),
                "status": status,
                # 出自哪个引擎：分批补跑可能混用云端/本地，qc 与交付据此判断
                # 音色是否会段间摆动（2026-10-22 A 项目就是混合产物）
                "engine": engine_name,
                "elapsed": round(time.monotonic() - t_seg, 2),
            }
            if rescue:
                # 救援元数据：s08 按 fit_dur 变速、按 pre_borrow 前移起点、
                # 对 overlap 段越界部分做尾淡出；qc 放行计划内叠话
                entry["fit_dur"] = round(fit, 3)
                entry["rescue"] = rescue
                if pre_borrow > 0:
                    entry["pre_borrow"] = round(pre_borrow, 3)
            entries.append(entry)
            outputs.append(out_rel)
            extra = f" → 目标 {fit:.2f}s[{rescue}]" if rescue else ""
            print(f"[tts] {seg_id} {speaker}: {dur:.2f}s / 预算 {budget:.2f}s{extra} -> {status}")
            done += 1
            if done % 10 == 0 or done == len(todo):
                spent = time.monotonic() - t_all
                avg = spent / done
                left = (len(todo) - done) * avg
                print(f"[tts] 进度 {done}/{len(todo)}，段均 {avg:.1f}s，"
                      f"已用 {spent / 60:.1f}min，预计剩余 {left / 60:.1f}min（引擎 {engine_name}）")
                if avg > slow_limit and not warned_slow:
                    warned_slow = True
                    print(f"[tts] ⚠ 段均 {avg:.1f}s 超过告警线 {slow_limit:.0f}s："
                          f"云端排队/本机显存争用都可能。长片建议 ①切 tts.engine 本地 GPU "
                          f"②分批 --seg 跑完几段就落盘（产物可续）③退掉占显存的程序")

        for entry in existing.values():
            if entry["id"] not in {e["id"] for e in entries}:
                entries.append(entry)

        entries.sort(key=lambda e: e["id"])
        save_json(manifest_path, {"segments": entries, "engine": engine_name})
        run_info["failed"] = failed

        if failed and failed == len(todo):
            raise RuntimeError("所有段落都合成失败，请检查模型与环境")

        counts: dict[str, int] = {}
        for entry in entries:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        print(f"[tts] 共 {len(entries)} 段，状态分布={counts}")
        return outputs

    input_rels = [
        str(translation_path.relative_to(project_dir)).replace("\\", "/"),
        str(profiles_path.relative_to(project_dir)).replace("\\", "/"),
    ]
    if segments_path.exists():
        input_rels.append(
            str(segments_path.relative_to(project_dir)).replace("\\", "/")
        )

    ran = run_stage(
        project_dir, STAGE, input_rels, cfg, produce,
        config_keys=["tts", "align", "target_lang"], force=args.force,
        # --seg 时传判定函数：跑完看「是否已凑齐全量 + 无失败段」再决定
        # 标 done 还是 partial（state.py 支持 partial 为可调用对象）
        partial=((lambda: not (will_cover and run_info["failed"] == 0))
                 if selected is not None else False),
    )

    if ran or manifest_path.exists():
        print("提示：可试听 06_tts/<SPK>/*.wav，确认音色与语速（R3 卡点）")


if __name__ == "__main__":
    main()
