"""项目配置：默认值、读写 project.json、配置哈希。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

DEFAULT_CONFIG: dict[str, Any] = {
    "source_lang": "en",
    "target_lang": "zh",
    # 单主播模式（s04）："auto"=wespeaker 轻量预判，单人跳过 pyannote 省显存；
    # "on"=强制单人；"off"=强制完整分离。老项目的布尔值自动归一化
    "single_speaker": "auto",
    "audio": {
        "master_sample_rate": 48000,
        "asr_sample_rate": 16000,
    },
    "separation": {
        "model": "htdemucs_ft",
        "device": "cuda",
    },
    "denoise": {
        "enabled": False,
    },
    "asr": {
        "model": "large-v3",
        "compute_type": "int8",
        "device": "cuda",
        "batch_size": 8,
        # 超过该秒数的段按词间停顿切成子段（0 = 不切分）。
        # 长段中文配音远短于原声时，段尾会留下大块静音（原声人声已被分离丢弃）
        "max_segment_seconds": 10.0,
    },
    "diarization": {
        # auto：有 HF token 走 pyannote，否则用本地声纹聚类；也可强制 pyannote / embedding / none
        "method": "auto",
        "model": "pyannote/speaker-diarization-3.1",
        "embedding_model": "pyannote/wespeaker-voxceleb-resnet34-LM",
        "min_speakers": None,
        "max_speakers": None,
        "clustering_threshold": 0.7,
        "min_embed_seconds": 0.8,
        # 过拆收敛（s04）：段数占比 ≤25% 的碎片类与最近邻类中心余弦 ≥ 该值
        # 才并入最近邻（实测同人 ≥0.63 / 真异人 ≤0.13）；≤0 关闭
        "merge_sim": 0.60,
    },
    "translation": {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "base_url": None,
        "api_key": None,
        # 译文长度预算系数（>1 允许略超每秒字数估算）。2026-10-20 从 1.15 收紧到
        # 1.10：宁短勿长——超预算段会流到 s07 触发整段重合成甚至 review 人工精简，
        # 收紧后首轮译文更少漏网（BCS 实测 review 29 段，多为压一轮仍超的病态句）
        "max_length_ratio": 1.10,
        # 人名表（仅 API 后端）：s05 先用一次 LLM 调用从全片对白提取人名/专有
        # 名词并定统一译名（落 04_translate/glossary.json，重跑复用），再注入
        # 翻译与压缩轮的 system prompt，解决 LLM 翻译的人名漂移（如 Saul
        # Goodman 被译成"索尔·戈登"）。false 关闭
        "glossary": True,
        "chars_per_second": 4.5,
        "batch_size": 20,
        "timeout": 120,
        "fallback_local": True,
        "local_model": "Helsinki-NLP/opus-mt-en-zh",
    },
    "tts": {
        # indextts2：IndexTTS-2.5（默认，env_tts 环境）——本机首选
        # gpt_sovits：GPT-SoVITS v2final（独立部署 + venv，见 tts.gpt_sovits_home；
        #              s07 自动 reexec 过去执行，产物格式与 IndexTTS 版一致）
        # siliconflow：硅基流动 CosyVoice2 API（云端备选，F-1）——显卡被占/
        #              无卡时用；消耗账户余额且参考音频上传云端；2026-10-21
        #              实测通过（WebUI 配置页可一键测连通性）
        "engine": "indextts2",
        # GPT-SoVITS 部署根（含 GPT_SoVITS/ 代码与 venv/）；engine=gpt_sovits 时必填
        "gpt_sovits_home": r"D:\GPT-SoVITS",
        # 硅基流动 CosyVoice2 API（engine=siliconflow 时生效）：
        # api_key 缺省回退 translation.api_key / 环境变量 SILICONFLOW_API_KEY
        "siliconflow": {
            "base_url": "https://api.siliconflow.cn/v1",
            "api_key": None,
            "model": "FunAudioLLM/CosyVoice2-0.5B",
        },
        "device": "cuda",
        "sample_rate": 24000,
        "ref_seconds": 12.0,
        "min_ref_seconds": 1.5,
        "use_qwen_emo": False,
        # ref: 语气跟随参考音频（原声对白）；text: 用 QwenEmotion 从中文译文
        # 分析 8 维情感向量引导语气（音色仍来自参考音频），语气更贴合台词内容。
        # 默认 ref（2026-10-20 BCS 真实影片试听定版）：text 在正片上语气偏平缓
        # （逐句判 calm 居多），ref 保留原声对白的表演张力，更像"演员在演"。
        # 注意：QwenEmotion 为 IndexTTS 专有能力，engine=gpt_sovits/siliconflow 时被忽略
        "emo_mode": "ref",
        # 语速提升系数（合成时用 duration_factor 原生提速，无变速失真）。
        # IndexTTS 默认朗读腔约 4 字/秒，电影对白约 5.5 字/秒；
        # 1.15 即让配音带电影对白的语速密度，译文短于原声时尾部自然留白作句间间隔
        "tempo_boost": 1.0,
        # 超预算兜底（s07）：重生成后仍超预算的段，用 ffmpeg atempo 后处理
        # 硬性贴合预算上限（轻微变速失真 < 叠话/错位）。false 恢复旧行为
        "atempo_fallback": True,
        # 参考音频质量预检（s06，H-4）：GSV 对参考音频质量敏感（BGM 残留
        # 毁音色锚点）。ASR 置信度低于 ref_min_confidence、或该窗口 BGM 能量
        # 占比（no_vocals/vocals 干声 RMS 比）高于 ref_max_bg_ratio 的段
        # 判为不合格——自动选段时垫底，仅在没有合格段时兜底使用并警告
        "ref_min_confidence": 0.30,
        "ref_max_bg_ratio": 2.5,
        # 参考音频过短的告警线（2026-10-22 A/D 实测）：<3s 的参考音色锚点不稳，
        # 相似度明显偏低（GSV 更是硬性要求 3~10s）。低于该值只告警不阻塞——
        # 有些受访者全片只有一两句，宁可用短参考也别借用别人的音色
        "ref_short_warn_seconds": 3.0,
        # 参考段是否跨时间轴分散抽样（2026-10-22）：同一说话人在片头/片中/
        # 片尾的语气与录音条件不同，只取「最长段 + 凑够就停」会把音色锚在
        # 单一场景（bcs_12min 的 SPK_04 占 63% 段，12s 参考全落在中段
        # 277~565s，片尾 150s 无覆盖，相似度只有 0.13）。
        # true = 按预计段数把时间轴等分，每段各取最优；false = 旧行为
        "ref_spread": True,
        # 单段合成耗时告警线（秒）。长片云端排队会从 2s/段恶化到 20s+/段，
        # 超过该值持续出现时打印告警并给切引擎/分批的建议（不中断）
        "slow_segment_seconds": 20.0,
    },
    "align": {
        # 只加速不放慢：偏短段落保持合成语速，尾部留白作句间间隔
        "max_speed": 1.15,
        "max_gap_borrow": 0.4,
        # 极端段救援（2026-10-21，用户提意）：估算字/秒 ≥ rescue_min_cps 的段
        # 硬压预算会快到听不懂，改为「借空档」——用时间轴真实窗口（下一句起点
        # 前）做拟合目标，能多慢多慢。借为主、叠话兜底（2026-10-21 下午修订，
        # 用户反馈叠话有明显听感不适）：纯借阶段只要总变速不超
        # rescue_speed_max、实际字/秒不超 rescue_max_cps 就不叠话；
        # 只有借满窗口仍超上限才允许伸进下一句，且只伸「让总变速回落到
        # 上限」所需的最小量（事实对白常有抢话），叠话量有上限并做尾淡出
        "rescue_min_cps": 7.0,    # 触发：估算字/秒（字数/预算）≥ 该值走救援
        "rescue_max_cps": 7.0,    # 可懂度闸门：借满窗口后实际字/秒 ≤ 该值才不叠话
        "gap_buffer": 0.10,        # 借空档目标距下一句起点保留的静音
        "pre_borrow_max": 0.25,    # 向前借用上限（提前起播，仅救援段）
        "rescue_max_ratio": 2.5,   # 借空档目标不超过预算的该倍率（防无限拖沓）
        "rescue_speed_max": 2.2,   # 纯借允许的总变速上限（相对自然合成）；
                                  #   超过才叠话。IndexTTS 自然合成偏慢，
          # 2.2x ≈ 中文 4~5 字/秒，日常语速听感
        "overlap_max": 1.2,        # 单段叠话时长上限（秒）
        "overlap_next_ratio": 0.5, # 叠话不超过下一句自身窗口的比例
        "overlap_min": 0.30,       # 低于该值的叠话不做，仍按窗口压
    },
    "mix": {
        "background_gain_db": -3.0,
        "duck_db": -6.0,
        "target_lufs": -23.0,
    },
    "qc": {
        # 声纹相似度阈值（配音 vs 原声 音色余弦中位）。
        # 2026-10-22 三引擎实测：同人跨语言 0.20~0.34，不同人 ≤0.12，
        # 0.18 是两者中间偏保守的线。**这是跨引擎通用线，不要按引擎各设一套**
        # ——5 分钟素材上 IndexTTS 与 CosyVoice2 同为 0.22，差异来自素材而非引擎
        "voice_similarity_min": 0.18,
        # 同一项目混用多个 TTS 引擎时视为 WARN（音色会段间摆动，不适合交付）
        "allow_mixed_engine": False,
        # 参考音频短于该值时在 qc 里点名（与 tts.ref_short_warn_seconds 同口径）
        "ref_short_warn_seconds": 3.0,
    },
    "subtitle": {
        # false: 只生成 .srt（现状）；true: 烧录进画面（视频需重编码，耗时显著增加）
        "burn_in": False,
        "font": "Microsoft YaHei",
        "font_size": 26,
        "margin_v": 40,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_project(project_dir) -> dict:
    path = Path(project_dir) / "project.json"
    if not path.exists():
        raise FileNotFoundError(f"未找到 project.json: {path}")
    # utf-8-sig 兼容带 BOM 的文件（记事本旧版 / PowerShell 5 Set-Content 会写入 BOM）
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    data["config"] = _deep_merge(DEFAULT_CONFIG, data.get("config", {}))
    return data


def save_project(project_dir, data: dict) -> None:
    path = Path(project_dir) / "project.json"
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def config_hash(config: dict) -> str:
    blob = json.dumps(config, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:8]


def config_subset(config: dict, keys: Iterable[str]) -> dict:
    """取出配置中指定的顶层键，用于按阶段计算配置哈希。

    每个阶段只应关心自己用到的配置段，否则改动任一配置都会让整条链路重算。
    """
    missing = [k for k in keys if k not in config]
    if missing:
        raise KeyError(f"配置中缺少键: {', '.join(missing)}")
    return {k: config[k] for k in keys}
