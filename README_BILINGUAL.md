# MovieTrans · 英文电影 → 中文原生配音 / English Movies → Native Chinese Dubbing

> **写在前面 —— 一个 30 年前程序员的话 / Preface — words from a programmer of 30 years ago**
>
> 作为一个30年前的程序员，由于身体原因，我已经很久没有写代码了，我热爱代码，我曾经想过，可能这辈子也不可能继续写代码了。
>
> *I was a programmer 30 years ago. For health reasons I hadn't written code for a long time. I love code, and I once thought I might never write it again.*
>
> 这次所谓 Vibe Coding 的浪潮，让我又体会到了一次写代码的快乐，感谢这个时代吧。
>
> *This so-called Vibe Coding wave let me feel the joy of writing code once again. Thank you, this era.*
>
> 我只是想搞一个自己想用的小玩意，一个周末的时间就做出了第一个可以接受的版本；后来一路加到了十个阶段，能克隆原演员音色、能自动裁剪节奏，几乎没有支付任何费用。一个周末的开头，换来了几个月的乐趣。
>
> *I just wanted to build a little tool for myself. One weekend got it to the first acceptable version; since then it has grown to ten stages — cloning the original actors' voices, trimming pacing automatically — with almost no money paid. A weekend's beginning turned into months of fun.*
>
> 在使用时，我自己就是一个小白，什么都是从头摸索，和有没有编程经验毫无关系，从开发环境，依赖什么都让机器自己干，除了这段文字外，其它都是我来提需求做最终测试拍板。期间有过把思考模式开到最高，烧掉一亿Token，也有过被AI“蒙骗”。确实是一种新的体验。快乐已经不来自于代码本身，而是做一个有趣的小玩意。
>
> *Using it, I was a complete novice figuring everything out from scratch — prior coding experience was irrelevant. From the dev environment to dependencies, the machine did the work. Apart from this text, everything else was me stating requirements and doing final acceptance testing. There were moments of maxing out the thinking mode and burning 100 million tokens, and moments of being "fooled" by the AI. It's a genuinely new experience. The joy no longer comes from the code itself, but from building something fun.*

---

把英文电影配音成中文（非字幕），保留原演员音色。

*Dubs English movies into Chinese (not subtitles), preserving the original actors' voices.*

- 规范文档：[SPEC.md](SPEC.md) / *Spec document: [SPEC.md](SPEC.md)*
- 性能基准：[BENCHMARK.md](BENCHMARK.md)（实测耗时与估算公式） / *Performance benchmark: [BENCHMARK.md](BENCHMARK.md) (measured timings and estimation formulas)*
- 环境搭建：[envs/README.md](envs/README.md) / *Environment setup: [envs/README.md](envs/README.md)*
- **需要人工操作的事项**：[需要人工操作.md](需要人工操作.md)（GPU / HF token / API Key / 复核卡点） / *Manual steps: [需要人工操作.md](需要人工操作.md) (GPU / HF token / API key / review checkpoints)*

## 当前进度 / Current Progress

| 阶段 Stage | 脚本 Script | 状态 Status |
|---|---|---|
| 1 拆音轨 Extract audio | `s01_extract_audio.py` | ✅ 已实现 / done |
| 2 对白/背景分离 Dialogue/background separation | `s02_separate.py` | ✅ 已实现（含分离质量自检与回退） / done (with separation-quality check & fallback) |
| 3 降噪去混响 Denoise/de-reverb | `s03_denoise.py` | ✅ 已实现（可选，默认关闭） / done (optional, off by default) |
| 4 ASR+说话人分离 ASR + diarization | `s04_asr.py` | ✅ 已实现 / done |
| 5 长度受控翻译 Length-controlled translation | `s05_translate.py` | ✅ 已实现 / done |
| 6 音色档案 Voice profiles | `s06_voice_profile.py` | ✅ 已实现 / done |
| 7 克隆 TTS Cloned TTS | `s07_tts.py` | ✅ 已实现 / done |
| 8 时间轴对齐 Timeline alignment | `s08_align.py` | ✅ 已实现（含碰撞自动消解、逐段响度贴回） / done (auto collision resolution, per-segment loudness matching) |
| 9 混音 Mixing | `s09_mix.py` | ✅ 已实现 / done |
| 10 装配成片 Assembly | `s10_assemble.py` | ✅ 已实现 / done |

