# 英文电影 → 中文原生配音

> **写在前面 —— 一个 30 年前程序员的话**
>
> 作为一个30年前的程序员，由于身体原因，我已经很久没有写代码了，我热爱代码，我曾经想过，可能这辈子也不可能继续写代码了。
>
> 这次所谓 Vibe Coding 的浪潮，让我又体会到了一次写代码的快乐，感谢这个时代吧。
>
> 我只是想搞一个自己想用的小玩意，一个周末的时间就做出了第一个可以接受的版本；后来一路加到了十个阶段，能克隆原演员音色、能自动裁剪节奏，几乎没有支付任何费用。一个周末的开头，换来了几个月的乐趣。
>
> 在使用时，我自己就是一个小白，什么都是从头摸索，和有没有编程经验毫无关系，从开发环境，依赖什么都让机器自己干，除了这段文字外，其它都是我来提需求做最终测试拍板。期间有过把思考模式开到最高，烧掉一亿Token，也有过被AI“蒙骗”。确实是一种新的体验。快乐已经不来自于代码本身，而是做一个有趣的小玩意。

---


把英文电影配音成中文（非字幕），保留原演员音色。

- 规范文档：[SPEC.md](SPEC.md)
- 性能基准：[BENCHMARK.md](BENCHMARK.md)（实测耗时与估算公式）
- **English companion**: [README_BILINGUAL.md](README_BILINGUAL.md)（中英对照版，one Chinese paragraph followed by its English rendering）
- **规划文档**：[ROADMAP.md](ROADMAP.md)（下一步计划、触发条件、不做清单）
- **TTS 备选评估**：[TTS备选评估.md](TTS备选评估.md)（本地轻量/在线经济与质量档位选型）
- 环境搭建：[envs/README.md](envs/README.md)
- **需要人工操作的事项**：[需要人工操作.md](需要人工操作.md)（GPU / HF token / API Key / 复核卡点）

## 当前进度

| 阶段 | 脚本 | 状态 |
|---|---|---|
| 1 拆音轨 | `s01_extract_audio.py` | ✅ 已实现 |
| 2 对白/背景分离 | `s02_separate.py` | ✅ 已实现（含分离质量自检与回退） |
| 3 降噪去混响 | `s03_denoise.py` | ✅ 已实现（可选，默认关闭） |
| 4 ASR+说话人分离 | `s04_asr.py` | ✅ 已实现 |
| 5 长度受控翻译 | `s05_translate.py` | ✅ 已实现 |
| 6 音色档案 | `s06_voice_profile.py` | ✅ 已实现 |
| 7 克隆 TTS | `s07_tts.py` | ✅ 已实现 |
| 8 时间轴对齐 | `s08_align.py` | ✅ 已实现（含碰撞自动消解） |
| 9 混音 | `s09_mix.py` | ✅ 已实现 |
| 10 装配成片 | `s10_assemble.py` | ✅ 已实现 |

全链路已端到端跑通（`projects/20261010_e2e_cpu`，CPU 模式），交付物见该项目的 `09_output/`。

