"""阶段7（GPT-SoVITS 版）：克隆 TTS + 时长控制。

与 s07_tts.py（IndexTTS-2.5 版）流程完全一致：读同一份 translation/profiles，
产出同一格式的 06_tts/<SPK>/<seg_XXXX>.wav + manifest.json，s08/s09/s10 无感。

差异只在引擎：
    - 运行环境：本脚本由 s07_tts.py 在 `tts.engine=gpt_sovits` 时自动 reexec 到
      GPT-SoVITS 的 venv 执行（两套 torch 版本互斥，不能共用一个环境）
    - 推理接口：GPT_SoVITS.inference_webui.get_tts_wav（v2final 底模）
    - 音色参考 = 参考音频 + **参考文本**（IndexTTS 零样本不需要文本；
      参考文本取 s06 在该说话人 ref_segments[0] 对应的英文原文）
    - 语速：IndexTTS 的 duration_factor（>1 变慢）在这里映射为
      speed = 1 / duration_factor（>1 变快）
    - emo_mode（QwenEmotion 语气）为 IndexTTS 专有能力，本引擎忽略

配置（project.json 的 tts 段）：
    engine = "gpt_sovits"
    gpt_sovits_home = "D:\\GPT-SoVITS"     # GPT-SoVITS 部署根（含 venv 与 GPT_SoVITS/）

用法（一般不用手跑，pipeline 会自动分流）:
    python scripts/s07_tts_gsv.py projects/20261018_demo [--force] [--seg seg_0001]
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from lib import audio, envs  # noqa: E402
from lib.budget import plan_rescue, timeline_room  # noqa: E402
from lib.config import load_project  # noqa: E402
from lib.schemas import load_json, resolve_edited, save_json  # noqa: E402
from lib.state import run_stage  # noqa: E402

STAGE = "s07_tts"
TRANSLATION_REL = "04_translate/translation.json"
PROFILES_REL = "05_voices/profiles.json"
SEGMENTS_REL = "03_asr/segments.json"
MANIFEST_REL = "06_tts/manifest.json"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIN_SPEED = 0.5  # 与 IndexTTS 版 MIN_DURATION_FACTOR 对应（最快 2 倍速）


def _gsv_home(cfg: dict) -> Path:
    home = str(cfg["tts"].get("gpt_sovits_home") or r"D:\GPT-SoVITS")
    p = Path(home)
    if not (p / "GPT_SoVITS" / "inference_webui.py").exists():
        raise SystemExit(
            f"未找到 GPT-SoVITS 部署：{p}\n"
            "请在 project.json 的 tts.gpt_sovits_home 配置实际路径"
            "（需含 GPT_SoVITS/ 代码目录与 venv/）"
        )
    return p


def _model_paths(home: Path) -> tuple[Path, Path]:
    base = home / "GPT_SoVITS" / "pretrained_models" / "gsv-v2final-pretrained"
    gpt = base / "s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt"
    sovits = base / "s2G2333k.pth"
    for p in (gpt, sovits):
        if not p.exists():
            raise SystemExit(f"未找到 GPT-SoVITS 权重: {p}，请先下载（fetch_models.py gpt_sovits）")
    return gpt, sovits


def load_engine(cfg: dict, home: Path):
    """构造 GPT-SoVITS 推理入口（加载 v2final 底模）。"""
    # inference_webui 的 bert/hubert 路径是相对部署根的（GPT_SoVITS/pretrained_models），
    # 且模块 import 时就加载 bert —— **chdir 必须在 import 之前**
    os.chdir(home)
    if str(home) not in sys.path:
        sys.path.insert(0, str(home))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    from GPT_SoVITS.inference_webui import change_gpt_weights, change_sovits_weights

    gpt, sovits = _model_paths(home)
    change_gpt_weights(gpt_path=str(gpt))
    change_sovits_weights(sovits_path=str(sovits))
    return {"gpt": str(gpt), "sovits": str(sovits)}


REF_MIN, REF_MAX = 3.0, 9.5  # inference_webui 硬校验 3~10s，留余量


def _seg_text(seg: dict) -> str:
    # segments.json 用 text 字段；早期产物曾叫 source，两者都认
    return str(seg.get("text") or seg.get("source") or "").strip()


def _seg_dur(seg: dict) -> float:
    return float(seg.get("end", 0.0)) - float(seg.get("start", 0.0))


def build_gsv_ref(
    project_dir: Path, speaker: str, profiles: dict, segments: list[dict], out_dir: Path
) -> tuple[Path, str] | None:
    """为 GSV 构造「音频-文本严格对应」的参考（3~9.5s）。

    GPT-SoVITS 用 prompt_text 建立音色锚点，音频与文本不一致会显著破坏克隆
    相似度（本机实测：文本错配时音色余弦 0.08，正确对应后 0.2+）。s06 的
    reference.wav 是 ≤12s 的多段拼接，裁到 9.9s 后与任何单段原文都对不上，
    因此这里从人声轨重新切：
      1) 优先单段落在 3.0~9.5s（音频与文本天然一致）
      2) 否则多段拼接，累计落进 3.5~9.5s，文本按段顺序用空格连接
    """
    by_id = {s["id"]: s for s in segments}
    mine = [s for s in segments if (s.get("speaker") or "SPK_01") == speaker and _seg_text(s)]
    if not mine:
        return None

    # 候选顺序：s06 选的 ref_segments 优先（已过质量筛选），再按时长降序补足
    ordered: list[dict] = []
    seen: set[str] = set()
    for sid in (profiles.get(speaker) or {}).get("ref_segments") or []:
        seg = by_id.get(sid)
        if seg and _seg_text(seg) and sid not in seen:
            ordered.append(seg)
            seen.add(sid)
    for seg in sorted(mine, key=lambda s: -_seg_dur(s)):
        if seg["id"] not in seen:
            ordered.append(seg)
            seen.add(seg["id"])

    # 1) 单段命中
    for seg in ordered:
        if REF_MIN <= _seg_dur(seg) <= REF_MAX:
            return _cut_ref(project_dir, speaker, [seg], out_dir)

    # 2) 多段拼接（优先长段）；短句角色（只有 0.2~2s 的零碎台词）也允许拼到 3.5s
    picked: list[dict] = []
    total = 0.0
    for seg in ordered:
        if _seg_dur(seg) < 0.3:
            continue
        picked.append(seg)
        total += _seg_dur(seg)
        if total >= REF_MIN + 0.5:
            break
        if len(picked) >= 6:
            break
    if picked and REF_MIN <= total <= REF_MAX + 2.0:
        return _cut_ref(project_dir, speaker, picked, out_dir)
    return None


def _cut_ref(project_dir: Path, speaker: str, segs: list[dict], out_dir: Path) -> tuple[Path, str]:
    """按给定段切/拼参考音频，文本与音频一一对应；结果按段 id 缓存。"""
    src = project_dir / "02_stems" / "vocals_clean.wav"
    if not src.exists():
        src = project_dir / "02_stems" / "vocals.wav"
    if not src.exists():
        raise SystemExit(f"缺少人声轨 {src}，无法构造 GSV 参考音频")

    out_dir.mkdir(parents=True, exist_ok=True)
    tag = "__".join(s["id"] for s in segs)
    out = out_dir / f"{speaker}__{tag}.wav"
    txt = out.with_suffix(".txt")
    text = " ".join(_seg_text(s) for s in segs)
    if not out.exists():
        parts: list[Path] = []
        for seg in segs:
            part = out_dir / f"{speaker}__{seg['id']}.part.wav"
            start = float(seg["start"])
            audio.cut(str(src), str(part), start, start + _seg_dur(seg), 24000, 1)
            parts.append(part)
        if len(parts) == 1:
            parts[0].replace(out)
        else:
            audio.concat([str(p) for p in parts], str(out), 24000, 1)
            for p in parts:
                p.unlink(missing_ok=True)
    txt.write_text(text, encoding="utf-8")
    return out, text


def synthesize(engine: dict, home: Path, ref_wav: Path, ref_text: str,
               text: str, out_path: Path, speed: float) -> float:
    """合成一段，返回实际时长（秒）。

    GPT-SoVITS v2final 的 speed 参数实测不改变输出时长（2026-10-18），
    因此固定 speed=1 合成，语速用 ffmpeg atempo 后处理实现（变速不变调）。
    """
    from GPT_SoVITS.inference_webui import get_tts_wav

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".raw.wav")
    result = list(get_tts_wav(
        ref_wav_path=str(ref_wav),
        prompt_text=ref_text,
        prompt_language="英文",
        text=text,
        text_language="中文",
        top_p=0.6,
        top_k=20,
        temperature=0.6,
    ))
    if not result:
        raise RuntimeError("GPT-SoVITS 未返回音频")
    import soundfile as sf

    sr, wav = result[-1]
    sf.write(str(tmp), wav, sr)

    if abs(speed - 1.0) > 0.01:
        atempo = max(0.5, min(2.0, speed))
        sped = out_path.with_suffix(".sped.wav")
        subprocess.run(
            [audio.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(tmp), "-af", f"atempo={atempo}", str(sped)],
            check=True,
        )
        tmp.unlink(missing_ok=True)
        tmp = sped

    audio.extract_audio(tmp, out_path, 24000, 1)
    tmp.unlink(missing_ok=True)
    return audio.probe_duration(out_path)


def classify(dur: float, budget: float, cfg: dict) -> str:
    if budget <= 0:
        return "review"
    if dur <= budget:
        return "ok"
    if dur <= budget * float(cfg["align"]["max_speed"]):
        return "compress"
    return "review"


def resolve_references(project_dir: Path, profiles: dict) -> tuple[dict[str, Path], Path | None]:
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


def main() -> None:
    ap = argparse.ArgumentParser(description="阶段7：克隆 TTS（GPT-SoVITS 版）")
    ap.add_argument("project", help="项目目录")
    ap.add_argument("--force", action="store_true", help="强制重跑")
    ap.add_argument("--seg", action="append", help="只合成指定段落（可重复）")
    args = ap.parse_args()

    audio.use_bundled_ffmpeg()

    project_dir = Path(args.project).resolve()
    project = load_project(project_dir)
    cfg = project["config"]

    # 2026-10-22 中英对照实测：零样本音色余弦 0.017（英→英）/ -0.009（英→中），
    # 参考音频与 prompt_text 正确匹配的前提下依然塌到「不同人」区间。
    # 这里不阻止运行（做实验/微调后验证仍需要它），但必须让用户知道默认不可交付。
    print("[tts] ⚠ GPT-SoVITS 零样本模式音色不可用（实测余弦 0.07，阈值 0.18）："
          "仅适合做实验或 few-shot 微调后的验证；要可交付音色请用 indextts2 / siliconflow")
    # GSV 的 device 由 inference_webui 内部管理，无法像 IndexTTS 那样强制 CPU——
    # 显存不足只警告（提示切回 indextts2 引擎获得自动降级）
    from lib import gpu as gpu_mod

    free = gpu_mod.free_vram_gb()
    if str(cfg["tts"].get("device", "cuda")).startswith("cuda") and free is not None and free < 6.0:
        print(f"[gpu] ⚠️ GPU 空闲显存 {free:.1f}GB < GSV 需求 ~6GB：GSV 引擎暂不支持自动 CPU 降级，显存不足可能 OOM")
        print("[gpu]    建议关闭占显存的程序后重跑，或将 tts.engine 切回 indextts2（支持自动降级）")

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
        raise SystemExit("没有任何可用的参考音频（05_voices/*/reference.wav）")

    segments_path = resolve_edited(project_dir, SEGMENTS_REL)
    segments = load_json(segments_path).get("segments", []) if segments_path.exists() else []

    # GSV 的参考必须「音频与文本严格对应」且落在 3~10s（inference_webui 硬校验）：
    # s06 的 reference.wav 是 ≤12s 多段拼接，直接裁剪会让 prompt_text 与音频内容
    # 对不上，音色锚点失效（实测音色余弦 0.08）。改为按段id 重建参考 + 对应文本。
    gsv_work = project_dir / "work" / "tts" / "gsv_refs"
    prompts: dict[str, str] = {}
    for spk in list(refs):
        built = build_gsv_ref(project_dir, spk, profiles, segments, gsv_work)
        if not built:
            print(f"[tts] ⚠️ {spk}: 找不到 3~9.5s 可用的单段/可拼接参考，退回拼接参考页")
            continue
        wav, text = built
        refs[spk] = wav
        prompts[spk] = text
        dur = audio.probe_duration(str(wav))
        print(f"[tts] {spk} 参考 {dur:.2f}s ← {wav.name}  文本: {text[:48]}")
    # 兜底参考取任意一个已重建的（保证文本也对应）
    fallback_ref = next((refs[s] for s in prompts), fallback_ref)

    home = _gsv_home(cfg)
    sample_rate = int(cfg["tts"]["sample_rate"])
    work_dir = project_dir / "work" / "tts"
    manifest_path = project_dir / MANIFEST_REL

    selected = set(args.seg) if args.seg else None
    existing: dict[str, dict] = {}
    if selected is not None and manifest_path.exists():
        for entry in load_json(manifest_path).get("segments", []):
            existing[entry["id"]] = entry

    todo = [
        it for it in items
        if (selected is None or it["id"] in selected) and (it.get("target") or "").strip()
    ]
    todo.sort(key=lambda it: (it.get("speaker", ""), it["id"]))
    if not todo and selected is None:
        raise SystemExit("所有段落译文为空，请先在 R2 复核里补译")

    # 时间轴邻接表 + 极端段预判：救援（借空档/叠话）需要真实窗口，
    # 与 IndexTTS 版共用 lib/budget 的决策函数
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
    rescue_min_cps = float(cfg["align"].get("rescue_min_cps", 7.0))
    extreme_ids = {
        it["id"] for it in items
        if float(it.get("duration_budget") or 0) > 0
        and len(re.sub(r"\s", "", it.get("target") or "")) / float(it["duration_budget"]) >= rescue_min_cps
    }

    def produce() -> list[str]:
        work_dir.mkdir(parents=True, exist_ok=True)
        engine = load_engine(cfg, home)
        boost = max(1.0, min(float(cfg["tts"].get("tempo_boost") or 1.0), 1.5))
        base_speed = boost  # IndexTTS 的 base_factor=1/boost 变慢；这里 speed=boost 变快
        entries: list[dict] = []
        outputs = [MANIFEST_REL]
        failed = 0

        for item in todo:
            seg_id = item["id"]
            speaker = item.get("speaker") or "SPK_01"
            text = item["target"].strip()
            budget = float(item.get("duration_budget") or 0.0)
            ref = refs.get(speaker, fallback_ref)
            # 参考文本必须与参考音频内容一致：优先用 build_gsv_ref 写出的 .txt
            prompt = prompts.get(speaker)
            if prompt is None:
                saved = (refs.get(speaker) or fallback_ref).with_suffix(".txt")
                prompt = saved.read_text(encoding="utf-8").strip() if saved.exists() else ""

            out_rel = f"06_tts/{speaker}/{seg_id}.wav"
            out_path = project_dir / out_rel

            try:
                dur = synthesize(engine, home, ref, prompt, text, out_path, base_speed)
                speed_used = base_speed

                # 极端段救援（与 IndexTTS 版同口径）：估算字/秒超标不压预算，
                # 按真实时间窗（借空档/叠话）定目标；manifest 记 fit_dur/rescue
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
                    print(f"[tts] {seg_id} 救援[{rescue}]：预算 {budget:.2f}s → 目标 "
                          f"{fit:.2f}s（实测 {dur:.2f}s）")

                # 超目标：提速重生成（与 IndexTTS 版的 duration_factor 压缩对应）
                if fit > 0 and dur > fit * float(cfg["align"]["max_speed"]):
                    new_speed = min(2.0, speed_used * dur / max(fit, 0.1))
                    if new_speed > speed_used + 0.01:
                        retry = out_path.with_suffix(".retry.wav")
                        dur2 = synthesize(engine, home, ref, prompt, text, retry, new_speed)
                        if dur2 < dur:
                            retry.replace(out_path)
                            dur, speed_used = dur2, new_speed
                        else:
                            retry.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001 - 单段失败不拖垮整链路
                failed += 1
                print(f"[tts] {seg_id} 合成失败：{type(exc).__name__}: {exc}")
                entries.append({
                    "id": seg_id, "speaker": speaker, "file": None,
                    "sr": sample_rate, "target_dur": round(budget, 3),
                    "actual_dur": 0.0, "duration_factor": round(1.0 / base_speed, 3),
                    "status": "failed",
                })
                continue

            # atempo 硬性贴合兜底（与 IndexTTS 版一致）：重生成后仍超目标时
            # 直接变速压进目标（普通段=预算；救援段=窗口/叠话目标）。
            # tts.atempo_fallback=false 可关闭
            if fit > 0 and dur > fit and bool(cfg["tts"].get("atempo_fallback", True)):
                fitted = out_path.with_suffix(".fit.wav")
                tempo = dur / fit
                if audio.tempo_fit(out_path, fitted, tempo):
                    dur_fit = audio.probe_duration(fitted)
                    if dur_fit <= fit + 0.05:
                        fitted.replace(out_path)
                        dur = dur_fit
                        print(f"[tts] {seg_id} atempo 兜底：x{tempo:.2f} -> {dur_fit:.2f}s")

            entry = {
                "id": seg_id, "speaker": speaker, "file": out_rel,
                "sr": sample_rate, "target_dur": round(budget, 3),
                "actual_dur": round(dur, 3), "duration_factor": round(1.0 / speed_used, 3),
                "status": classify(dur, fit, cfg),
                "engine": "gpt_sovits",
            }
            if rescue:
                entry["fit_dur"] = round(fit, 3)
                entry["rescue"] = rescue
                if pre_borrow > 0:
                    entry["pre_borrow"] = round(pre_borrow, 3)
            entries.append(entry)
            outputs.append(out_rel)
            extra = f" → 目标 {fit:.2f}s[{rescue}]" if rescue else ""
            print(f"[tts] {seg_id} {speaker}: {dur:.2f}s / 预算 {budget:.2f}s{extra} -> {entry['status']}")

        for entry in existing.values():
            if entry["id"] not in {e["id"] for e in entries}:
                entries.append(entry)
        entries.sort(key=lambda e: e["id"])
        save_json(manifest_path, {"segments": entries, "engine": "gpt_sovits"})
        if failed:
            print(f"[tts] 警告：{failed} 段合成失败（manifest 已标记 status=failed）")
        return outputs

    ran = run_stage(
        project_dir, STAGE, [TRANSLATION_REL, PROFILES_REL, SEGMENTS_REL], cfg, produce,
        config_keys=["tts", "align"], force=args.force,
        partial=bool(args.seg),
    )

    if ran or manifest_path.exists():
        manifest = load_json(manifest_path)
        ok = sum(1 for e in manifest["segments"] if e.get("status") != "failed")
        print(f"[tts] 完成 {ok}/{len(manifest['segments'])} 段（引擎 GPT-SoVITS v2final）")


if __name__ == "__main__":
    main()
