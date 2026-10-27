# MovieTrans · 调整计划与路线 / Adjustment Plan & Roadmap

> 本文档是项目唯一的规划入口：记录当前基线、下一步计划、触发条件与"明确不做"清单。
> 规则：**每完成一项，更新状态；每推翻一项决策，记录理由**。技术细节见 SPEC，性能见 BENCHMARK。
> 更新日期：2026-10-22

---

## 一、当前基线（2026-10-27 快照）

### 已验证的能力

| 能力 | 状态 | 验证方式 |
|---|---|---|
| 10 阶段流水线（s01–s10） | ✅ | 17 视频（133.6 分钟）全量批量；selftest **67/0**（2026-10-27 复跑 67 通过 / 0 失败） |
| 纯离线运行（本地翻译 + 全部本地推理） | ✅ | 防火墙阻断全部 python 出站流量实测 |
| LLM API 翻译（硅基流动 DeepSeek-V3） | ✅ | 全部批量项目使用，译文质量明显优于本地 MarianMT |
| 说话人分离 | ✅ | pyannote 魔搭本地版（免 token 免墙），无 token 时自动启用 |
| 逐段响度贴回原声 | ✅ | fretboard_v2 实测逐段差中位 -0.0 dB |
| 碰撞自动消解 | ✅ | s08 加速上限 1.30 + QC 时间轴碰撞判定 |
| 无语音视频处理 | ✅ | "乐句"纯演奏视频自动交付原样成片 |
| 分离质量自检回退 | ✅ | 乐句类视频实测触发（master 96% vs vocals 2%） |
| Web 界面（中英双语） | ✅ | 项目/配置/运行/复核/批量/交付物 + EN 切换 |
| 批量转换 + 防重入锁 + 自动归拢成片 | ✅ | 大师课 15 集批量实测 |
| 自动质检 qc.py | ✅ | **13 项**（阶段/时长/响度/覆盖/碰撞/字幕/翻译/间隙残留/响度贴合/声纹/参考音频/引擎一致性） |
| 性能基准 | ✅ | 217.7 分钟转换 115.7 分钟素材（1.9×），估算公式见 BENCHMARK.md；长片系数见 2026-10-22 实测 |
| 长片整片（单文件 44.5 分钟） | ✅ | `20261022_ancient_china_44min`：414 段 / 9 说话人，qc 12/13，声纹中位 0.34 |
| 多人剧集（12 分钟） | ⚠ | `20261022_bcs_12min`：222 段 / 6 人，qc 12/13；声纹 0.15 FAIL，已定位 SPK_04（占 63% 段） |
| 搬家后全链路复跑 | ✅ | 20261018_smoke 项目 s01–s10 + QC 通过（2026-10-18，ASCII 新家 E:\MovieTrans） |
| env_tts（IndexTTS 2.5） | ✅ | 断链 junction 修复后冒烟通过（py3.11.13 + torch 2.8.0+cu128 + CUDA True） |
| GPT-SoVITS 备选引擎 | ✅ 部署就绪 | D:\GPT-SoVITS（v2final 底模），散人音色双引擎合成验证；s07 接入未做（排期见下） |

### 发布形态（2026-10-27 定版：两个开源版本）

| 形态 | 产物 | 体积 | 安装方式 | 状态 |
|---|---|---|---|---|
| **lite** | `E:\MovieTrans-lite` | 245 MB | 双击 `1_安装.bat`：`setup_envs.py` 建 4 个环境（清华 pip 源）+ `fetch_models.py` 下 14 GB 权重（魔搭优先 3.7 MB/s） | ✅ 已生成验证 |
| **full** | `E:\MovieTrans-full` | 约 24 GB（权重 14 + wheels 10 + 工具） | 双击 `安装_离线.bat`：从包内 `wheels/` 建环境，**全程不联网** | 🔶 流程已通（`--no-models` 试产 286 MB 通过）；待全量 wheel 下载后出正式包 |
| 开发主仓 | `E:\MovieTrans` | — | git 基线 `v0.9.0`（commit 6eb4e8d） | ✅ |
| `E:\MovieTrans-dev` | 换机开发副本 | 26–33 GB | 待定，E 盘已扩至 932G 可落实 | ⏸ |

**打包关键约束（已写进 `publish.py` 并处理）**：
1. `envs/env_tts` 是 junction，实体在 `tools/index-tts/.venv` —— 普通递归复制会得到空壳或重复 7 GB
2. **Windows venv 不可跨机搬迁**（`pyvenv.cfg` 存绝对路径），所以 full 不塞 venv，改为「包内 Python + 离线 wheel」，首次安装现建
3. `make_wheelhouse.py` 需把 `xxx @ file:///…whl` 形式的本地依赖还原成 `xxx==版本+cuXXX`（env_tts 的 torch 当年是本地 wheel 装的，那些 wheel 已随 work/ 清理删除）

---


### 已知遗留（用户听片后定夺）

- ~~3 个讲师语速快的项目（指板可视化 / 改变思维3 / 离调音）存在轻微叠话~~
  → 2026-10-21 极端段救援机制上线后此问题有自动解：借相邻静音为主、叠话兜底，
  重跑即消（见 2026-10-21 变更记录）；这三个老项目未重跑，属历史成片
- ~~fretboard_v2 的 SPK_02 疑似误拆~~：pyannote 已判定为同一人，R1 疑点自动消除

---

## 二、近期计划（按优先级）

### P-1 · 多人对白实测 ✅ 已完成（2026-10-20）

**为什么**：产品宣称"多说话人"能力，但全部实测素材基本为单人——这是唯一未被
真实场景验证过的核心能力。diarization 的说话人数量判断、多角色音色分配、
R1 复核流程都可能在多人场景暴露问题。