全链路已端到端跑通（`projects/20261010_e2e_cpu`，CPU 模式），交付物见该项目的 `09_output/`。

*The full pipeline runs end-to-end (`projects/20261010_e2e_cpu`, CPU mode); deliverables in that project's `09_output/`.*

**2026-10-10 双模式实测 / Dual-mode verification**（测试项目在 `E:\dub_projects\`，源视频取自 `E:\中文版Mateus Asato五声音阶大师课` / test projects in `E:\dub_projects\`, sources from the Asato pentatonic masterclass）：

- **在线模式 Online mode** `20261010_online_end`（95s，硅基流动 DeepSeek-V3 翻译，GPU）：全链路 12 分钟跑通，qc.py 9/9 通过。/ *95 s, SiliconFlow DeepSeek-V3 translation, GPU: full pipeline in 12 minutes, qc.py 9/9.*
- **离线模式 Offline mode** `20261010_offline_blues`（88s，本地 MarianMT 翻译，GPU，防火墙阻断全部 python 出站流量 / 88 s, local MarianMT translation, GPU, with a firewall blocking all python outbound traffic）：全链路跑通，qc.py 8/8 通过。*Full pipeline passed, qc.py 8/8.* ASR 覆盖经语音活动区比对验证无漏识别。/ *ASR coverage verified against speech-activity regions — no missed speech.*

**2026-10-11 大师课全量批量 / Full masterclass batch**：17 个视频（133.6 分钟）经 `scripts/batch_class.py` 全部转换完成，14 个项目 qc 全过；3 个讲师语速快的项目上调 `tempo_boost` 后通过；1 个纯演奏视频自动识别并交付原样成片。详见 `work/batch/status.json`。
/ *17 videos (133.6 minutes) fully converted via `scripts/batch_class.py`: 14 projects passed QC; 3 fast-talking videos passed after raising `tempo_boost`; 1 pure-performance video was auto-detected and delivered as-is. See `work/batch/status.json`.*

公共库（`scripts/lib/`）已就绪：配置、状态与断点续跑、数据模型、时长预算、ffmpeg 封装、字幕生成、环境切换、**离线加固（offline.py）**。自动质检：`python scripts/qc.py projects/<项目>`（详见 SPEC 7.1）。
/ *Common library (`scripts/lib/`) ready: config, state & resume, data models, duration budget, ffmpeg wrappers, subtitle generation, env switching, **offline hardening (offline.py)**. Auto QC: `python scripts/qc.py projects/<project>` (see SPEC 7.1).*

## 前置依赖 / Prerequisites

**ffmpeg 已就位**：`tools/ffmpeg/bin/` 下已放置完整版 ffmpeg 9.0.2 与 ffprobe（gyan.dev essentials 构建，两者同版本）。查找顺序：
/ ***ffmpeg is bundled*: `tools/ffmpeg/bin/` contains full ffmpeg 9.0.2 and ffprobe (gyan.dev essentials build, same version). Lookup order:**

1. 环境变量 `FFMPEG_BIN` / `FFPROBE_BIN` / *env vars `FFMPEG_BIN` / `FFPROBE_BIN`*
2. `tools/ffmpeg/bin/ffmpeg.exe`
3. 系统 PATH / *system PATH*

> 系统 PATH 里的 ffmpeg 是 TRAE 自带的精简版（`--disable-everything`，连 wav 都读不了），**不要依赖它**。需要调第三方库（demucs / whisperx）时，脚本会先调 `audio.use_bundled_ffmpeg()` 把内置目录前置到 PATH。
> *The ffmpeg on the system PATH is TRAE's stripped build (`--disable-everything`, can't even read wav) — **don't rely on it**. When third-party libs (demucs / whisperx) need ffmpeg, scripts call `audio.use_bundled_ffmpeg()` to prepend the bundled folder to PATH.*

**Python**：本机 `python` 是 TRAE 自带精简版（无 `venv` 模块），建虚拟环境请用 `py -3.12`。各阶段会按需自动切换到对应环境（见下），从哪个解释器启动都可以。
/ ***Python**: the local `python` is TRAE's stripped build (no `venv` module) — use `py -3.12` for venvs. Each stage auto-switches to its environment (below); any interpreter can launch it.*

**ASCII 路径（历史约定，2026-10-18 起放宽） / ASCII paths (legacy, relaxed 2026-10-18)**：项目根原为中文（`d:\电影翻译`），而 `sentencepiece`、`kaldifst`（`wetext` 依赖）、torch 的 jit 序列化（s04 加载 `silero_vad.jit`，C++ 层窄字符 `fopen`）等 C++ 库用窄字符打开文件，遇到非 ASCII 路径会直接报错，因此历史上用三个 junction 把 `envs` 与 `models` 暴露成 ASCII 路径：
/ *The project root used to contain Chinese characters, and C++ libs like `sentencepiece`, `kaldifst` (a `wetext` dependency), and torch's jit serialization (s04 loads `silero_vad.jit` via narrow-char `fopen`) fail on non-ASCII paths, so three junctions historically exposed `envs` and `models` as ASCII paths:*

```powershell
cmd /c mklink /J C:\dub_envs   "E:\MovieTrans\envs"
cmd /c mklink /J C:\dub_models "E:\MovieTrans\models"
cmd /c mklink /J C:\dub_torch  "E:\MovieTrans\models\torch"
```

**项目已于 2026-10-18 整体迁入 ASCII 根 `E:\MovieTrans`**，现状：
/ *The project moved to the ASCII root `E:\MovieTrans` on 2026-10-18. Current status:*

- `C:\dub_models`：**仍在使用**——模型权重的真实存储就在 `C:\dub_models`，`E:\MovieTrans\models` 是指向它的 junction（不要把权重复制回 D 盘）/ *still in use — real weights live at `C:\dub_models`; `E:\MovieTrans\models` is a junction to it (do not copy weights back to D:)*
- `C:\dub_envs`：**可选**——`lib/envs.py` 的 `ascii_envs_dir()` 在项目根为 ASCII 时已自动回退到项目内 `envs`（2026-10-18 新增）/ *optional — `ascii_envs_dir()` now falls back to the in-project `envs` when the root is ASCII (added 2026-10-18)*
- `C:\dub_torch`：**可选**——`torch_home()` 同样有项目内 `models/torch` 回退 / *optional — `torch_home()` also falls back to in-project `models/torch`*

也可用环境变量 `DUB_ENVS_DIR` 指定另一个 ASCII 的 envs 目录、`DUB_TORCH_DIR` 指定另一个 ASCII 的 torch 缓存目录。若项目再次被移到中文路径下，请补建上述三个 junction。
/ *Env vars `DUB_ENVS_DIR` / `DUB_TORCH_DIR` can point to other ASCII dirs. If the project is moved back under a Chinese path, recreate the three junctions above.*

自检 / Self-test：

```powershell
python scripts/selftest.py
```

最后一项会校验 ffmpeg 是否具备 `pcm_s16le` 音频编码器。/ *The last item checks that ffmpeg has the `pcm_s16le` audio encoder.*

## 快速开始 / Quick Start

### 方式零：发行包 / Option 0: Distribution packages

| 形态 / Form | 体积 / Size | 安装 / Install | 适合 / For |
|---|---|---|---|
| **lite** | 245 MB | 双击 `1_安装.bat`（建环境 + 下 14 GB 权重）/ run `1_安装.bat` (build envs + fetch 14 GB weights) | 有网络 / online |
| **full** | 约 24 GB | 双击 `安装_离线.bat`，全程不联网 / run `安装_离线.bat`, fully offline | 内网 / offline |

两种都需要 Python 3.12 + 3.11，解压到英文路径。装完双击 `2_启动.bat`。
/ Both need Python 3.12 + 3.11, and an ASCII-only path. Then run `2_启动.bat`.

出包 / Build: `python scripts/publish.py lite` / `publish.py full`.

### 方式一：Web 界面（推荐） / Option 1: Web UI (recommended)

```powershell
python scripts/webui.py
# 自动打开 http://127.0.0.1:8765
```

界面能力（零第三方依赖，纯 Python 标准库，随仓库分发即可用）/ *UI capabilities (zero third-party deps, pure stdlib, works out of the repo)*：

- **新建项目**：磁盘浏览器选源文件，自动建目录骨架；可选「翻译设置继承」（直接沿用某个旧项目的翻译后端与 Key，如硅基流动）与「运行设备」（CPU / GPU）
  / ***New project**: pick a source file via the disk browser; folder skeleton is created automatically; optional "inherit translation config" (reuse an old project's backend and key) and "run device" (CPU/GPU)*
- **配置编辑**：翻译后端一键切换 **本地 MarianMT ↔ DeepSeek / OpenAI / 自定义 OpenAI 兼容 API**（如硅基流动），以及其他全部参数（设备 / 模型 / 变速 / 混音…）。每个配置区块都标注了对应的阶段（s01–s10）——改哪块、从哪个阶段重算一目了然
  / ***Config editor**: one-click switch of translation backend **local MarianMT ↔ DeepSeek / OpenAI / custom OpenAI-compatible API**, plus all other parameters (device/model/tempo/mixing…). Each block is tagged with its stage (s01–s10) — which block to change and which stage re-runs is obvious*
- **语气与语速（s07）**：「文本智能分析」模式用 QwenEmotion 从中文译文分析 8 维情感引导语气（音色仍取原演员参考音频）；「语速提升」在合成时原生提速（IndexTTS 默认朗读腔约 4 字/秒，电影对白约 5.5 字/秒；建议 1.10–1.20）
  / ***Tone & pace (s07)**: "text sentiment analysis" uses QwenEmotion on the Chinese translation for 8-dim emotion guidance (voice still from the actor); "pace boost" speeds up natively during synthesis*
- **TTS 引擎切换（s07）**：IndexTTS-2.5（本机主力）/ GPT-SoVITS（本机备选）/ 硅基流动 CosyVoice2 API（云端备选，2026-10-21 实测通过）三选一；选云端可填 Key 并一键「测试连通性」——用项目自己的音色端到端合成一句样音（费用可忽略），Key 有效性与云端合成一次验完，样音可直接播放
  / ***TTS engine switch (s07)**: pick one of IndexTTS-2.5 (local primary) / GPT-SoVITS (local alt) / SiliconFlow CosyVoice2 API (cloud backup, verified 2026-10-21); for the cloud option you can set a key and hit "test connection" — an end-to-end one-line sample with the project's own voice (negligible cost) validates the key and cloud synthesis at once, and the sample is playable*
- **阶段进度表**：10 个阶段的完成/跳过/运行中/失败状态，逐个显示开始时间、结束时间、耗时；每阶段可一键查看日志或从此重跑
  / ***Stage progress**: all 10 stages with done/skipped/running/failed status, start/end times and durations; per-stage log view and re-run*
- **运行控制**：全流程 / 从某阶段起 / 仅某阶段 / 强制重跑 / 单段调试，实时控制台与阶段日志（控制台与阶段日志每行均带 `[HH:MM:SS]` 时间戳，运行横幅显示开始时间 / 已运行时长 / 当前阶段）
  / ***Run control**: full / from-stage / only-stage / force / single-segment debug, live console and stage logs (each line time-stamped; banner shows start / elapsed / current stage)*
- **人工复核 R1–R4**：说话人表、译文表在线编辑，保存为 `*.edited.json` 编辑层（脚本永不覆盖）；参考音频 / TTS 分段 / 混音在线试听
  / ***Human review R1–R4**: speaker and translation tables editable in the browser, saved as `*.edited.json` edit layers (scripts never overwrite); online audition of reference audio / TTS segments / mix*
- **批量处理**：选一个视频文件夹批量转换整门课程/整季视频，自动继承翻译配置、逐个质检、进度表实时更新（与单项目流水线互斥）；每集质检通过后自动按原文件名归拢到 `成片汇总\`
  / ***Batch conversion**: convert a whole course/season from one folder; inherits translation config, per-video QC, live progress (mutually exclusive with single-project runs); after each QC pass, videos are auto-archived by original filename into `成片汇总\`*
- **交付物**：成片在线预览、字幕查看、一键下载
  / ***Deliverables**: in-browser video preview, subtitle view, one-click download*
- **中英双语界面**：顶栏 EN/中文 按钮一键切换（词条覆盖主要界面）
  / ***Bilingual UI**: EN/中文 toggle in the top bar (covers the main interface)*

> 发布提示：界面默认只监听 127.0.0.1；要给局域网访问需显式 `--host 0.0.0.0`（接口无鉴权）。项目默认建在 `<仓库>/projects/`，本盘空间紧张可 `--projects-dir E:\dub_projects`。
> *Publishing note: the UI listens on 127.0.0.1 by default; use `--host 0.0.0.0` explicitly for LAN access (no auth on the API). Projects go to `<repo>/projects/` by default; use `--projects-dir` if the disk is tight.*

### 方式二：命令行 / Option 2: Command line

```powershell
# 1. 新建项目（会复制源文件到 00_source/） Create a project (copies the source into 00_source/)
#    自动继承最近一个带 API Key 项目的翻译/TTS 配置（否则 s05 会退回本地翻译模型）
#    Inherits translation/TTS config from the newest project that has an API key
python scripts/new_project.py 20261008_demo_clip "D:\path\to\clip.mp4"

