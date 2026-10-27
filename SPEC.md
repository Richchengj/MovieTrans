# 英文电影 → 中文原生配音 · 工程规范

> 本文件定义目录结构、中间产物格式、命名约定与流水线机制。
> 目标是：每一环可单独重跑、人工修改永不被覆盖、输入未变则自动跳过。

---

## 0. 目标与范围

- 输入：英文电影（或片段）视频文件
- 输出：中文原生配音的成片（非字幕）
- 首期范围：2–3 分钟片花，单人/少人、无重叠对白
- 运行环境：本地 GPU，12 GB 显存
- 不做：唇形同步（lip-sync）

---

## 1. 流水线总览

| 阶段 | 脚本 | 说明 |
|---|---|---|
| 1 | `s01_extract_audio.py` | 拆音轨（ffmpeg） |
| 2 | `s02_separate.py` | 对白 / 背景分离（Demucs） |
| 3 | `s03_denoise.py` | 降噪去混响（可选） |
| 4 | `s04_asr.py` | ASR + 词级时间戳 + 说话人分离（WhisperX + pyannote） |
| 5 | `s05_translate.py` | 长度受控翻译（LLM） |
| 6 | `s06_voice_profile.py` | 建立每角色音色档案 |
| 7 | `s07_tts.py` | 克隆 TTS + 时长控制（引擎可选，见 §5.1） |
| 8 | `s08_align.py` | 时间轴微调（rubberband） |
| 9 | `s09_mix.py` | 混音 + 响度归一化（ffmpeg） |
| 10 | `s10_assemble.py` | 装配成片（ffmpeg） |

---

## 2. 目录结构

```
D:\MovieTrans\
├── SPEC.md                       # 本规范
├── README.md                     # 使用说明
├── projects\
│   ├── _template\                # 新项目模板（勿直接使用）
│   └── <project_id>\             # project_id = YYYYMMDD_<片段名>
│       ├── project.json          # 项目元数据 + 全局配置
│       ├── state.json            # 流水线状态（断点续跑用）
│       ├── 00_source\            # L0 原始素材，只读
│       ├── 01_audio\             # 阶段1 音轨
│       ├── 02_stems\             # 阶段2/3 分离与降噪
│       ├── 03_asr\               # 阶段4 识别
│       ├── 04_translate\         # 阶段5 翻译
│       ├── 05_voices\            # 阶段6 音色档案（每角色一个子目录）
│       ├── 06_tts\               # 阶段7 TTS
│       ├── 07_align\             # 阶段8 对齐
│       ├── 08_mix\               # 阶段9 混音
│       ├── 09_output\            # 阶段10 交付物
│       ├── work\                 # 临时缓存，可随时清空
│       └── logs\                 # 每阶段一个 .log
├── models\                       # 模型权重，跨项目共享
├── envs\                         # 每个模型的独立 Python 环境
├── scripts\                      # 流水线脚本
│   ├── pipeline.py               # 统一入口
│   ├── new_project.py            # 新建项目
│   ├── s01_...py ~ s10_...py     # 各阶段脚本
│   └── lib\                      # 公共库
└── tools\                        # ffmpeg / rubberband 等第三方可执行文件
```

---

## 3. 产物分级

| 级别 | 位置 | 说明 | 覆盖规则 |
|---|---|---|---|
| **L0 原始** | `00_source/` | 输入素材 | 永不修改 |
| **L1 机器产物** | `*.json` / `*.wav` | 脚本输出 | 可被重跑覆盖 |
| **L2 人工编辑** | `*.edited.json` / `review.csv` | 人工校对 | **脚本只读，绝不覆盖** |
| **L3 交付物** | `09_output/` | 成片 | 仅由最后一步生成 |

下游统一读「**编辑层优先**」：存在 `X.edited.json` 就用它，否则用 `X.json`。

---

## 4. 核心中间产物 Schema

### 4.1 `03_asr/segments.json` — 时间轴真相