**2026-10-10 双模式实测**（测试项目在 `E:\dub_projects\`，源视频取自
`E:\中文版Mateus Asato五声音阶大师课`）：

- **在线模式** `20261010_online_end`（95s，硅基流动 DeepSeek-V3 翻译，GPU）：
  全链路 12 分钟跑通，qc.py 9/9 通过。
- **离线模式** `20261010_offline_blues`（88s，本地 MarianMT 翻译，GPU，
  防火墙阻断全部 python 出站流量）：全链路跑通，qc.py 8/8 通过
  （间隙检查因语音段少自动跳过；本地翻译直译感重，R2 复核尤其重要）。
  ASR 覆盖经语音活动区比对验证无漏识别。

**2026-10-11 大师课全量批量**：17 个视频（133.6 分钟）经 `scripts/batch_class.py`
全部转换完成，14 个项目 qc 全过；3 个讲师语速快的项目上调 `tempo_boost` 后通过；
1 个纯演奏视频自动识别并交付原样成片。详见 `work/batch/status.json`。

公共库（`scripts/lib/`）已就绪：配置、状态与断点续跑、数据模型、时长预算、
ffmpeg 封装、字幕生成、环境切换、**离线加固（offline.py）**。
自动质检：`python scripts/qc.py projects/<项目>`（详见 SPEC 7.1）。

## 存储地图（哪盘放什么，2026-10-19 换机后现状）

| 盘/路径 | 内容 | 为什么 |
|---|---|---|
| `E:\MovieTrans` | **开发主仓**：代码 + scripts + tools + projects + models（2026-10-19 随旧机 D:\MovieTrans 整体迁入） | ASCII 根（C++ 库窄字符路径坑）；E 盘 900G，权重直接进仓库 |
| `E:\MovieTrans\models` | **模型权重真实存储**（indextts2.5 / pyannote / faster-whisper / torch.hub 缓存 / hf 缓存） | 本机不再需要 C 盘 junction |
| `D:\GPT-SoVITS` | 备选引擎部署（代码在；venv/权重待重建） | 独立 torch 版本，独立 venv |
| `E:\MovieTrans-src` | SRC 发布包（纯源码，`publish.py` 生成）——**旧机产物，本机未重建** | 换机重建用 |

> 规律：**全部资产进主仓**（本机盘大、根 ASCII，历史的三盘分裂与 C 盘 junction 已废弃）。
> 改任何一方的位置，同步更新 ROADMAP 的发布形态表与本表。

### `work/` 是临时区（可定期清理，2026-10-22 立规）

- 内容：pipeline/selftest/qc 日志（`work/qc/<项目>.log` 是 WebUI 质检结果来源）、
  下载残留（*.whl）、测试素材（`sources/` 原始片源、`duo/`+`fixtures/` 夹具）、
  各次调试备份（如 `rescue_backup/`）
- **随时可删**：`*.whl`（装完即废物）、`qc/`（重新质检会重建）、日志
- **删前三思**：`sources/`（测试片源唯一副本，删了夹具无法再生）、
  `rescue_backup/` 等调试备份（回滚路径）
- 2026-10-22 已清掉 3.1G torch 安装包与 whl_tmp/silero_tmp/abtest，3.3G→228M

## 前置依赖

**ffmpeg 已就位**：`tools/ffmpeg/bin/` 下已放置完整版 ffmpeg 9.0.2 与 ffprobe（gyan.dev essentials 构建，两者同版本）。
查找顺序：

1. 环境变量 `FFMPEG_BIN` / `FFPROBE_BIN`
2. `tools/ffmpeg/bin/ffmpeg.exe`
3. 系统 PATH

> 系统 PATH 里的 ffmpeg 是 TRAE 自带的精简版（`--disable-everything`，连 wav 都读不了），**不要依赖它**。
> 需要调第三方库（demucs / whisperx）时，脚本会先调 `audio.use_bundled_ffmpeg()` 把内置目录前置到 PATH。

**Python**：本机 3.12 在 `C:\Users\Admin1\AppData\Local\Programs\Python\Python312`，
3.11 在 `D:\Py311`。**环境重建一条命令**：`python scripts/setup_envs.py`（2026-10-19 新增，
失效 venv 自动隔离后原地重建，详见脚本头部说明）。各阶段会按需自动切换到对应环境（见下），
从哪个解释器启动都可以。

**ASCII 路径（历史约定，2026-10-18 起放宽）**：项目根目录原为中文（`d:\电影翻译`），而
`sentencepiece`、`kaldifst`（`wetext` 依赖）、torch 的 jit 序列化（s04 加载 `silero_vad.jit`，
C++ 层窄字符 `fopen`）等 C++ 库用窄字符打开文件，遇到非 ASCII 路径会直接报错，因此历史上用
三个 junction 把 `envs` 与 `models` 暴露成 ASCII 路径：

```powershell
cmd /c mklink /J C:\dub_envs   "E:\MovieTrans\envs"
cmd /c mklink /J C:\dub_models "E:\MovieTrans\models"
cmd /c mklink /J C:\dub_torch  "E:\MovieTrans\models\torch"
```

**项目已于 2026-10-19 整体迁入本机 ASCII 根 `E:\MovieTrans`**，现状：

- **三个 C 盘 junction 本机均不需要**：项目根是 ASCII、E 盘空间充足，权重直接放
  仓库 `models/`；`lib/envs.py` 的 `ascii_envs_dir()` / `torch_home()` 自动回退到项目内路径
- 历史约定保留：若项目再次被移到中文路径下，才需要补建下述 junction

也可用环境变量 `DUB_ENVS_DIR` 指定另一个 ASCII 的 envs 目录、`DUB_TORCH_DIR` 指定另一个
ASCII 的 torch 缓存目录。若项目再次被移到中文路径下，请补建上述三个 junction。

自检：

```powershell
python scripts/selftest.py
```

最后一项会校验 ffmpeg 是否具备 `pcm_s16le` 音频编码器。

## 快速开始

### 方式一：Web 界面（推荐）

```powershell
python scripts/webui.py
# 自动打开 http://127.0.0.1:8765
```

界面能力（零第三方依赖，纯 Python 标准库，随仓库分发即可用）：

- **新建项目**：磁盘浏览器选源文件，自动建目录骨架；可选「翻译设置继承」（直接沿用
  某个旧项目的翻译后端与 Key，如硅基流动）与「运行设备」（CPU / GPU）；
  口播/讲师/独白类素材勾选「**单主播模式**」——跳过说话人分离，省约 4GB 显存与数分钟，
  克隆一次全程复用同一音色
- **配置编辑**：翻译后端一键切换 **本地 MarianMT ↔ DeepSeek / OpenAI / 自定义 OpenAI 兼容 API**
  （如硅基流动），以及其他全部参数（设备 / 模型 / 变速 / 混音…）。
  每个配置区块都标注了对应的阶段（s01–s10）——改哪块、从哪个阶段重算一目了然
  （s04 区顶部有「单主播模式」开关，随时可勾选/取消，改后从 s04 起重算）
- **语气与语速（s07）**：「文本智能分析」模式用 QwenEmotion 从中文译文分析 8 维情感
  引导语气（音色仍取原演员参考音频）；「语速提升」在合成时原生提速
  （IndexTTS 默认朗读腔约 4 字/秒，电影对白约 5.5 字/秒；建议 1.10–1.20）
- **TTS 引擎切换（s07）**：IndexTTS-2.5（本机主力）/ GPT-SoVITS（本机备选）/
  硅基流动 CosyVoice2 API（云端备选）三选一；选云端可填 Key 并一键
  「测试连通性」——用项目自己的音色端到端合成一句样音（费用可忽略），
  Key 有效性与云端合成一次验完，样音可直接播放
- **阶段进度表**：10 个阶段的完成/跳过/运行中/失败状态，逐个显示开始时间、结束时间、耗时；
  每阶段可一键查看日志或从此重跑
- **运行控制**：全流程 / 从某阶段起 / 仅某阶段 / 强制重跑 / 单段调试，实时控制台与阶段日志
  （控制台与阶段日志每行均带 `[HH:MM:SS]` 时间戳，运行横幅显示开始时间 / 已运行时长 / 当前阶段）
- **人工复核 R1–R4**：说话人表、译文表在线编辑，保存为 `*.edited.json` 编辑层（脚本永不覆盖）；
  参考音频 / TTS 分段 / 混音在线试听
- **批量处理**：选一个视频文件夹批量转换整门课程/整季视频，自动继承翻译配置、
  逐个质检、进度表实时更新（与单项目流水线互斥）
- **交付物**：成片在线预览、字幕查看、一键下载

> 发布提示：界面默认只监听 127.0.0.1；要给局域网访问需显式 `--host 0.0.0.0`（接口无鉴权）。
> 项目默认建在 `<仓库>/projects/`，本盘空间紧张可 `--projects-dir E:\dub_projects`。

### 方式二：命令行

```powershell
# 1. 新建项目（会复制源文件到 00_source/）
#    自动继承最近一个「带 API Key」项目的翻译/TTS 配置（否则 s05 会退回本地翻译模型）
python scripts/new_project.py 20261008_demo_clip "D:\path\to\clip.mp4"