#    仓库所在盘空间紧张时，可把项目放到别的盘（流水线不受影响）
#    Put projects on another disk if the repo disk is tight (pipeline is unaffected)
python scripts/new_project.py 20261008_demo "clip.mp4" --out E:\dub_projects

# 2. 一条命令跑完整条链路（自动按需切环境；输入未变的阶段自动跳过）
#    Run the whole chain in one command (auto env switching; unchanged stages are skipped)
python scripts/pipeline.py projects/20261008_demo_clip

# 3. 看状态 / 交付物  Status / deliverables
Get-Content projects/20261008_demo_clip/state.json
Get-ChildItem projects/20261008_demo_clip/09_output
```

也可以逐阶段单跑 / Or run stages individually：

```powershell
python scripts/s01_extract_audio.py projects/20261008_demo_clip
python scripts/s02_separate.py projects/20261008_demo_clip      # 自动切到 env_sep / auto-switches to env_sep
python scripts/s03_denoise.py projects/20261008_demo_clip       # 可选，默认关闭 / optional, off by default
python scripts/s04_asr.py projects/20261008_demo_clip           # 自动切到 env_asr
python scripts/s05_translate.py projects/20261008_demo_clip
python scripts/s06_voice_profile.py projects/20261008_demo_clip
python scripts/s07_tts.py projects/20261008_demo_clip           # 自动切到 env_tts
python scripts/s08_align.py projects/20261008_demo_clip
python scripts/s09_mix.py projects/20261008_demo_clip
python scripts/s10_assemble.py projects/20261008_demo_clip
```

## 阶段3 降噪 / Stage 3 denoise

`denoise.enabled` **默认 false**（SPEC 里标注为可选步骤）。无论开关，s03 都会产出 `02_stems/vocals_clean.wav`，下游只需认这一个路径：
/ *`denoise.enabled` is **false by default** (marked optional in SPEC). Either way s03 produces `02_stems/vocals_clean.wav` — downstream reads only this path:*

- `enabled = false`：不做降噪，原样转码（保证路径稳定） / *no denoise, straight transcode (stable path)*
- `enabled = true`：调 `tools/deepfilternet/deep-filter.exe` 降噪 / *denoise via `tools/deepfilternet/deep-filter.exe`*

s03 是纯 CLI 调用，**不依赖任何 Python 环境**，不需要切 env。
/ *s03 is a pure CLI call — **no Python environment needed**, no env switching.*

## 环境切换 / Environment switching

各阶段依赖不同的模型环境（`env_sep` / `env_asr` / `env_tts`），而 `pipeline.py` 用同一个解释器调度所有阶段。`s02` 起，脚本会在 `import` 前调 `envs.ensure_env(...)`：**当前解释器缺依赖时，自动用目标环境的 python 重跑本脚本**。所以从哪个解释器启动都可以，不需要手动激活环境。切换时会打印一行 `[env] ... 改用 env_sep 重新执行`。
/ *Stages depend on different model environments (`env_sep` / `env_asr` / `env_tts`) while `pipeline.py` schedules everything with one interpreter. From `s02` on, scripts call `envs.ensure_env(...)` before imports: **if the current interpreter lacks a dependency, the script re-executes itself with that environment's python**. Launch from any interpreter; no manual activation. A `[env] ...` line is printed when switching.*

## 命令速查 / Command cheat sheet

```powershell
# 跑流水线（s01→s10；输入未变的阶段自动跳过） Run pipeline (s01→s10; unchanged stages skipped)
python scripts/pipeline.py projects/20261008_demo_clip