```json
{
  "project_id": "20261008_demo_clip",
  "source_lang": "en",
  "audio": "01_audio/audio_asr.wav",
  "speakers": ["SPK_01", "SPK_02"],
  "segments": [
    {
      "id": "seg_0001",
      "speaker": "SPK_01",
      "start": 12.34,
      "end": 15.02,
      "text": "You have to understand what's at stake here.",
      "words": [{ "w": "You", "start": 12.34, "end": 12.52 }],
      "confidence": 0.93,
      "flags": []
    }
  ]
}
```

### 4.2 `03_asr/speakers.json` — 说话人映射

```json
{
  "SPK_01": { "character": "男主", "ref_segments": ["seg_0001"], "notes": "" },
  "SPK_02": { "character": "女主", "ref_segments": ["seg_0012"], "notes": "" }
}
```

> **单主播模式**（`project.json` 顶层 `"single_speaker": true`，WebUI 创建/配置页可勾选）：
> 全片只有一个说话人的素材（口播/讲师/独白）开启后，s04 跳过全部分离路径
> （省 pyannote 约 4GB 显存与数分钟耗时），所有段直接归 `SPK_01`，s06 只建一份
> 音色档案、s07 全程复用同一参考音频——即「只克隆一次」。多人影片切勿开启
> （多角色会被并成同一个音色）。

> **过拆自动收敛**（s04 默认开启，`diarization.merge_sim` 可调，≤0 关闭）：
> 急速轮替对白中 pyannote 会把同一人的极短感叹拆成第三个说话人（D_fast 夹具
> 实测：0.5–1.5s 轮替 12 句判对 10）。s04 分离出口统一做「碎片类合并」后处理：
> 段数占比 ≤25% 的碎片类，与最近邻类中心余弦 ≥ `merge_sim`（默认 0.60，
> 实测同人 ≥0.63 / 真异人 ≤0.13，判别带宽）才并入最近邻；两个大类的相互合并
> 永远不会发生，也不会合并到 `min_speakers` 以下。合并动作打印在 s04 日志
> （`[diar] 过拆收敛：…`），R1 复核时可核对。

> **参考音频质量预检**（s06，`tts.ref_min_confidence` / `tts.ref_max_bg_ratio`，
> 置 0 关闭）：GSV 对参考音频质量敏感（BGM 残留会毁音色锚点）。s06 给候选段
> 算两项指标——ASR 置信度、BGM 能量占比（no_vocals/vocals 干声 RMS 比）；
> 不合格的段自动选段时垫底（仅无合格段可替时兜底使用并警告），人工指定的
> ref_segments 始终尊重（只警告）。结果记入 `profiles.json` 的 `ref_quality`
> （min_confidence / max_bg_ratio / all_qualified），R1/R3 试听时可参考。

> **参考段跨时间轴分散**（s06，`tts.ref_spread` 默认 true，2026-10-22）：
> 同一说话人在片头/中/尾的语气与录音条件不同，而旧逻辑「最长优先 + 凑够
> `ref_seconds` 就停」会把 12s 参考全压在某个场景（bcs_12min 的 SPK_04：
> 戏份 127~715s，参考只覆盖 277~565s，片尾 150s 一句没取到）。现按
> `_bins_for`（预计要几段才凑够 ref_seconds）把该说话人的段序等分，每个
> 等分段各取最优一段。**实测影响在噪声内**（三组对照中位 0.236/0.234/0.241），
> 属防御性改动；`tts.ref_short_warn_seconds`（默认 3s）对过短参考只告警，
> 因为素材不足时短参考仍好过借用别人的音色。

> **GSV 参考音频 3~10s 硬校验**（2026-10-21 修复）：GPT-SoVITS inference_webui
> 对参考音频强制 3~10 秒范围（超出直接报"参考音频在3~10秒范围外"），而 s06 的
> reference.wav 默认拼 12s——`engine=gpt_sovits` 时整条管线会全灭。s07_tts_gsv
> 现自动把超长参考裁到 9.9s（落 `work/tts/gsv_refs/<SPK>_clip10.wav`，正好保留
> 首段参考与 prompt_text 对应的音频）。注意：GSV 参考"文音匹配"很重要，拼接参考
> 的 prompt_text 只对应首段。