#    指定继承来源 / 完全不继承
python scripts/new_project.py 20261008_x "clip.mp4" --config-from projects/20261022_bcs_zero2_sf
python scripts/new_project.py 20261008_y "clip.mp4" --no-inherit

#    仓库所在盘空间紧张时，可把项目放到别的盘（流水线不受影响）
python scripts/new_project.py 20261008_demo "clip.mp4" --out E:\dub_projects

# 2. 一条命令跑完整条链路（自动按需切环境；输入未变的阶段自动跳过）
python scripts/pipeline.py projects/20261008_demo_clip

# 3. 看状态 / 交付物
Get-Content projects/20261008_demo_clip/state.json
Get-ChildItem projects/20261008_demo_clip/09_output
```

也可以逐阶段单跑：

```powershell
python scripts/s01_extract_audio.py projects/20261008_demo_clip
python scripts/s02_separate.py projects/20261008_demo_clip      # 自动切到 env_sep
python scripts/s03_denoise.py projects/20261008_demo_clip       # 可选，默认关闭
python scripts/s04_asr.py projects/20261008_demo_clip           # 自动切到 env_asr
python scripts/s05_translate.py projects/20261008_demo_clip
python scripts/s06_voice_profile.py projects/20261008_demo_clip
python scripts/s07_tts.py projects/20261008_demo_clip           # 自动切到 env_tts
python scripts/s08_align.py projects/20261008_demo_clip
python scripts/s09_mix.py projects/20261008_demo_clip
python scripts/s10_assemble.py projects/20261008_demo_clip
```

## 阶段3 降噪

`denoise.enabled` **默认 false**（SPEC 里标注为可选步骤）。无论开关，s03 都会产出
`02_stems/vocals_clean.wav`，下游只需认这一个路径：

- `enabled = false`：不做降噪，原样转码（保证路径稳定）
- `enabled = true`：调 `tools/deepfilternet/deep-filter.exe` 降噪

s03 是纯 CLI 调用，**不依赖任何 Python 环境**，不需要切 env。

要不要开、开了值不值，用 `denoise_ab.py` 拿数据说话（见命令速查）：它把
`02_stems/vocals.wav` 当干净基准，按 SNR 梯度注入 `work/noise_lib/` 里的噪声
（影视剧配乐/环境 bed + 合成粉/棕/白噪），对比降噪前后 ASR 的 WER 与置信度。

## 环境切换

各阶段依赖不同的模型环境（`env_sep` / `env_asr` / `env_tts`），而 `pipeline.py` 用同一个解释器调度所有阶段。
`s02` 起，脚本会在 `import` 前调 `envs.ensure_env(...)`：**当前解释器缺依赖时，自动用目标环境的 python 重跑本脚本**。
所以从哪个解释器启动都可以，不需要手动激活环境。切换时会打印一行 `[env] ... 改用 env_sep 重新执行`。

## 命令速查

```powershell
# 跑流水线（s01→s10；输入未变的阶段自动跳过）
python scripts/pipeline.py projects/20261008_demo_clip