# 从某阶段开始 / 只跑某阶段 / 强制重跑  From stage / only stage / force re-run
python scripts/pipeline.py projects/20261008_demo_clip --from s04_asr
python scripts/pipeline.py projects/20261008_demo_clip --only s01_extract_audio
python scripts/pipeline.py projects/20261008_demo_clip --force

# 只处理某一段（单段调试） Single-segment debug
python scripts/pipeline.py projects/20261008_demo_clip --only s07_tts --seg seg_0001

# 核心逻辑自检  Core self-test
python scripts/selftest.py

# 成片自动质检（13 项：时长/流/响度/覆盖/碰撞/字幕/翻译/间隙残留/响度贴合/声纹/参考音频/引擎一致性）
# Auto QC of the deliverable (13 checks incl. voice-similarity, reference audio, engine consistency)
python scripts/qc.py projects/20261008_demo_clip

# Web 界面项目默认建在 <仓库>/projects/，也可指定到大盘
# Web UI projects default to <repo>/projects/; point elsewhere if needed
python scripts/webui.py --projects-dir E:\dub_projects

# WebUI 冒烟回归（改前端/接口后跑；默认只验 API 与页面元素，约 5 秒）
# WebUI smoke regression (run after touching the front-end/API; API+DOM layer only, ~5s)
python scripts/webui_smoke.py                    # 自动挑最新有产出的项目 / auto-picks the newest project with output
python scripts/webui_smoke.py --ui               # 额外跑真实 Edge 流程（需 Node+playwright）/ adds a real Edge flow