> **人名表（`translation.glossary`，默认开，仅 API 后端）**：s05 翻译前先用
> 一次 LLM 调用从全片对白提取人名/专有名词并定统一译名，落
> `04_translate/glossary.json`（重跑复用），译名表硬约束注入翻译与压缩轮的
> system prompt。解决 LLM 翻译的人名漂移（同一 Saul Goodman 被译成
> "索尔·古德曼"/"索尔·戈登"两种）。配 `work/fix_names.py` 可对已完成项目
> 事后替换译名并输出受影响段清单（配 `--seg` 精确重合成）。

### 4.3 `04_translate/translation.json` — 译文 + 时长预算

```json
{
  "segments": [
    {
      "id": "seg_0001",
      "speaker": "SPK_01",
      "source": "You have to understand what's at stake here.",
      "target": "你得明白现在赌上的是什么。",
      "duration_budget": 2.68,
      "target_est_duration": 2.55,
      "strategy": "native",
      "notes": ""
    }
  ]
}
```

`strategy` 取值：`native`（正常）｜`compress`（译文需压缩）｜`review`（需人工介入）。

### 4.4 `06_tts/manifest.json` — TTS 结果

```json
{
  "segments": [
    {
      "id": "seg_0001",
      "speaker": "SPK_01",
      "file": "SPK_01/seg_0001.wav",
      "sr": 24000,
      "target_dur": 2.68,
      "actual_dur": 2.61,
      "status": "ok"
    }
  ]
}
```

### 4.5 `07_align/align_report.json` — 对齐报告

```json
{
  "segments": [
    {
      "id": "seg_0001",
      "target_dur": 2.68,
      "input_dur": 2.61,
      "speed": 1.027,
      "borrowed": 0.0,
      "flags": []
    }
  ]
}
```

### 4.6 `state.json` — 流水线状态

```json
{
  "project_id": "20261008_demo_clip",
  "stages": {
    "s01_extract_audio": {
      "status": "done",
      "finished": "2026-10-08T12:00:00",
      "input_fingerprints": { "00_source/source.mp4": "12345:6789" },
      "config_hash": "ab12cd34",
      "outputs": ["01_audio/audio_master.wav", "01_audio/audio_asr.wav"]
    }
  }
}
```

---

## 5. 三个关键机制

### 5.1 时长预算算法

```
base        = seg.end - seg.start
borrow_max  = align.max_gap_borrow (默认 0.4s)，前后各取一半，且不超过实际静音间隙
budget      = base + 实际可借用间隙
变速比      = max(actual_dur / budget, 1.0)   # 只加速不放慢
若变速比超出 max_speed（默认 1.15）
    → 标记 flags:["overrun"]，交由人工复审
配音短于预算时保持合成语速，尾部留白自然成为与下一句的间隔
（放慢会抵消 s07 的原生提速并吃掉句间留白，故不采用）
```

**预算借用口径（2026-10-20 修正）**：相邻判断必须用**全局时间轴**（任意说话人
的相邻段），而不是同说话人内取邻——多人交替对白时同说话人的"下一句"之间隔着
他人对白，把别人的时间借进预算就会撞进对方台词。s07 的 atempo 兜底目标也从
`budget × max_speed` 收紧为 `budget` 本身（预算已含硬窗，余量会重新吃掉下一段
起点）；atempo 超过单次上限 2.0 时链式拆级（如 x2.91 = 1.71×1.71）。
lib/budget.py 对任意输入保证 budget ≤ 与下一段的硬窗。

**逐段响度贴回（s08，2026-10-11）**：配音段电平自动对齐原演员该句电平——
gain = 原声段 RMS − 配音段 RMS（vocals_clean 对应区间），夹 ±8dB；
不可测段（原声过短/被残留污染）用该说话人增益中位数兜底。段级匹配，
段内 TTS 动态原样保留；s09 全局 loudnorm（-23 LUFS）只管交付响度。
QC 新增「响度贴合」项核验（中位 ±3dB 内为佳）。