**怎么做**：找一段 2–5 分钟、两人以上英文对白的电影片段 → 建项目跑全链路 →
对照原片检查：说话人数量是否判对、每句归属是否正确、各角色音色是否稳定。

**验收**：R1 复核通过（无拆错/合错），成片各角色音色可辨识。

> 2026-10-18 注：smoke 项目（MA-1 单人讲师课）已在新家完成全链路复跑 + QC 10/10，
> 但**单人场景不构成多人验证**；等不到片源时用「合成对白桩」自测（见 E-2）。
>
> ✅ 2026-10-20 完成：真实影片《风骚律师》4 集合集切 5min56s 多人对白窗口
> （项目 `20261020_bcs_multi`），s04 自动判多人、119 段 4 说话人归属全对，
> 过拆收敛无误触发，s01–s10 全链路 + 成片产出（翻译走本地 opus-mt，Key 失效顺带验证了兜底）。

### P-2 · emo_mode 默认值 ✅ 已定版 text（2026-10-20 试听）

**为什么**：QwenEmotion 语气分析是现成能力（权重随包），从未开过。
8 维情感引导可能让台词更贴情绪，也可能过烈。

**怎么做**：同一素材（建议选对白情绪起伏大的）跑 `emo_mode=ref` 与 `emo_mode=text`
两版，同一句并排试听。

**产出**：定下默认值；若默认改 text，更新 DEFAULT_CONFIG 与文档。

> ✅ 2026-10-20 试听定版：默认值 ref→text。依据：参考音频是全片拼的固定锚点、
> 情绪≠每句台词应有情绪，ref 会让全片一个腔调；用户听感偏好 text 一致。
> DEFAULT_CONFIG 与 SPEC 已同步；WebUI 语气模式单选保留，既有项目逐项目可调。

### P-3 · s03 降噪开关对比 ✅ 已完成（2026-10-22，结论：默认 false 站得住）

**实测结果**（`scripts/denoise_ab.py`，噪声库 9 个音源，5 档 SNR）：

| 目标 SNR | WER 中位（脏 → DeepFilterNet 后） |
|---|---|
| -5 dB | 0.000 → 0.099（**变差**：辅音被吃掉） |
| 0 / 5 / 10 / 15 dB | 0.000 → 0.000（无增益） |

large-v3 对配乐/宽带噪声本身极鲁棒，降噪**提不动 ASR 可懂度**。
降噪的价值在听感（TTS 参考音质、混音干净度），不在转录 ——
所以 `denoise.enabled` 保持 `false`，想开按听感开。详见 BENCHMARK「降噪 A/B」。

### P-4 · 长视频验证（40–60 分钟）✅ 已完成（2026-10-22，44.5 分钟纪录片）

**结果**：`20261022_ancient_china_44min`（BV13f4y1Q7qa P1，44.5 分钟英文旁白）
全链跑通，414 段 / 9 说话人，qc 12/13（1 WARN = 混合引擎）。

**暴露并修掉的两处硬上限**：s02 分离 OOM（改按 300s 分块）、
s09 混音命令行 32767 字符上限（改 ≤100 段/批分窗 + concat）。

**剩余待办**：90–120 分钟整片（内存占用、字幕分段、章节切分）仍未验证；
且长片的云端 TTS 会**越跑越慢**（2s/段 → 75s/段），需要「分批 + 断点续跑」
或并发，属 P-6。

### P-5 · WebUI 英文长尾补全 🟡 低优先

**现状**：i18n 词条 299 条覆盖主要界面，个别长提示句里有未翻译碎片。

**怎么做**：收集实际英文使用中别扭的句子 → 补词条。**有英文用户真实反馈再补**，
不提前铺。

---

## 三、工程评审调整计划（2026-10-18，基于搬家+跑通实战复盘）

> 来源：项目迁入 `E:\MovieTrans` 并全链路复跑后的工程评审。按「会死人的 / 天天难受的 /
> 痒」分级立项，完成后在状态列打勾并更新变更记录。

### E 组 · 会死人的（P0）

| # | 问题 | 处方 | 状态 |
|---|---|---|---|
| E-1 | **selftest 只验「文件在不在」，不验「环境能不能用」**——实战应验：env_tts 断链 junction、GPU torch/lib 漏搬 6G，两项都全绿放过 | 每个 venv 加 import 冒烟（env_sep: demucs / env_asr: whisperx / env_tts: torch+cuda+indextts），warn 项改硬 fail | ✅ 2026-10-18：must() 四项硬检查 + GPU warn + GSV 文件级检查，selftest 52/0 |
| E-2 | **GPU 显存协调靠人工**：ComfyUI 常驻时 TTS 只能 CPU 跑（RTF 25×，一部电影 25 小时），用户踩坑才知道 | pipeline 启动时探空闲显存，<8G 自动切 CPU 并在日志警告「预计耗时 ×10」 | ✅ 2026-10-18：lib/gpu.py effective_device()（各阶段按需 4/6/8GB 探测，DUB_FORCE_CPU/DUB_ALLOW_LOW_VRAM 环境变量，降级不写回配置不影响指纹）；单测三路+真实 s02 验证 |
| E-3 | **发布四处手工同步，搬家即漂移**——实战应验：robocopy 静默漏 6G GPU torch、副本与主存储不一致 | 写 `publish.py` 一键生成 SRC/PUB/dev；发布纪律从「记得做」变「一条命令」。本次先落 SRC（E 盘 30G 放不下 PUB+dev，需扩盘或改发网盘） | ✅ SRC（2026-10-18，`E:\MovieTrans-src` 245MB + 换机说明）；PUB/dev 待扩盘 |