# 从某阶段开始 / 只跑某阶段 / 强制重跑
python scripts/pipeline.py projects/20261008_demo_clip --from s04_asr
python scripts/pipeline.py projects/20261008_demo_clip --only s01_extract_audio
python scripts/pipeline.py projects/20261008_demo_clip --force

# 只处理某一段（单段调试）
python scripts/pipeline.py projects/20261008_demo_clip --only s07_tts --seg seg_0001

# 生成测试素材（带 ground truth 的英文对白场景；幂等，自动切 env_tts）
python scripts/make_test_fixture.py --list            # 查看场景与预期说话人数
python scripts/make_test_fixture.py                   # 合成 A/B/C 三场景
python scripts/make_test_fixture.py --make-projects   # 顺手建好测试项目（auto+继承翻译）
python scripts/make_test_fixture.py --scenes A_mono --refresh   # 强制重合成

# 核心逻辑自检
python scripts/selftest.py

# 降噪 A/B 实验（SNR 梯度注入噪声 → 对比 DeepFilterNet 前后的 ASR WER/置信度）
# 物料：干净人声取 s02 的 vocals.wav，噪声取 work/noise_lib/*.wav
envs\env_asr\Scripts\python.exe scripts\denoise_ab.py ^
    --clean projects\<pid>\02_stems\vocals.wav --noise-dir work/noise_lib ^
    --snr 0,5,15 --segments 6 --device cuda