**字幕装配（s10，2026-10-11）**：默认只生成 .srt；`subtitle.burn_in` 开启时
用 subtitles 滤镜把中文字幕烧进画面（字体/字号/底边距可配，视频需重编码）。
s10 会检测源视频的内封字幕流并在日志提示；源画面已有硬字幕时建议关闭烧录。

**碰撞自动消解（s08，2026-10-11）**：按 `max_speed` 变速后仍会压到下一句起点
0.15s 以上的段，s08 自动在 1.30 上限内再提速消除碰撞（短句略快，听感自然）；
修复后不再标 overrun。1.30 也救不回来的才保留 overrun 交人工。
质检（qc.py）按最终铺轨判定时间轴碰撞，容差 0.25s。

**极端段救援：借空档 / 叠话（s07+s08，2026-10-21，用户提意）**：预算的借用上限
只有 `max_gap_borrow`（0.4s），而时间轴上的真实空档往往大得多（zero2 实测
seg_0062 窗口 4.27s vs 预算 0.93s）——估算字/秒（字数/预算）≥ `align.rescue_min_cps`
（默认 7.0，中文日常 5~6）的段若继续硬压预算，语速会到 7~9 字/秒（zero2 实测
最高 8.8，2 段 >8.5），"快但听不懂"。此类段不贴预算，改贴 `lib/budget.py` 算出的
真实窗口目标：

```
触发     est_cps = 字数/预算 ≥ rescue_min_cps（s07 合成前即可判定）
窗口     window = 下一句起点 - gap_buffer(0.10) - 本段起点 (+ pre_borrow)
          pre_borrow = min(本段起点-上一句终点-0.05, pre_borrow_max(0.25))
          （上一句同为极端段时不前借，防三重重叠；仅救援段前借）
① 借空档  target = min(window, 自然时长, 预算 × rescue_max_ratio(2.5))
            → s07 按 target 重生成（duration_factor = 当前 × target/实测），
              s08 按 pre_borrow 前移起点铺轨；只加速不放慢原则不变
            纯借闸门（2026-10-21 下午修订，**借为主、叠话兜底**）：
              总变速 dur/target ≤ rescue_speed_max(2.2) 且
              实际字/秒 ≤ rescue_max_cps(7.0) → 不叠话，窗口静音全用上
            （IndexTTS 自然合成偏慢，2.2x ≈ 中文 4~5 字/秒日常语速；
             相对倍率高 ≠ 听感快，以字/秒判可懂度）
② 叠话    仅当纯借总变速仍 > rescue_speed_max 时才伸进下一句：
            overlap = min(让总变速回落到上限所需量, overlap_max(1.2),
                          下一句窗口×0.5)
            ≥ overlap_min(0.30) 才做；s08 跳过碰撞消解，对越界部分做
            ≤0.8s 尾淡出（下一句清晰、本句自然收尾），qc 按计划量放行
```

manifest 记 `fit_dur`/`rescue`/`pre_borrow`，align_report 记 `rescue`/`overlap`；
qc 超预算判定改用 fit_dur（贴目标是设计行为）；WebUI 语速列悬停显示救援方式。
zero2 实测 6 段触发（seg_0098/0034/0118/0069/0062/0036），字/秒 8.8~7.1 →
2.3~3.2，全部回到自然区间；gsv 引擎变体同口径接入，siliconflow 复用 s07 主循环。
**2026-10-21 下午修订（用户试听反馈：叠话有明显听感不适）**：叠话判据从
「相对自然合成倍率 > 1.8」改为双闸门——纯借阶段总变速 ≤ 2.2 且字/秒 ≤ 7.0
就不叠话；叠话量从「补满自然时长」改为「让总变速回落到 2.2 所需的最小量」。
zero2 重测：叠话段 4 → 3（seg_0118 后档有 1.11s 空闲，转纯借），
总叠话量 4.4s → 2.6s；仅 seg_0098（前后档共 0.1s，无静音可借）保留 1.01s 叠话。