### F 组 · 天天难受的（P1）

| # | 问题 | 处方 | 状态 |
|---|---|---|---|
| F-1 | **TTS 占全链路 70%**，备选评估的「① 硅基流动 API 接入」停在纸上（半天工作量，Key 现成） | 排期接入：s07 引擎分发接 API，兼作 GPU 被占时的逃生通道 | ✅ 2026-10-20 代码交付 + **2026-10-21 实测通过**（余额充足；修了三处 API 契约问题：合成端点 `/audio/generations`→`/audio/speech`（旧路径已 404）、上传音色补参考文本 text（profiles.ref_segments 重建）、缓存音色失效时合成遇 5xx 自动重传换 URI + 上传后生效等待；zero2 六个极端段 6/6 合成成功，救援判据自动适配其慢语速特性） |
| F-2 | **多人对白（核心卖点）干等片源**，测试数据不会自己造 | 无片源时用 ffmpeg 拼对白 / TTS 双声轨合成桩，自测 diarization 拆得准不准 | ✅ 2026-10-18：两讲师人声交替拼接成双人素材，auto 判多人 + pyannote 拆 SPK_01/02 全链路通过 |
| F-3 | **中文路径兜底代码仍在核心链路**（s05 tokenizer 临时目录、envs.py 三级 fallback）——项目根已 ASCII，属历史包袱 | 代码内标注「历史兜底，中文根场景才激活」，防新人误读必走 | ✅ 2026-10-18 核实：s05 docstring 与 envs.py ascii_envs_dir 均已标注（随搬迁完成） |

### G 组 · 痒（P2）

| # | 问题 | 处方 | 状态 |
|---|---|---|---|
| G-1 | QC 无客观音质指标，「克隆像不像」靠人耳 | 用项目已有 wespeaker/campplus 加「配音段 vs 原声段声纹相似度自动分」 | ✅ 2026-10-18：qc 第 11 项，实测基线校准阈值 0.18（同人 0.22-0.35 / 不同人 ≤0.12），solo/duo 11/11 |
| G-2 | `scripts/webui.py` 与 `scripts/webui/` 双轨并存，改界面不知道改哪套 | 明确主副本，另一路径留 deprecation 说明 | ✅ 2026-10-18 核实为**误报**：webui.py 是后端服务器、webui/ 是其前端静态目录（WEB_DIR/serve 实锤），配套结构非双轨 |
| G-3 | 三盘分裂（代码 D / 模型 C / 发布 E），备份换机精神分裂 | README 补一张存储地图（哪盘放什么、为什么） | ✅ 2026-10-18：存储地图表入 README（前置依赖节前） |

### H 组 · 复盘新增（2026-10-18 晚，双引擎+auto 上线后）

| # | 问题 | 处方 | 状态 |
|---|---|---|---|
| H-1 | **BENCHMARK 缺今日实测系数**：GSV RTF、auto 预判耗时、单主播省时 36%、四组合对比——估算公式的 GSV/单主播场景没有数据 | 把 20261018_smoke/solo/duo 三项目的实测数据补进 BENCHMARK.md | ✅ 2026-10-18：引擎对比/单主播省时/声纹基线三表入档 |
| H-2 | **pipeline 阶段无耗时统计**：state.json 只有 finished 时间戳（无 started/duration），对比/校准全靠手工扒 | run_stage 记 started/duration 入 state.json；QC 可直接复用 | ✅ 2026-10-18：实测 s10 duration=5.3s 正确记录 |
| H-3 | **双人测试素材靠手工拼**（提人声→deep-filter→交替 concat），下次回归还要再拼一遍 | 固化成 `make_test_fixture.py` 一键生成（单/多人素材） | ✅ 2026-10-18：scripts/make_test_fixture.py 正式化——三场景 CLI（--list/--scenes/--refresh/--make-projects/--ref 覆盖音色）+ ground truth json 导出 + 自动 reexec env_tts + 幂等 |
| H-4 | **GSV 参考音频质量无预检**：GSV 对参考音频+文本对齐敏感，BGM 残留会毁音色锚点（duo 素材 B 段实证） | s06 给参考段加质量检查（ASR 置信度/BGM 占比），不合格自动换段 | ✅ 2026-10-20（s06 质量预检上线：`tts.ref_min_confidence` 0.30 / `ref_max_bg_ratio` 2.5×；不合格段自动选段垫底、人工指定只警告；指标入 profiles.json ref_quality；B_duo 实跑通过） |
| H-5 | **GSV LoRA 微调音色**（真正的「训练一次」）：零样本效果不满足的音色（如散人若有 10 分钟+素材）可微调提升稳定度 | 大投入，等零样本不满足的实例出现再立项 | ☐ 按需 |

### I 组 · 复盘新增（2026-10-22 全项目评估）