# 成片自动质检（13 项：时长/流/响度/覆盖/碰撞/字幕/翻译/间隙残留/响度贴合/
#   声纹相似度/参考音频/引擎一致性；WARN=环境缺依赖跳过或可交付性提示、不计通过数；
#   WebUI 交付页也可一键质检）
python scripts/qc.py projects/20261008_demo_clip

# Web 界面项目默认建在 <仓库>/projects/，也可指定到大盘
python scripts/webui.py --projects-dir E:\dub_projects

# WebUI 冒烟回归（改前端/接口后跑；默认只验 API 与页面元素，约 5 秒）
python scripts/webui_smoke.py                    # 自动挑最新有产出的项目
python scripts/webui_smoke.py --ui               # 额外跑真实 Edge 流程（需 Node+playwright）

# 大师课/多视频批量转换（扫描目录、失败不阻塞、断点续跑自动跳过已完成）
#   --inherit 选一个配好翻译的项目，批量里所有视频沿用它的翻译设置；带防重入锁
python scripts/batch_class.py --src "E:\课程视频" --inherit projects/<已配好Key的项目>
python scripts/batch_class.py --playlist work\batch\playlist.json   # 自定义清单
python scripts/batch_class.py --src ... --dry-run                    # 只打印计划
```

## 设计要点

- **每阶段落盘**：任何一环出问题只重跑那一环。
- **编辑层优先**：`X.edited.json` 存在时下游优先读它，脚本永不覆盖人工修改。
- **断点续跑**：输入文件指纹 + 配置哈希都没变就自动跳过；`--force` 强制重跑。
- **配置哈希按阶段隔离**：每个阶段在 `run_stage(..., config_keys=[...])` 里声明自己依赖哪些顶层配置段，
  只有这些段变化才触发重跑。改 `tts.engine` 不会让 Demucs 白跑一遍。
  声明必须显式给出，写错键名会直接报错（避免静默漏判）。
- **TTS 引擎三选一**（WebUI 配置页切换）：IndexTTS-2.5（本机主力，默认）/
  GPT-SoVITS（本机备选，相似度上限更高，需单独部署）/
  硅基流动 CosyVoice2 API（云端备选，2026-10-21 实测通过）——云端不占显卡、
  按量计费（约 ¥1.5–3/万汉字），参考音频上传云端（隐私自酌），适合显卡被占/
  无卡时应急。三引擎产物格式一致，时长预算/对齐/混音全链路与引擎无关；
  配置页可一键「测试连通性」（端到端小样合成，费用可忽略）。
- **串行执行**：峰值显存取单步最大值（约 8 GB），12 GB 显存有余量。
- **Web 界面连接容错**（2026-10-11）：预览成片/试听时关页面、拖进度条产生的
  客户端断连已全链路静默处理（流式写、JSON 兜底、服务器级 handle_error），
  不会拖垮服务。
- **纯离线可跑**：模型权重全部随包附带，运行期不联网。两个 torch/HF 的静默联网点已代码级
  钉死（`lib/offline.py`：torch.hub 缓存命中前查 GitHub 的 API、HF 系联网校验）。
  翻译选「本地 MarianMT」即全离线；选 LLM API 时仅 s05 走网络。
  已用防火墙阻断出站流量实测：纯离线全链路跑通且质检通过。
- **语速原则**：中文表达密度高于英语，**绝不放慢配音去填满原声时长**——只加速、不放慢；
  译文比原声短时尾部自然留白。对齐后 0.25s 内的轻微重叠可接受（qc.py 按时间轴碰撞判定）。
  讲师语速特别快的视频若 QC 报大量碰撞，把 `tts.tempo_boost` 上调到 1.20–1.25 重跑
  `--from s07_tts` 即可（实测语速最快的讲解视频需要 1.25）。
- **极端段救援（s07/s08，2026-10-21）**：估算字/秒 ≥ `align.rescue_min_cps`（默认 7）
  的段硬压预算会快到听不懂，改为借时间轴真实空档合成——前后有静音就借（起点可前移
  ≤0.25s）。**借为主、叠话兜底**（用户试听反馈叠话有明显听感不适）：纯借阶段总变速
  ≤ `align.rescue_speed_max`（默认 2.2，≈中文 4~5 字/秒）且字/秒 ≤ `align.rescue_max_cps`
  （默认 7.0）就不叠话；只有前后都无静音可借的极端段（如 zero2 的 seg_0098）才叠话，
  且只伸「让总变速回落到上限」的最小量（≤1.2s 且不超过下一句窗口一半，s08 对越界部分
  做尾淡出，qc 放行计划内叠话）。调 `rescue_speed_max` 越小叠话越少、段内变速越多。
  manifest 记 `fit_dur`/`rescue`/`pre_borrow`，WebUI 语速列悬停可见救援方式。
- **纯演奏/无语音视频**：s04 检测不到语音时（如纯示范演奏片段），自动跳过 s05-s10，
  交付原样成片到 `09_output/`（qc.py 识别此情形并放行）。
- **分离质量自检（s02）**：Demucs 偶发把人声几乎全分到背景轨（乐句类视频实测
  master 96% vs vocals 2%），人声轨近乎为空时自动回退用原始混音做人声轨，
  配音不再静默丢失。
- **逐段响度贴回（s08）**：配音段电平自动对齐原演员该句的电平（gain 夹 ±8dB、
  中位数兜底），保留原片的响度动态；s09 全局 loudnorm 只管交付响度，互不干扰。
- **字幕装配（s10）**：默认只生成 .srt（视频流拷贝）；可选把中文字幕烧录进画面
  （视频需重编码）。源画面已有硬字幕时建议保持关闭（会双层叠加）。

## 已修复的 bug（发布记录）

- **2026-10-11** webui：预览大文件中途断开曾导致整个服务崩溃
  （写断开的 socket 二次抛异常）——三层修复：`_serve_file` 流式写捕获断连、
  `do_GET/do_POST` 兜底响应防二次异常、`DubServer.handle_error` 静默连接错误。
  已实测 RST 断连后服务存活。
- **2026-10-11** webui 复核页 R3：时长列读错字段（`duration`/`budget`，
  manifest 实际是 `actual_dur`/`target_dur`）恒显 0.00s——已修。
- **2026-10-11** s08：overrun 段变速到 1.15 后仍可能与下一句碰撞（实测 0.2s）
  ——新增碰撞感知加速（上限 1.30），QC 时间轴碰撞判定不变。
- **2026-10-11** s02：Demucs 偶发把人声错分到背景轨——新增分离质量自检与回退。
- **2026-10-11** pipeline：无语音视频（纯演奏）静默产出空配音——自动识别并交付原样成片。
- **2026-10-11** webui 新增「批量处理」页：文件夹扫描 → 逐视频建项目跑全流程 → QC，实时进度表；
- **2026-10-11** s08 新增逐段响度贴回、s10 新增可选字幕烧录（见设计要点）。
- **2026-10-11** batch_class：skip 判定只查"已记录阶段都 done"导致半途中断的项目被误跳——
  改为 10 阶段全 done 或已有交付物；新增防重入锁（pid + 映像名校验）。

## 路线图（评估过、暂不做）

- **备选克隆引擎**（F5-TTS / CosyVoice 本地版）：IndexTTS2.5 对某些
  音色不满足时再评估；接入契约见 `s07_tts.py` 模块 docstring
- **Edge-TTS 免费配音**：不做——不能克隆音色，与"保留原演员音色"主张冲突
- **硬字幕 OCR 检测**：不做——成本与收益不匹配，烧录开关由用户自行判断