**语速原则（2026-10-10 定稿，2026-10-21 补窄例外）**：中文表达密度远高于英语，同一句意思中文本来就
更快。因此**绝不放慢配音去填满原声时长**——只加速、不放慢；译文比原声短时，
尾部自然留白成为句间停顿。对齐后 0.25s 以内的轻微重叠视为可接受
（qc.py 按时间轴碰撞而非死扣预算判定）。
**窄例外（s08 `gap_fill`，2026-10-21）**：译文明显短于原声（铺轨 < 0.80×原声
时长）且下一段起点前有充足空隙时，适度放慢（下限 0.85x，目标 0.92×原声时长，
不越过下一段起点）——不为凑时长，而是填掉"一句秒说完 + 数秒死寂"的突兀停顿
（zero2 实测 19 段留白 >1.5s，最大 8.4s，用户听感"特别长"）。flags 记
`gap_fill`，R1 可核对。

估算口径：s05 估算译文时长用**有效语速** = `chars_per_second × tts.tempo_boost`
（s07 合成时按 1/tempo_boost 的 duration_factor 原生提速，实际念白密度随之升高；
不乘会把放得下的译文误标成 compress/review）。API 翻译首轮超预算的句子会自动
追加**循环硬性压缩**（`_compress_overlong`，2026-10-21 从单轮改循环，最多 3 轮，
第 2 轮起提示加压"砍得更狠"；每轮仍超的采纳更短版本继续压）——单轮时超预算段
全靠 s07 atempo 硬压 x1.4~2.9，听感"说话特别快"（zero2 实测 43 段 review，
循环压缩后 review 降到 21，且全部译文变短）。仍超长的保持 review 交人工。

> **超预算三层防线**（2026-10-20 起）：① s05 提示词给 `max_chars`（=
> budget × cps × `translation.max_length_ratio`，默认 1.10，宁短勿长，收紧
> 后首轮更少漏网）；② s05 **循环**压缩补轮（2026-10-21 从单轮改循环，最多
> 3 轮，逐轮加压，仍超的采纳更短版本继续压——zero2 实测 review 43→21 且
> 全部译文变短）；③ s07 合成后超预算先按 `budget/dur` 重生成一次，仍超则
> `tts.atempo_fallback`（默认开）atempo 硬性贴合预算；预算×变速比仍压不进
> （如 0.65s 预算 1.73s 译文，需 x2.91）时链式拆级贴合。pipeline 在 s07 前
> 会打印 review 段数提醒先做 R1 精简（TTS 占全链路 ~70%，废合成是最大浪费源）。
> **时长基准**：s07 合成后先 `trim_silence`（audio.py，-45dB 阈值、两端留
> 0.15s）再测时长——TTS 输出普遍带 0.3~1s 头尾静音，不 trim 会把"真的吗"
> （有效语音 1.0s）测成 2.39s，兜底倍率虚高到 x3+（zero2 实测 x4.22→x2.39）。
> **零干预实测（2026-10-21）**：三层防线全开 + 预算硬窗后，5.9 分钟 4 人
> 对白全自动跑通 qc 11/11（review 段 0、碰撞 0）。

> **语气模式**（`tts.emo_mode`，WebUI 配置页可调，仅 IndexTTS 生效）：
> `ref`（默认，2026-10-20 BCS 真实影片试听定版）跟随参考音频的情绪，保留原声
> 对白的表演张力；`text` 用 QwenEmotion 从中文译文逐句分析 8 维情感向量引导
> 语气——合成夹具 A/B 时听感更好，但 BCS 正片实测正剧对白多被判 calm，情绪
> 偏平缓、缺少"演员在演"的张力，故降为选项。engine=gpt_sovits/siliconflow
> 时该配置被忽略（这两者无 QwenEmotion 情感引导能力）。