| # | 问题 | 处方 | 状态 |
|---|---|---|---|
| I-1 | **QC 第 11 项（声纹相似度）假绿**：env_base 缺 numpy 时该项静默跳过且计 PASS（zero2 复跑实证）——E-1「只验文件在不在」同款病的新变种，selftest 全绿但质检实际少一项 | qc.py 在跳过时输出 `[WARN]` 不计入通过数；selftest 加 qc 冒烟（或至少校验 numpy 依赖在） | ✅ 2026-10-22：Report 三态（PASS/FAIL/WARN，warn 不计通过数、不影响退出码）；selftest 增 [11] 三态语义单测 4 项 + env_asr 声纹依赖 hint；env_asr 全项实跑 11/11（声纹中位 0.22）；selftest 57/0 |
| I-2 | **WebUI 无质检入口**：流水线跑完后要回命令行跑 `qc.py` 才知道质量，交付页只有文件列表——刚合并的 R2/R3 复核页就在旁边，缺最后一环 | 交付页加「运行质检」按钮 + 结果面板（qc.py 已有 JSON 输出可复用）；进阶：pipeline 完成时自动跑 | ✅ 2026-10-22：POST/GET `/api/project/<pid>/qc`（Runner kind=qc 异步跑+轮询，优先 env_asr 解释器使声纹项可跑）；交付页「🔍 质检」按钮+三态徽标结果面板+重新质检；parse_qc_log 从带前缀日志剥时间戳解析 JSON；curl 四路径（启动/轮询/400 空交付物/409 互斥）+ Playwright 6 项断言零 JS 错误；SPEC 7.1 同步三态语义 |
| I-3 | **work/ 积压约 3.3GB 临时资产**：torch wheel（2.8G+231M，已装完）、whl_tmp 16M、rescue_backup 66M、abtest 35M、duo 55M | 清理（属项目内临时目录、非个人数据）；后续在 README 或 envs/README 注明 work/ 清理口径 | ✅ 2026-10-22：删 3 个 torch wheel（3.1G）+ whl_tmp + silero_tmp + abtest + 临时校验文件，3.3G→228M；保留 sources（片源唯一副本）/duo/fixtures（回归语料）/rescue_backup（回滚）；README 存储地图下立「work/ 清理口径」 |
| I-4 | **BENCHMARK 缺三组新实测数据**：硅基流动云端 RTF（探针音频可推算）、rescue 机制效果（zero2 叠话 4→2）、zero2 全链耗时（s07 1491s/129 段 ≈ 11.6s/段） | 补表，校准估算公式（公式当前只有 GSV/单主播系数） | ✅ 2026-10-22：云端 RTF 实测 0.31（3 段计时，~3.2× 实时、每字 0.21s、129 段 s07 ≈4.5 分钟 vs GPU 24.8 分钟）；rescue 数据（6/129 段 = 借 4 段 0.42s + 叠 2 段 1.02s，判据修订 4.4s→1.0s）；zero2 全链分阶段表（合计 36.2 分钟 = 6.1× 源时长，公式校验误差 0.5%） |
| I-5 | **`需要人工操作.md` 已大部过时**：junction 只剩 C:\dub_models 必需、GPU 型号/E-2 自动降级描述仍是手工版 | 已更新 GPU 段（2026-10-22）；其余随下次环境变动整体重写 | ✅ 2026-10-22：全文重写——纠正 junction 事实（models 是真实目录、C:\dub_models 是指向未建 dev 副本的失效残留）、pyannote 段改为「魔搭本地管线免 token 已跑通」（token 仅多人拆分不准时才需要）、selftest 57/0、补 zero2/救援/云端三条实测结论 |
| I-6 | **WebUI 无自动化回归**：每次改前端都靠一次性 Playwright 脚本，跑完即删，下一轮无从复用 | 固化为 `scripts/webui_smoke.py`（API/页面层）+ `webui_smoke_ui.js`（真实浏览器层，`--ui` 触发） | ✅ 2026-10-22：API 层 10 项（起临时服务、关键元素齐全、i18n 可载、项目列表、详情字段、交付物、404、坏引擎 400、路径穿越 403）+ 浏览器层 12 项（切三种引擎的设备行显隐、对照表有数据、质检面板在位、零 JS 错误）；`--ui` 用系统 Edge，无浏览器下载 |
| I-7 | **新项目默认配置会静默降质**：`new_project.py` 直接写 `DEFAULT_CONFIG`（`deepseek` + 空 key），s05 只在日志里一行提示就退回本地 MarianMT —— A 项目首跑 review 占 32%，白跑一遍翻译 | new_project 继承模板项目的 translation/tts 两段；s05 回退时打 `⚠` 并在汇总行标「local（回退，无 Key）」 | ✅ 2026-10-22：`new_project` 自动挑最近一个带 API Key 的项目继承（`--config-from` 可指定、`--no-inherit` 可关），建项目即打印继承了谁的 provider/model/engine；s05 回退提示加 ⚠ 与成因 |
| I-8 | **分批补跑无法收口**：`--seg` 跑完一律标 `partial`，即使已凑齐全量 —— A 的 275 段云端 + 139 段本地补跑后 state 停在 partial，`--from s08` 被判上游未完成，只能手工改 state.json | 补跑结束按「有译文的段是否全部有配音 + 无失败段」判定 done/partial；s07 补进度与 ETA、段均耗时超线告警 | ✅ 2026-10-22：`run_stage(partial=可调用)` 支持产物落盘后再判定；`s07._manifest_coverage` 纯函数 + selftest 2 项；每 10 段打进度/ETA，段均 > `tts.slow_segment_seconds`(20s) 告警并给切引擎/分批建议；manifest 每段记 `engine`/`elapsed` |
| I-9 | **声纹判定被「主场角色」绑架**：bcs_12min 池化中位 0.147 ≈ SPK_04 一人（63% 段）的分数，其余 5 人 0.21~0.31；初判「参考段被污染」被 `ref_quality` 数据否掉（6 人全 qualified、BGM 比 ≤0.83） | qc 按说话人拆开报明细并点名差分者；s06 参考段改跨时间轴分散抽样 | ✅ 2026-10-22：qc 声纹 detail 出「SPK_04 0.13(63%段)…」+ 换参考的具体指路；`tts.ref_spread`（默认 true）按早/中/晚三段各取一段，selftest 2 项；`tts.ref_short_warn_seconds`(3s) 短参考点名告警 |
| I-10 | **GSV 定位失准**：文档把它写成「已可跑的备选引擎」，但零样本音色实测余弦 0.07（英文对照 0.017 / 中文 -0.009），落在「不同人」区间 | 降级为「实验/微调验证用」；要可用必须 per-角色 few-shot 微调 | ✅ 2026-10-22：`s07_tts_gsv` 启动即打印不可交付告警（保留运行能力）；BENCHMARK 记录两层排查；few-shot 训练列入触发式计划（见四节） |