# 大师课/多视频批量转换（扫描目录、失败不阻塞、断点续跑自动跳过已完成）
# Batch conversion (folder scan, failures don't block, resume skips finished projects)
#   --inherit 选一个配好翻译的项目，批量里所有视频沿用它的翻译设置；带防重入锁
python scripts/batch_class.py --src "E:\课程视频" --inherit projects/<已配好Key的项目>
python scripts/batch_class.py --playlist work\batch\playlist.json   # 自定义清单 / custom playlist
python scripts/batch_class.py --src ... --dry-run                    # 只打印计划 / print plan only
```

## 设计要点 / Design notes

- **每阶段落盘**：任何一环出问题只重跑那一环。/ ***Per-stage artifacts**: if anything breaks, only that stage re-runs.*
- **编辑层优先**：`X.edited.json` 存在时下游优先读它，脚本永不覆盖人工修改。/ ***Edit layers first**: when `X.edited.json` exists downstream reads it; scripts never overwrite human edits.*
- **断点续跑**：输入文件指纹 + 配置哈希都没变就自动跳过；`--force` 强制重跑。/ ***Resume**: unchanged file fingerprints + config hash → auto skip; `--force` re-runs.*
- **配置哈希按阶段隔离**：每个阶段在 `run_stage(..., config_keys=[...])` 里声明自己依赖哪些顶层配置段，只有这些段变化才触发重跑。改 `tts.engine` 不会让 Demucs 白跑一遍。声明必须显式给出，写错键名会直接报错（避免静默漏判）。
  / ***Per-stage config hashing**: each stage declares the top-level config keys it depends on; only changes to those trigger a re-run. Changing `tts.engine` never makes Demucs re-run. Declarations must be explicit; wrong key names fail loudly (no silent misses).*
- **TTS 引擎三选一**（WebUI 配置页切换）：IndexTTS-2.5（本机主力，默认）/ GPT-SoVITS（本机备选，相似度上限更高，需单独部署）/ 硅基流动 CosyVoice2 API（云端备选，2026-10-21 实测通过）——云端不占显卡、按量计费（约 ¥1.5–3/万汉字），参考音频上传云端（隐私自酌），适合显卡被占/无卡时应急。三引擎产物格式一致，时长预算/对齐/混音全链路与引擎无关；配置页可一键「测试连通性」（端到端小样合成，费用可忽略）。
  / ***Three TTS engines** (switch in the WebUI config page): IndexTTS-2.5 (local primary, default) / GPT-SoVITS (local alt, higher similarity ceiling, needs a separate deployment) / SiliconFlow CosyVoice2 API (cloud backup, verified 2026-10-21) — the cloud option uses no GPU and bills per use (~¥1.5–3 per 10k chars), uploads reference audio to the cloud (privacy is your call), and is an escape hatch when the GPU is busy or unavailable. All three produce identical artifact formats; the budget/align/mix chain is engine-agnostic; the config page offers one-click "test connection" (end-to-end sample synthesis, negligible cost).*
- **串行执行**：峰值显存取单步最大值（约 8 GB），12 GB 显存有余量。/ ***Serial execution**: peak VRAM is the max of any single step (~8 GB); 12 GB VRAM has headroom.*
- **Web 界面连接容错**：预览成片/试听时关页面、拖进度条产生的客户端断连已全链路静默处理（流式写、JSON 兜底、服务器级 handle_error），不会拖垮服务。
  / ***Web UI connection fault tolerance**: client disconnects while previewing (page close, progress seeking) are handled silently end to end (streaming writes, JSON fallbacks, server-level handle_error) — they never take the service down.*
- **纯离线可跑**：模型权重全部随包附带，运行期不联网。两个 torch/HF 的静默联网点已代码级钉死（`lib/offline.py`：torch.hub 缓存命中前查 GitHub 的 API、HF 系联网校验）。翻译选「本地 MarianMT」即全离线；选 LLM API 时仅 s05 走网络。已用防火墙阻断出站流量实测：纯离线全链路跑通且质检通过。
  / ***Fully offline capable**: all weights ship with the package; nothing touches the network at runtime. Two silent network paths in torch/HF are pinned at code level (`lib/offline.py`: torch.hub queries GitHub before cache hits; HF online checks). Choose "local MarianMT" translation for a fully offline run; with an LLM API only s05 goes online. Verified by firewall-blocking all python outbound traffic: the offline pipeline ran end to end and passed QC.*
- **语速原则**：中文表达密度高于英语，**绝不放慢配音去填满原声时长**——只加速、不放慢；译文比原声短时尾部自然留白。对齐后 0.25s 内的轻微重叠可接受（qc.py 按时间轴碰撞判定）。讲师语速特别快的视频若 QC 报大量碰撞，把 `tts.tempo_boost` 上调到 1.20–1.25 重跑 `--from s07_tts` 即可（实测语速最快的讲解视频需要 1.25）。
  / ***Pace principle**: Chinese is denser than English — **never slow the dub down to fill the original timing** — only speed up; shorter lines leave natural pauses. Overlaps ≤0.25 s after alignment are acceptable (qc.py judges by timeline collision). For fast-talking videos with many collisions, raise `tts.tempo_boost` to 1.20–1.25 and re-run `--from s07_tts` (the fastest-talking video needed 1.25).*
- **纯演奏/无语音视频**：s04 检测不到语音时（如纯示范演奏片段），自动跳过 s05-s10，交付原样成片到 `09_output/`（qc.py 识别此情形并放行）。
  / ***No-speech videos**: when s04 detects no speech (e.g. pure performance clips), s05–s10 are skipped and the untouched video is delivered to `09_output/` (qc.py recognizes this case).*
- **分离质量自检（s02）**：Demucs 偶发把人声几乎全分到背景轨（乐句类视频实测 master 96% vs vocals 2%），人声轨近乎为空时自动回退用原始混音做人声轨，配音不再静默丢失。
  / ***Separation quality check (s02)**: Demucs occasionally puts nearly all vocals into the background stem (measured master 96% vs vocals 2% on a licks video). When the vocal stem is nearly empty, the pipeline falls back to the original mix as the vocal track — the dub is never silently lost.*
- **逐段响度贴回（s08）**：配音段电平自动对齐原演员该句的电平（gain 夹 ±8dB、中位数兜底），保留原片的响度动态；s09 全局 loudnorm 只管交付响度，互不干扰。
  / ***Per-segment loudness matching (s08)**: each dub segment is level-matched to the original actor's line (gain clamped ±8 dB, median fallback), preserving the film's loudness dynamics; s09's global loudnorm only sets delivery loudness — the two don't interfere.*
- **字幕装配（s10）**：默认只生成 .srt（视频流拷贝）；可选把中文字幕烧录进画面（视频需重编码）。源画面已有硬字幕时建议保持关闭（会双层叠加）。
  / ***Subtitles (s10)**: generates .srt only by default (video stream copied); optionally burns Chinese subtitles into the video (requires re-encoding). If the source already has burned-in subs, keep it off (double layer).*

## 已修复的 bug（发布记录） / Fixed bugs (release log)

- **2026-10-11** webui：预览大文件中途断开曾导致整个服务崩溃（写断开的 socket 二次抛异常）——三层修复：`_serve_file` 流式写捕获断连、`do_GET/do_POST` 兜底响应防二次异常、`DubServer.handle_error` 静默连接错误。已实测 RST 断连后服务存活。
  / *webui: mid-preview disconnects used to crash the whole service (double exception writing to a dead socket) — fixed at three layers: streaming-write catch in `_serve_file`, double-fault-safe fallbacks in `do_GET/do_POST`, silent `DubServer.handle_error`. Verified alive after RST disconnects.*
- **2026-10-11** webui 复核页 R3：时长列读错字段（`duration`/`budget`，manifest 实际是 `actual_dur`/`target_dur`）恒显 0.00s——已修。
  / *webui review page R3: duration column read the wrong fields (`duration`/`budget` instead of `actual_dur`/`target_dur`) and always showed 0.00s — fixed.*
- **2026-10-11** s08：overrun 段变速到 1.15 后仍可能与下一句碰撞（实测 0.2s）——新增碰撞感知加速（上限 1.30），QC 时间轴碰撞判定不变。
  / *s08: overrun segments sped up to 1.15 could still collide with the next line (0.2 s observed) — collision-aware speed-up added (cap 1.30); QC collision criterion unchanged.*
- **2026-10-11** s02：Demucs 偶发把人声错分到背景轨——新增分离质量自检与回退。/ *s02: Demucs occasionally misroutes vocals to the background stem — separation-quality check and fallback added.*
- **2026-10-11** pipeline：无语音视频（纯演奏）静默产出空配音——自动识别并交付原样成片。/ *pipeline: no-speech videos (pure performance) silently produced an empty dub — now auto-detected and delivered as the untouched video.*
- **2026-10-11** webui 新增「批量处理」页：文件夹扫描 → 逐视频建项目跑全流程 → QC，实时进度表；/ *webui: new "Batch" page — folder scan → per-video project runs → QC, with live progress.*
- **2026-10-11** s08 新增逐段响度贴回、s10 新增可选字幕烧录（见设计要点）。/ *s08 per-segment loudness matching and s10 optional subtitle burning added (see design notes).*
- **2026-10-11** batch_class：skip 判定只查"已记录阶段都 done"导致半途中断的项目被误跳——改为 10 阶段全 done 或已有交付物；新增防重入锁（pid + 映像名校验）。
  / *batch_class: the skip check only required "recorded stages are done", so half-finished projects were wrongly skipped — now requires all 10 stages done or an existing deliverable; re-entry lock added (pid + image-name check).*

## 路线图（评估过、暂不做） / Roadmap (evaluated, deferred)

- **备选克隆引擎**（F5-TTS / CosyVoice 本地版）：IndexTTS2.5 对某些音色不满足时再评估；接入契约见 `s07_tts.py` 模块 docstring
  / *Alternative cloning engines (F5-TTS / local CosyVoice): evaluate if IndexTTS2.5 disappoints on some voices; integration contract in the `s07_tts.py` module docstring*
- **Edge-TTS 免费配音**：不做——不能克隆音色，与"保留原演员音色"主张冲突
  / *Edge-TTS free dubbing: rejected — it cannot clone voices, conflicting with the "keep the original actor's voice" premise*
- **硬字幕 OCR 检测**：不做——成本与收益不匹配，烧录开关由用户自行判断
  / *Burned-in subtitle OCR detection: rejected — cost doesn't match benefit; users decide the burn-in toggle themselves*