**TTS 引擎分发（s07，2026-10-21 云端备选实测通过）**：`tts.engine` 三选一，
WebUI 配置页可直接切换并测连通性——① `indextts2`（默认，本机主力，env_tts）；
② `gpt_sovits`（本机备选，相似度上限更高，s07 自动 reexec 到其独立 venv）；
③ `siliconflow`（云端备选，硅基流动 CosyVoice2-0.5B API，0 显存、按量计费
约 ¥1.5–3/万汉字，参考音频上传云端）。三引擎产物格式一致（24k 单声道 wav），
s05 预算 / s08 碰撞消解与响度贴回均作用在音频产物上，与引擎无关；极端段
救援（借空档/叠话）对三引擎同口径生效——siliconflow 自然语速偏慢，双闸门
判据（总变速 ≤2.2 且字/秒 ≤7）会自动让它多走叠话分支。云端引擎首次使用
前建议点 WebUI 的「测试连通性」（用项目参考音频端到端合成一句，费用可忽略，
音色 URI 缓存正式跑时直接复用）。

### 5.2 断点续跑

每阶段记录 `input_fingerprints`（文件大小 + 修改时间）与 `config_hash`。
两者均未变化且产物齐全 → 自动跳过。`--force` 可强制重跑。

`config_hash` **按阶段隔离**：每个阶段只哈希自己声明依赖的顶层配置段
（如 s01 只认 `audio`，s02 认 `audio` + `separation`），
避免改动任一配置就让整条链路重算。依赖的配置段名记在 state.json 的 `config_keys` 里。

### 5.3 单段调试

支持 `--seg seg_0001`，只重跑指定段落的 TTS / 对齐，不必跑整条链路。

### 5.4 长段切分（s04）

WhisperX 会把连播的讲解聚成 25–30s 的长段。中文配音通常只有原声段的一半甚至
1/3 长（且原声人声已被 Demucs 分离丢弃），配音铺在段头后，段尾会剩下十几秒的
纯静音——成片听感就是“这里被切掉了”。

s04 落盘前把超长段切成子段，三步策略（`asr.max_segment_seconds`，默认 10s，0 = 关闭）：

1. **大停顿天然分界**：词间 gap ≥ 0.8s（如原声的演奏/停顿区）直接作为段边界，
   配音不横跨长静音；
2. **碎块合并**：时长 < 3s 的块按“跨小间隙优先”合并进邻居，避免翻译上下文过碎
   （s05 是逐段翻译的）；
3. **均匀细分**：仍超长的块按词间停顿均匀选切点（gap 封顶 2.0 + 句末标点加权，
   防止一个巨型 gap 独裁切点选择），单段下限 1s。

切分后全链路（翻译 / TTS / 铺轨）自动按子段工作，长静音变成自然分散的句间间隔。

---

## 6. 命名与格式约定

| 项 | 约定 |
|---|---|
| 项目 ID | `YYYYMMDD_<片段名>` |
| 段落 ID | `seg_XXXX`（零填充 4 位，跨阶段不变） |
| 说话人 ID | `SPK_XX`（零填充 2 位，跨阶段不变） |
| 时间单位 | 秒，浮点，保留 3 位小数 |
| 文本编码 | UTF-8（无 BOM） |
| 音频 | `.wav` / PCM；ASR 16k 单声道，其余 48k |
| 日志 | 每阶段一个 `logs/<stage>.log` |

---

## 7. 人工复核卡点

| 卡点 | 看什么 | 通过标准 |
|---|---|---|
| R1 说话人 | `03_asr/speakers.json` + 各 `05_voices/*/preview.wav` | 无一人被拆成多人 |
| R2 译文 | `04_translate/review.csv` | 意思准确，长度偏差 < 20% |
| R3 克隆 | `05_voices/*/preview.wav` | 能听出是同一演员 |
| R4 混音 | `08_mix/mix.wav` | 无双重人声，响度达标 |

### 7.1 自动质检（qc.py）

人工复核前先跑机器质检，13 项检查不过的不要进入复核：