### 执行序建议（2026-10-18 晚评审）

**本周清隐患**：E-1（selftest 冒烟，1h）→ G-1（声纹相似度分，2h）→ E-2（显存自动降级，2h）——合计半天清掉全部 P0。
**下周增能力**：F-1（硅基流动 API，半天——引擎分发层已就绪，HTTP 版更简单）→ H-3 + H-1（测试资产与数据基建，1.5h）→ H-2/H-4/P-3 按需。
**决策待定**：四段成片试听后定 GSV vs IndexTTS 的音色口味，影响 F-1 的 API 选型（CosyVoice2 vs GSV 云端）；PUB/dev 发布位等 E 盘扩容决策。

### 新增已知坑（2026-10-18 实战，解法已入 envs/README.md）

- **WorkBuddy 沙箱 sitecustomize 劫持 `shutil.rmtree`**：跑 pipeline 必须 `PYTHONPATH=` 绕开，
  否则 s02 临时目录清理 fail-closed 崩盘
- **junction 类目录搬家后会断链**：env_tts 断链实锤；搬家后必须逐个 junction 验证目标可达

---

## 四、触发式计划（条件出现才做）

| 项目 | 触发条件 | 方案概要 |
|---|---|---|
| 硅基流动 CosyVoice2 API 克隆 | 显卡被占/无卡，本地 TTS 不可接受 | ✅ 已交付实测（2026-10-21，见 F-1 行）：`tts.engine=siliconflow`，WebUI 可切换+测连通性；也解决批量时 GPU 独占问题 |
| 备选克隆引擎（CosyVoice2 / F5-TTS / GPT-SoVITS） | IndexTTS2.5 偏重（8GB/慢）或某些音色效果不满足 | 选型已完成，见 [TTS备选评估.md](TTS备选评估.md)；接入顺序：硅基流动 API → CosyVoice2 本地 → 按听感再定 |
| PUB 干净机冒烟测试 | 要把整合包发给别人之前 | 在无 Python/无缓存的机器（或 VM）双击 setup.bat 从零走通 |
| 多语言源（日/韩→中） | 出现非英语片源需求 | whisper 识别天然多语；LLM 翻译改语言对；MarianMT 兜底需补对应模型 |
| 云 API 转写/分离 | 出现"无 GPU 机器也要跑"的硬需求 | Azure/阿里云按分钟计费，需账号与付费 |

---

## 五、明确不做（含理由，防止反复）

| 项目 | 理由 |
|---|---|
| Edge-TTS 免费配音 | 不能克隆音色，与"保留原演员音色"的核心主张冲突 |
| TTS 引擎抽象层预重构 | 单引擎下的无消费抽象；接入新引擎时按真实需求设计（见 s07 docstring 契约） |
| 多渠道 TTS/ASR 大而全接入 | 维护成本高于价值；MovieTrans 定位是深加工专线 |
| 硬字幕 OCR 检测 | 成本与收益不匹配；烧录开关由用户自行判断 |
| 时间轴重排大改 | 现有预算算法 + 碰撞消解已实测有效，不引入 848 行式复杂度 |
| lip-sync 口型对齐 | SPEC 明确范围外；开源方案（latentsync 等）存在，确有需要时单独立项 |

---

## 六、工程纪律（长期有效）

1. **文档同步**：改任何影响行为/用法的代码，同一回合更新 README/SPEC——文档滞后即返工
2. **编码禁令**：本机 PowerShell 的 Get-Content/Set-Content 处理中文 UTF-8 文件会写坏内容，
   一律用 Python 的 `io.open(..., encoding="utf-8")`（已两次踩坑，见记忆）
3. **发布纪律**：代码改动同步四处（开发仓 / PUB / SRC / dev 副本）；发布前跑
   selftest + qc.py + API Key 扫描
4. **离线纪律**：任何新依赖模型必须走"随包或 fetch_models.py 预下载"，
   运行期联网视为 bug（lib/offline.py 有既有的钉死机制可扩展）
5. **验证纪律**：新能力必须有实测数据（QC/对比/截图）才算完成

---

## 七、变更记录