```powershell
python scripts/qc.py projects/<项目>            # 逐项 PASS/FAIL/WARN
python scripts/qc.py projects/<项目> --json     # JSON 输出（退出码 0=无 FAIL）
```

检查项：阶段状态全 done、成片时长与源一致（±0.5s）、成片含视频/音轨、
mix.wav 响度接近 `mix.target_lufs`（±3 LU）、配音覆盖率 ≥95%、
时间轴碰撞（配音尾部压到下一句起点 0.25s 以上才算失败；计划内叠话按
align_report 的 overlap 量放行；超预算但仍留人工复核的只列出不阻塞）、
字幕条数/单调/空文本、翻译空译文与策略分布、
间隙残留电平（段间隙 vs 段内 ≥6dB，双重人声预警）、
逐段响度贴合（配音-原声 ≤3dB）、声纹相似度（配音 vs 原声音色余弦 ≥
`qc.voice_similarity_min`，默认 0.18）、参考音频（每个说话人都要有参考，
过短的点名）、引擎一致性（混用多个 TTS 引擎 → WARN）。

> **声纹按说话人拆开报（2026-10-22，I-9）**：池化中位会被段数最多的角色
> 主导（bcs_12min 的 SPK_04 占 63% 段、中位 0.13，其余 5 人 0.21~0.31，
> 池化中位 0.147）。判定仍用池化中位（不因换算法放宽），但 detail 输出
> 每个说话人的中位与段数占比，并点名差分者、给出「换参考音频」的具体指路。
> 阈值是**跨引擎通用线**：5 分钟素材上 IndexTTS 与 CosyVoice2 同为 0.22，
> 差异来自素材（说话人分布、参考段位置）而非引擎，不要按引擎各设一套。

> **参考音频抽样（2026-10-22，I-9）**：s06 自动选段默认走 `tts.ref_spread`
> （按预计段数把说话人的时间轴等分，每段各取最优），避免「最长优先 + 凑够就停」
> 把 12s 参考全压在同一个场景。实测三组对照（current/dense/spread）差异在 ±0.01
> 噪声内，属防御性改动；`tts.ref_short_warn_seconds`(3s) 对过短参考只告警不阻塞。

> **三态语义（2026-10-22，I-1）**：PASS / FAIL / **WARN**。WARN = 因环境缺依赖
> （如 env_base 缺 numpy 时声纹项）或计算异常而跳过——不计入通过数（防假绿），
> 但不影响退出码。数据性不适用（无语音视频无可测段）仍记 PASS。
> 声纹项需 pyannote/torch（env_asr）：`envs\env_asr\Scripts\python.exe scripts\qc.py ...`；
> WebUI 交付页「🔍 质检」按钮已自动优先选 env_asr，结果面板直接渲染。

> qc.py 主体只用标准库 + ffmpeg，可在任何环境跑；项目放在仓库外也能检（项目目录是参数）。

---

## 8. 选型定稿（12 GB 显存）

| 步骤 | 工具 | 显存 | 备注 |
|---|---|---|---|
| 1 拆音轨 | ffmpeg | CPU | |
| 2 分离 | Demucs `htdemucs_ft` | ~4 GB | 备选 UVR5 |
| 3 降噪 | DeepFilterNet | ~2 GB | 可选 |
| 4 ASR | WhisperX `large-v3` (int8) + pyannote 3.1 | ~6 GB | 分批处理 |
| 5 翻译 | LLM API（DeepSeek / GPT-4o） | 0 | 走 API 省显存 |
| 6 音色档案 | 纯 CPU | 0 | |
| 7 TTS | IndexTTS-2（主力）/ GPT-SoVITS（备选）/ 硅基流动 CosyVoice2 API（云端备选，0 显存） | ~8 GB | 逐段生成 |
| 8 对齐 | rubberband / sox | CPU | |
| 9 混音 | ffmpeg | CPU | |
| 10 装配 | ffmpeg | CPU | |