| 日期 | 变更 |
|---|---|
| 2026-10-11 | 初版：基线快照 + 近期计划 + 触发式计划 + 不做清单 + 工程纪律 |
| 2026-10-18 | 项目整体迁入 `E:\MovieTrans`（ASCII 根，selftest 48/0，原 `D:\电影翻译` 已清除）；修复 env_tts 断链 junction；全链路复跑 `20261018_smoke`（QC 10/10）；GPT-SoVITS 部署就绪（`D:\GPT-SoVITS`，s07 备选引擎）；「散人」音色重建并同步资料库（Index + GPT-SoVITS 双引擎验证）；新增「工程评审调整计划」E/F/G 三组共 9 项；沙箱 `PYTHONPATH` 坑与 junction 断链坑入 envs/README.md；发布形态表按 E 盘实况（30G）修订：SRC 本次落实，PUB/dev/dub_projects 暂缓 |
| 2026-10-18（晚） | **单主播模式上线**（`single_speaker` 顶层开关 + WebUI 创建/配置页勾选）：单人素材跳过说话人分离，省 pyannote 约 4GB 显存与数分钟，克隆一次全程复用；SPEC/README/s04 docstring 同步 |
| 2026-10-18（晚二） | **GPT-SoVITS 引擎接入 s07**（`tts.engine=gpt_sovits`，s07 自动 reexec 到 GSV venv 跑 `s07_tts_gsv.py`；参考文本取 s06 ref_segments 对应原文；语速用 ffmpeg atempo 后处理——GSV v2final 的 speed 参数实测无效）；**单主播自动判断**（`single_speaker=auto` 三态，wespeaker 预判单人跳过 pyannote，多人走完整分离）；WebUI：引擎选择/单主播三态下拉/emo 控件联动隐藏；qc.py 兼容音频源项目（m4a/wav 交付 + 音轨校验）；**四组测试矩阵全通**（单人/多人 × IndexTTS/GPT-SoVITS，solo/duo 项目 QC 10/10）；修复 `C:\dub_models` gpt_sovits 权重残缺（roberta/v2final 曾被沙箱吞写+随副本删除，重新下载+safetensors 重转） |
| 2026-10-18（晚六二） | **H-3 测试素材生成器正式化**（scripts/make_test_fixture.py：三场景 ground truth 导出 + CLI 全参数 + 自动 reexec env_tts + 幂等 + 已存在防护；README 命令速查收录） |
| 2026-10-18（晚五） | **E-2 显存自动降级上线**（lib/gpu.py：各阶段按需 4/6/8GB 探测 nvidia-smi 空闲显存，不足自动 CPU + 警告；DUB_FORCE_CPU/DUB_ALLOW_LOW_VRAM 环境变量；不写回配置不影响指纹）；G-2 核实为误报关闭（webui 后端+前端配套）；G-3 存储地图入 README；F-3 标注核实完成——**P0 组（E 组）整组关闭，项目进入纯增强阶段** |
| 2026-10-18（晚四） | **第一档改进落地**：E-1 selftest 环境冒烟（must 四项+GPU+GSV 检查，52/0）；G-1 QC 声纹相似度第 11 项（实测基线校准阈值 0.18）；H-1 BENCHMARK 补双引擎/单主播/声纹基线三表；H-2 run_stage 耗时入 state.json |
| 2026-10-18（晚三） | **改进项复盘立项**：新增 H 组 5 项（BENCHMARK 补实测/阶段耗时统计/测试素材生成器/GSV 参考音频质量预检/GSV LoRA 按需）；补执行序建议（本周 E-1→G-1→E-2 清 P0，下周 F-1 API + H 组）——全部待办以 ROADMAP 为准，回头按序开工 |
| 2026-10-20 | **D_fast 过拆自动收敛上线**（s04 分离出口「碎片类合并」：段占比 ≤25% 的碎片类与最近邻中心余弦 ≥ `diarization.merge_sim` 0.60 才并入，大类互并永不发生；实测判别带——同人 ≥0.63 / 真异人 ≤0.13；三夹具回归 ALL_PASS，D_fast 3→2 人与 ground truth 一致，B_duo/A_mono 零变化；wespeaker 加模块级缓存）；**H-4 参考音频质量预检上线**（s06：置信度 + BGM 占比双指标，自动垫底/人工警告，ref_quality 入 profiles.json）；**F-1 硅基流动 CosyVoice2 API 代码交付**（`tts.engine=siliconflow`，s07_tts_sf.py 纯 stdlib，音色 md5 缓存，WebUI 选项+Key 输入，emo=text 自动回退；**未实测**——余额不足，用户明确：本机引擎优先，云端仅逃生通道）；**GPT-SoVITS 本机复活**（venv 复用搬迁 site-packages + torch 重装 2.5.1+cu124，魔搭拉 v2final+hubert+roberta 仅 1.1GB（v1/判别器省略），bin→safetensors 过 CVE 检查，junction 挂 E:\MovieTrans\models，双底模加载冒烟通过，selftest 51/0） |
| 2026-10-20（午） | **GSV 真实克隆合成打通**（B_duo SPK_02 英文参考 → 中文台词 1.62s 正常；新补齐三个离线依赖：nltk_data→D:\GPT-SoVITS\runtime\nltk_data（ghfast.top 代理拉取，nltk 只认 zip 形态）、fasttext lid.176.bin→pretrained_models/fast_langdetect/，GSV 首次跑已联网补齐、后续可离线）；**P-2 emo 对比素材产出**（work/test_emo_compare.py：两句 × ref/text 共 4 wav，text 版时长偏长，待试听定默认值）；**WebUI EN 词条补齐**（siliconflow 三条 + GSV 遗漏两条，P-5 长尾清了一部分）；试听包在 work/abtest/（原声/IndexTTS/GSV 三连 + emo 四连），等用户试听后定 GSV vs IndexTTS 口味与 emo_mode 默认值 |
| 2026-10-20（午后） | **试听定版**：IndexTTS 更像原声（用户听感确认，默认引擎不变）；GSV 零样本质量偏弱——原因明确：v2final 通用底模 + 仅 6.6s 参考、未微调，GSV 强项在逐说话人微调，零样本只是及格线，维持备选定位；**emo_mode 默认值 ref→text**（config.py，注释更新；依据：参考音频是全片拼的固定锚点、情绪≠每句台词应有情绪，ref 会让全片一个腔调；用户听感偏好 text 一致）；WebUI 语气模式单选保留（既有项目配置不受影响，逐项目可调）；SPEC.md 增「语气模式」说明块 |
| 2026-10-20（下午） | **真实影片测试立项并跑批**（20261020_bcs_multi：B 站《风骚律师》四集合集切 5min56s 多人对白窗口——Acker 对峙+Squat Cobbler；s04 自动判多人 119 段 4 说话人全对，过拆收敛无误触发；翻译 Key 失效改走本地 opus-mt，顺手清掉 mt 兜底低优项）；**新增《翻译引擎对比.md》**（本地 vs 在线：opus-mt 实测 54% review + 重复退化实锤，TranslateGemma 12B 虽质量超底模但纯 MT 无指令能力，推荐梯队 Ollama qwen3:14b→云 API→opus-mt；待办 s05 加 provider=ollama）；**修复 IndexTTS QwenEmotion 生成死循环**（max_new_tokens 32768→512，病态输入单段挂 25min+，py-spy 定位；emo_mode=text 跑批可靠性的前置补丁） |
| 2026-10-21 | **极端段救援上线并按用户听感修订为「借为主、叠话兜底」**（s07+s08+lib/budget：估算字/秒 ≥7 的段不硬压预算，改贴时间轴真实窗口——前后静音全借上、起点可前移 0.25s；上午首版叠话判据用「相对自然合成倍率」被 IndexTTS 慢语速误导，4/6 段叠了话；下午改双闸门：纯借阶段总变速 ≤2.2×（≈中文 4~5 字/秒）且字/秒 ≤7 才不叠，叠话只伸「让总变速回落到上限」的最小量；zero2 重测叠话段 4→2、总量 4.4s→1.0s，仅 seg_0098（前后档共 0.1s 无静音可借）保留 0.62s；qc 11/11、selftest 52/0）；**WebUI 复核页大改**（R2/R3 合并为「原文+原声试听+译文编辑+配音试听+语速列」对照表，媒体片段定位原声、播放越界自动归位+⟲ 重置，R1~R4 全卡片折叠；语速列口径纠错：duration_factor 是时长倍率须取倒数）；**F-1 硅基流动 CosyVoice2 API 实测通过**（余额充足；修合成端点 404、上传缺参考文本、缓存音色失效重传三处契约问题；zero2 六极端段 6/6，其慢语速自动多走叠话分支）；**F-1 WebUI 接入收尾**（配置页引擎三选一标签改为「主力/备选/云端备选」、TTS 就绪徽标（未配 Key/venv 缺失即时警示，仿翻译 backend badge）、云端选中时隐藏本机设备项、新增「测试连通性」按钮——用项目参考音频端到端合成一句样音（费用可忽略、音色 URI 缓存复用、样音可播放），无参考音频时退化 chat 端点验 Key；post_config 增 tts.engine 白名单校验；余额端点 /user/info 已下线故不查余额；Playwright 12 项流程断言全过、零 JS 错误；SPEC/README(_BILINGUAL)/TTS备选评估同步） |
| 2026-10-22 | **全项目评估 + 状态同步**（无新功能）：P-1 多人对白实测标记完成（2026-10-20 风骚律师真实影片 4 人全链路验证，此前状态漏更新）；P-2 emo_mode 默认值标记定版 text（2026-10-20 试听，同上）；「已知遗留」3 项目叠话条目标注 rescue 机制已提供自动解（老项目未重跑，属历史成片）；`需要人工操作.md` GPU 段更新为 3080 Laptop 16G 实况 + gpu.py 自动降级说明（原文仍是 3060 12G 手工版）；新增 I 组 5 项评估发现（QC 声纹项 env_base 假绿、WebUI 无质检入口、work/ 积压 3.3GB、BENCHMARK 缺云端/救援/zero2 三组数据、人工操作文档重写） |
| 2026-10-22（上午续） | **I 组 5 项全部落地**：I-1 qc.py 三态语义（PASS/FAIL/WARN，环境跳过不计通过数）+ selftest [11] 单测 + env_asr 依赖 hint，env_asr 全项实跑 11/11、selftest 57/0；I-2 WebUI 交付页「🔍 质检」（异步 Runner + 轮询 + 三态结果面板，优先 env_asr 跑满 11 项，parse_qc_log 剥前缀解析），curl 四路径 + Playwright 6 项零 JS 错误；I-3 work/ 3.3G→228M（删 torch wheel×3/whl_tmp/silero_tmp/abtest，留 sources/fixtures/rescue_backup）+ README 立清理口径；I-4 BENCHMARK 补云端 RTF 0.31（3.2× 实时）/rescue 6 段数据/zero2 全链表（36.2 分钟=6.1×，公式误差 0.5%）；I-5《需要人工操作.md》全文重写（junction 事实纠正：models 是真实目录、C:\dub_models 失效残留；pyannote 魔搭免 token 已跑通）。文档同步：SPEC 7.1（11 项+三态+0.25s 容差）、README（9 项→11 项、work/ 口径） |
| 2026-10-22（近午） | **README 开头「写在前面」改写**（用户本人主述缘起）：`WibeCoding`→`Vibe Coding`；「周末做出第一个可以接受的版本」+ 补「后来一路加到十个阶段、克隆原演员音色、自动裁剪节奏」使由来与现状衔接；费用口径由「没有支付任何费用」改为「几乎没有支付任何费用」（现已含硅基流动付费 API 少量消耗）；README_BILINGUAL.md 同段英译同步 |
| 2026-10-22（下午） | **WebUI 冒烟回归固化**（I-6）：新增 `scripts/webui_smoke.py`（纯标准库自动起临时服务，API/页面层 10 项断言）+ `scripts/webui_smoke_ui.js`（Playwright，`--ui` 触发，12 项流程断言，用系统 Edge 免下浏览器）；实测 10/10 与 11/11 全过；README 中英双语命令速查补入口 |
| 2026-10-22（晚） | **按 A/D 双片实测结果做七处调整（I-7~I-10 落地）**：①`new_project` 自动继承最近带 API Key 项目的 translation/tts（`--config-from`/`--no-inherit`），修「新项目静默退回本地翻译」②s05 回退提示加 ⚠ 并在汇总行标「local（回退，无 Key）」③`run_stage(partial=可调用)` + `s07._manifest_coverage`：分批补跑跑满即标 done，不再需要手改 state ④s07 每 10 段打进度/ETA，段均超 `tts.slow_segment_seconds`(20s) 告警给三条建议；manifest 每段记 `engine`/`elapsed`，顶层记本次引擎 ⑤qc 声纹按说话人拆开报（点名差分者 + 换参考的指路），新增「参考音频」「引擎一致性」两项 → 13 项 ⑥s06 参考段跨时间轴分箱抽样（`tts.ref_spread`）+ 短参考告警（`tts.ref_short_warn_seconds`）⑦GSV 启动即打印「零样本不可交付」告警并降级定位。**实测复核**：D 10/11→11/13（声纹 0.15 FAIL，明细指出 SPK_04 占 63% 段/0.13）、A 11/11→12/13（唯一 WARN=混用引擎，声纹 0.34）；参考段三组对照差异 ±0.01（噪声内，属防御性改动）；selftest 57/0→65/0。文档：BENCHMARK 纠正「参考段被污染」误判并补数据、SPEC 7.1 改 13 项 + 抽样说明、README 改 13 项与 new_project 用法 |
| 2026-10-27 | **发布前体检：selftest 导入超时误判修复**。复跑自检得 64/2/1，两条 FAIL 均为 `run_import` 固定 300s 超时（env_sep demucs / env_tts indextts / env_asr pyannote）。单独计时确认环境无损坏：热盘 demucs 46s、IndexTTS 218s、pyannote **379s**（已超 300s），冷盘首次读 14G 权重更久。修：①上限提至 600s 并可用 `MT_SELFTEST_IMPORT_TIMEOUT` 覆盖 ②超时与导入报错分离——超时记「未判定」入环境提示、不计 FAIL、退出码仍 0，明细给出重跑/调大超时的建议（避免「发布前必须 0 失败」这条纪律在冷机翻车）③删除死函数 `env_ready`。验证：默认下限 67 通过/0 失败；把上限压到 1s 做极值测试 → 62 通过/0 失败/5 项超时提示、退出码 0。文档同步：ROADMAP 基线快照（49/0→67/0、qc 10→13 项、补 A/D 两行实测、发布形态表按 E 盘已扩至 932G 的实况修订——原「仅 30G」暂缓理由失效）、`需要人工操作.md` 质检 11→13 项 |
| 2026-10-27（发行） | **两个开源发行形态落地 v0.9.0**：①git 基线（`.gitignore` 排除 14G 权重 / 19G 环境 / 项目数据 / 素材视频，入库 390 文件 888KB；tag `v0.9.0`）+ `LICENSE`(MIT) + `VERSION` + `CHANGELOG` ②`publish.py` 重写为 lite/full 双目标，`_copy_tree` 对 junction **只记录不跟随**（否则 env_tts 被打成空壳或重复 7G），full 带 manifest.json ③新增 `make_wheelhouse.py`：四个 venv 导出依赖 → pip download 到同一仓库，并把 `pkg @ file:///…whl` 还原成 `pkg==ver+cuXXX`（env_tts 的 torch 当年是本地 wheel 装的，那些 wheel 已随 work/ 清理删掉，不还原必然装不上）④新增 `setup_envs_offline.py`：从包内 wheels 离线重建四个环境（半成品自动隔离为 `_broken_*`）⑤`setup_envs.py` 去掉本机 Python 路径硬依赖（候选 → `MT_PY312/MT_PY311/MT_UV` → py 启动器 → PATH；uv 改惰性查找，没 uv 不挡 base/sep/asr）。**实测**：lite 245MB、full `--no-models` 286MB 均生成通过，包内无 `__pycache__`/无 venv 残留；base 环境 24 个依赖 wheel 实测下载成功；`setup_envs_offline.py --check` 缺 wheels 时正确 fail-fast（exit 2）。待办：全量 wheel 下载（约 10G，`make_wheelhouse.py` 全跑）后出正式 full 包 |
| 2026-10-27（出包） | **两个发行包实际产出并落到 G 盘**。①`lite` 246MB→压缩 57MB（246MB 里 202MB 是 ffmpeg/ffprobe 两个二进制，7z 压缩率 77%）②`full` 22.4GB→压缩 **21GiB**（含 13.7GB 权重 + 8.5GB 离线 wheels + 245MB 源码），699 文件、CRC 校验通过。③离线 wheel 仓库补齐到 **8.5GB / 269 文件**，四个环境用 `pip install --dry-run --ignore-installed --no-index --find-links` 逐个验过：base/sep/asr/tts **全部可离线解析、退出码 0**（这一步才是"装得上"的真凭据，之前只看文件存在会漏掉 cp311/cp312 张冠李戴）。④补齐过程修掉四个坑（见 CHANGELOG）：env_tts 无 pip 导致 tts 150 个依赖全断、antlr4 只有 sdist 触发整批中止、官方源兜底把 jieba 拖到 32KB/s、py3.12 venv 不带 setuptools 缺 wheel 包。⑤`publish.py` 目标目录清理改「改名归档」，避开运行时批量删除拦截。产物：`G:/MovieTrans_release/`（G 盘根目录不可写文件、只能建子目录，故放子目录） |