> **长音频分块（>5 分钟必看）**：Demucs 的 `apply_model` 会把 4 条 stems 一次性
> 物化在内存里。44.5 分钟片源单条 vocals 就要 3.8 GB，实测直接抛
> `DefaultCPUAllocator: not enough memory`（2026-10-22，44 分钟纪录片）。
> 因此 s02 对超过 `separation.chunk_seconds`（默认 300s）的输入按块推理、
> 逐块写盘拼接，**内存占用与片长无关**；块边界不重叠（htdemucs 内部本身是
> `split=True / overlap=0.25` 的分块推理，边界效应在几十毫秒级）。

**串行执行，峰值取单步最大值 ≈ 8 GB**，12 GB 显存有余量。

---

## 9. 环境约定

本机**未安装 conda**，采用 **Python venv + 每模型独立环境**：

```
envs\
├── env_asr\        # whisperx + pyannote
├── env_sep\        # demucs + deepfilternet
├── env_tts\        # indextts2 / gpt-sovits
└── env_base\       # 公共库（本项目 lib 与脚本）
```

> 不同模型的依赖冲突严重（torch / numpy / 音频库版本），**严禁混装**。

**ASCII 路径约束**：项目根目录含中文，而 `sentencepiece`（本地翻译分词器）、
`kaldifst`（`wetext`，IndexTTS 文本正则）、**torch 的 jit 序列化**（C++ 层窄字符
`fopen`，s04 加载 silero-vad 的 `silero_vad.jit` 时踩过）等 C++ 依赖用窄字符打开
文件，遇到非 ASCII 路径会直接报错。因此 `envs` 与 `models` 需各有一个 ASCII junction
（`C:\dub_envs` → `envs`、`C:\dub_torch` → `models\torch`），`lib/envs.py` 优先用
ASCII 路径启动各阶段（`torch_home()` 同理：`DUB_TORCH_DIR` → `C:\dub_torch` →
`C:\dub_models\torch` → 项目内 `models\torch`）。详见 [README.md](README.md)。

**torch.hub 离线缓存**：torch.hub 的部分加载路径**本地有缓存仍会联网 GitHub 校验**
（s04 的 wav2vec2 英文对齐权重、silero-vad 仓库都走 torch.hub；s02 的 demucs 权重
走 `load_state_dict_from_url` 缓存）。缓存目录 `models\torch\hub\`（silero 仓库 +
`checkpoints\` 下 4 个 demucs `.th` 与 wav2vec2 对齐 `.pth`，共约 715 MB）随包分发，
s02/s04 通过 `envs.torch_home()` 自动设置 `TORCH_HOME` 指向它。**验证离线必须断网跑
s04**，联网通着跑通不算数。

### 离线加固（2026-10-10，`lib/offline.py`）

两个实测踩过的静默联网点，已代码级钉死：

1. **torch.hub 缓存命中前先查 GitHub**：torch 2.x 的 `_parse_repo_info` 在查本地
   缓存**之前**先访问 `https://api.github.com` 解析默认分支，断网直接
   `RemoteDisconnected`（2026-10-10 长片段实踩）。s04 启动时调
   `patch_torch_hub_local()` 把 `snakers4/silero-vad` 改写为本地缓存目录 +
   `source='local'` 加载，全程不碰网络。
2. **HF 系联网校验**：s04 的 `configure_hf()` 置 `HF_HUB_OFFLINE=1` /
   `TRANSFORMERS_OFFLINE=1`，faster-whisper / pyannote / transformers 一律只认本地
   缓存（权重随包分发，运行期不该有下载；首次补权重的下载走 fetch_models.py）。

其余阶段：s02 已置 `HF_HUB_OFFLINE=1`；s05 本地 MarianMT 用
`local_files_only=True` 加载；s07 的 IndexTTS/BigVGAN/w2v-bert 全部本地目录加载；
s01/s03/s06/s08/s09/s10 纯 ffmpeg。**纯离线（provider=local + 无 HF token）全链路
已用防火墙阻断全部 python 出站流量实测跑通**，qc.py 8/8 通过
（测试项目 `E:\dub_projects\20261010_offline_blues`，2026-10-10）。
