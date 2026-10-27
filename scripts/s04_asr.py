"""阶段4：语音识别 + 词级时间戳 + 说话人分离。

产物:
    03_asr/segments.json   时间轴真相（段级，词级时间戳在 words 里）
    03_asr/speakers.json   说话人 -> 角色映射（供人工校对，R1 卡点）

输入是 s03 的 `02_stems/vocals_clean.wav`（对白分离后的干净人声），
先转成 16k 单声道再交给 WhisperX（large-v3, int8）。

说话人分离按 `diarization.method` 选路径：
    auto      默认。有 HF token 走 pyannote，否则退回本地声纹聚类
    pyannote  强制 pyannote/speaker-diarization-3.1（门控模型，必须有 token）
    embedding 强制本地声纹嵌入 + 层次聚类（无需 token，完全离线）
    none      不分离，全部记为 SPK_01

顶层 `single_speaker=true`（单主播模式，WebUI 创建/配置页可勾选）时，
无论 diarization.method 是什么都直接走「全部 SPK_01」：全片一人的素材
（口播/讲师/独白）省掉 pyannote 的约 4GB 显存与数分钟耗时，克隆一次全程复用。

本地路径用 pyannote/wespeaker 声纹模型对每段语音求向量，再按余弦距离做
层次聚类。因为不依赖门控模型，所以默认可用；pyannote 门控模型需要用户先
在 HuggingFace 上接受条款并提供 token。

用法:
    python scripts/s04_asr.py projects/20261008_demo_clip [--force]
"""
from __future__ import annotations

import argparse
import math
import os
from pathlib import Path

from lib import audio, envs
from lib.config import load_project
from lib.offline import force_hf_offline, patch_torch_hub_local
from lib.schemas import Segment, Transcript, Word, load_json, save_json, save_transcript
from lib.state import run_stage

STAGE = "s04_asr"
INPUT_REL = "02_stems/vocals_clean.wav"
SEGMENTS_REL = "03_asr/segments.json"
SPEAKERS_REL = "03_asr/speakers.json"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HF_HOME = PROJECT_ROOT / "models" / "hf"
HF_HUB_DIR = HF_HOME / "hub"

# 单主播自动判定：auto 模式下 wespeaker 聚类若给出多类，但类间中心相似度
# 仍高于该值，视为「同一人被过拆」，判单人（阈值偏松，漏判比误判危险）
SOLO_MERGE_SIM = 0.55

# torch.hub 缓存（silero-vad 仓库 + wav2vec2 对齐权重）随包分发；
# 通过 envs.torch_home 优先走 ASCII junction，torch jit 在 C++ 层
# 打不开中文路径。无缓存时维持默认 ~/.cache/torch。
_torch_home = envs.torch_home(PROJECT_ROOT)
if _torch_home:
    os.environ.setdefault("TORCH_HOME", _torch_home)

TOKEN_ENV_VARS = ("HF_TOKEN", "HUGGINGFACE_TOKEN", "HUGGING_FACE_HUB_TOKEN")


def configure_hf() -> None:
    """统一模型缓存到 models/hf，并强制离线（权重随包分发，运行期不下载）。

    HF_HUB_OFFLINE 必须在 import huggingface_hub 之前设置（该库 import 时
    读取环境变量），本函数在 run_asr 里 import whisperx 之前调用。
    """
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ["HF_HOME"] = str(HF_HOME)
    force_hf_offline()


def hf_token(cfg: dict) -> str | None:
    for key in TOKEN_ENV_VARS:
        value = os.environ.get(key)
        if value:
            return value
    return cfg["diarization"].get("token")


def local_diarization_dir() -> Path | None:
    """魔搭下载的 pyannote 本地管线（含 config.yaml），无需 HF token。

    权重来自 modelscope.cn（pyannote 官方同名搬运，MIT）：
    speaker-diarization-3.1 的 config + segmentation-3.0 权重。
    """
    cand = PROJECT_ROOT / "models" / "pyannote" / "speaker-diarization-3.1"
    return cand if (cand / "config.yaml").exists() else None


def build_local_pipeline(model_dir: Path, device: str):
    """从魔搭本地目录构建 pyannote 分离管线（绕开两处联网点）。

    1. pyannote 4.0 的 SpeakerDiarization 默认 plda 指向门控的 community-1；
       本路径用 AgglomerativeClustering（不使用 plda），传哑 PLDA 实例
       通过必填预加载。配置里的 segmentation HF 引用改写为本地权重目录。
    2. embedding 走 HF id，命中本地缓存（HF_HUB_OFFLINE=1）。
    """
    import numpy as np
    import yaml
    from pyannote.audio.core.plda import PLDA
    from pyannote.audio.pipelines.speaker_diarization import SpeakerDiarization

    config = yaml.safe_load((model_dir / "config.yaml").read_text(encoding="utf-8"))
    params = config["pipeline"]["params"]
    params["segmentation"] = str((model_dir / "segmentation-3.0").resolve())
    params["clustering"] = "AgglomerativeClustering"

    dummy_dir = model_dir / "plda-dummy"
    if not (dummy_dir / "plda.npz").exists():
        dummy_dir.mkdir(parents=True, exist_ok=True)
        # 键名满足 pyannote.audio.utils.vbx.vbx_setup；数值为占位（不会被使用）
        np.savez(dummy_dir / "xvec_transform.npz",
                 mean1=np.zeros(4), mean2=np.zeros(4), lda=np.eye(4))
        np.savez(dummy_dir / "plda.npz",
                 mu=np.zeros(4), tr=np.eye(4), psi=np.ones(4))
    params["plda"] = PLDA(dummy_dir / "xvec_transform.npz", dummy_dir / "plda.npz")
    params["token"] = None

    import torch

    pipeline = SpeakerDiarization(**params)
    pipeline.instantiate(config.get("params", {}))
    return pipeline.to(torch.device(device))


def prepare_16k(src: Path, dst: Path, sample_rate: int) -> Path:
    audio.extract_audio(src, dst, sample_rate, 1)
    return dst


def run_asr(wav: Path, cfg: dict):
    import whisperx

    asr = cfg["asr"]
    model = whisperx.load_model(
        asr["model"],
        asr["device"],
        compute_type=asr["compute_type"],
        language=cfg["source_lang"],
        vad_method="silero",
        download_root=str(HF_HUB_DIR),
    )
    waveform = whisperx.load_audio(str(wav))
    result = model.transcribe(
        waveform,
        batch_size=asr["batch_size"],
        language=cfg["source_lang"],
        print_progress=True,
    )
    return result, waveform


def run_align(result, waveform, cfg: dict):
    """词级时间戳对齐；对齐模型不可用时退回段级时间戳。"""
    import whisperx

    device = cfg["asr"]["device"]
    try:
        model_a, metadata = whisperx.load_align_model(
            language_code=result["language"], device=device
        )
        return whisperx.align(
            result["segments"], model_a, metadata, waveform, device,
            return_char_alignments=False, print_progress=True,
        )
    except Exception as exc:  # noqa: BLE001 - 对齐只是增强，失败不该中断识别
        print(f"[align] 词级对齐不可用，退回段级时间戳：{type(exc).__name__}: {exc}")
        return result


def _normalize_labels(labels: list[str | None]) -> list[str]:
    """把任意说话人标签按首次出现顺序重排成 SPK_01、SPK_02……，并填补空缺。"""
    filled: list[str] = []
    last = None
    for lab in labels:
        if lab is not None:
            last = lab
        filled.append(last)
    last = None
    for i in range(len(filled) - 1, -1, -1):
        if filled[i] is not None:
            last = filled[i]
        else:
            filled[i] = last

    order: dict[str, str] = {}
    out: list[str] = []
    for lab in filled:
        key = lab or "SPK_01"
        if key not in order:
            order[key] = f"SPK_{len(order) + 1:02d}"
        out.append(order[key])
    return out


MERGE_SIM = 0.60  # 碎片类与最近邻中心余弦超过该值才合并（实测同人 ≥0.63 / 真异人 ≤0.13）
FRAGMENT_MAX_FRAC = 0.25  # 段数占比 ≤25% 的类才算碎片类（两个大类的相互合并不会发生）


def _merge_oversplit(labels: list[str | None], whisper_segments, waveform, cfg: dict):
    """过拆自动收敛：把「少量段组成的碎片类」合并回最近邻说话人。

    背景（2026-10-20 D_fast 夹具实测）：0.5–1.5s 急速轮替场景下 pyannote
    会把同一人的极短感叹拆成第三个说话人（12 句判对 10）。实测判别带极宽：
    碎片类与正确归属的中心余弦 0.63，而真异人类间仅 0.04–0.13。

    保守策略——只动「碎片类」：段数占比 ≤25%（FRAGMENT_MAX_FRAC）的类，
    若与最近邻类中心余弦 > diarization.merge_sim（默认 0.60，≤0 关闭），
    并入最近邻；两个大类的相互合并永远不会发生；不会合并到
    diarization.min_speakers 以下；向量不足的段保持原标签随类映射。
    返回 (labels, 合并次数)。
    """
    import numpy as np

    dia = cfg["diarization"]
    merge_sim = float(dia.get("merge_sim", MERGE_SIM))
    if merge_sim <= 0 or len({lab for lab in labels if lab}) < 2:
        return labels, 0
    matrix, indices = _extract_embeddings(whisper_segments, waveform, cfg)
    if matrix is None:
        return labels, 0

    min_spk = max(int(dia.get("min_speakers") or 1), 1)
    labs = [labels[i] for i in indices]
    cur = list(dict.fromkeys(labs))          # 首次出现序
    frag_max = max(1, round(FRAGMENT_MAX_FRAC * len(labs)))
    mapping: dict[str, str] = {}
    merged = 0

    def _centroid(g: str):
        rows = matrix[[j for j, l in enumerate(labs) if l == g]]
        c = rows.mean(axis=0)
        return c / (np.linalg.norm(c) + 1e-9)

    changed = True
    while changed and len(cur) > min_spk:
        changed = False
        # 按段数从小到大找碎片类
        for g in sorted(cur, key=lambda h: labs.count(h)):
            if labs.count(g) > frag_max:
                continue
            others = [h for h in cur if h != g]
            if not others:
                continue
            cg = _centroid(g)
            sims = [float(cg @ _centroid(h)) for h in others]
            best = int(np.argmax(sims))
            if sims[best] < merge_sim:
                continue
            src, dst = g, others[best]
            if cur.index(dst) < cur.index(src):
                src, dst = dst, src           # 保留先出现者的标签
            print(
                f"[diar] 过拆收敛：{src}({labs.count(src)} 段) 并入 {dst}"
                f"（中心相似度 {sims[best]:.2f}）"
            )
            for k, l in enumerate(labs):
                if l == src:
                    labs[k] = dst
            mapping[src] = dst
            merged += 1
            cur = [h for h in cur if h != src]
            changed = True
            break

    if not merged:
        return labels, 0
    return [mapping.get(l, l) if l else l for l in labels], merged


def _assign_from_intervals(whisper_segments, intervals: list[tuple[float, float, str]]) -> list[str | None]:
    """按时间重叠把区间级说话人标签映射到每个 ASR 段。"""
    labels: list[str | None] = []
    for seg in whisper_segments:
        start, end = float(seg["start"]), float(seg["end"])
        best, best_overlap = None, 0.0
        for a, b, speaker in intervals:
            overlap = min(end, b) - max(start, a)
            if overlap > best_overlap:
                best_overlap, best = overlap, speaker
        labels.append(best)
    return labels


def _speakers_via_pyannote(pipeline, whisper_segments, waveform, cfg: dict):
    import numpy as np
    import torch

    dia = cfg["diarization"]
    # pyannote 4.x 要求 Mapping 输入（channel, time 张量）
    audio_input = {
        "waveform": torch.from_numpy(np.ascontiguousarray(waveform)).float().unsqueeze(0),
        "sample_rate": int(cfg["audio"]["asr_sample_rate"]),
    }
    df = pipeline(
        audio_input,
        min_speakers=dia.get("min_speakers"),
        max_speakers=dia.get("max_speakers"),
    )
    # pyannote 4.x 返回 DiarizeOutput（.speaker_diarization 为 Annotation）；
    # 3.x 返回 pandas DataFrame
    if hasattr(df, "speaker_diarization"):
        ann = df.speaker_diarization
        intervals = [
            (float(seg.start), float(seg.end), str(label))
            for seg, _, label in ann.itertracks(yield_label=True)
        ]
    else:
        speaker_col = "speaker" if "speaker" in df.columns else "label"
        intervals = [
            (float(row["start"]), float(row["end"]), str(row[speaker_col]))
            for _, row in df.iterrows()
        ]
    return _assign_from_intervals(whisper_segments, intervals)


def _embed_inference(cfg: dict):
    """wespeaker 声纹推理器（模块级缓存：auto 预判与过拆合并共用一次加载）。"""
    import torch
    from pyannote.audio import Inference, Model

    global _EMBED_CACHE
    key = (str(cfg["diarization"]["embedding_model"]), str(cfg["asr"]["device"]))
    if _EMBED_CACHE and _EMBED_CACHE[0] == key:
        return _EMBED_CACHE[1]
    device = torch.device(cfg["asr"]["device"] if torch.cuda.is_available() else "cpu")
    model = Model.from_pretrained(cfg["diarization"]["embedding_model"])
    inference = Inference(model, window="whole", device=device)
    _EMBED_CACHE = (key, inference)
    return inference


_EMBED_CACHE: tuple | None = None


def _extract_embeddings(whisper_segments, waveform, cfg: dict):
    """wespeaker 声纹向量提取（auto 预判与 embedding 聚类共用）。

    返回 (归一化向量矩阵, 对应段下标列表)；不足 2 段时返回 (None, [])。
    """
    import numpy as np
    import torch

    dia = cfg["diarization"]
    sample_rate = cfg["audio"]["asr_sample_rate"]
    min_seconds = float(dia["min_embed_seconds"])
    inference = _embed_inference(cfg)

    vectors: list = []
    indices: list[int] = []
    for i, seg in enumerate(whisper_segments):
        start = max(0, int(float(seg["start"]) * sample_rate))
        end = min(len(waveform), int(float(seg["end"]) * sample_rate))
        clip = waveform[start:end]
        if len(clip) < int(min_seconds * sample_rate):
            continue
        emb = inference(
            {"waveform": torch.from_numpy(clip).float().unsqueeze(0), "sample_rate": sample_rate}
        )
        vectors.append(np.asarray(emb, dtype=np.float32).reshape(-1))
        indices.append(i)

    if len(vectors) < 2:
        return None, []
    matrix = np.stack(vectors)
    return matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9), indices


def _auto_detect_solo(whisper_segments, waveform, cfg: dict) -> bool | None:
    """自动判断全片是否单人。返回 True=单人 / False=多人 / None=无法判断。

    用 wespeaker 向量做两重判定（比直接信聚类数保守）：
        1. 层次聚类只有 1 类 → 单人
        2. 聚出多类但类间中心相似度仍 > SOLO_MERGE_SIM（阈值偏松，
           防同一人被过拆）→ 仍判单人
    其余判多人，交回完整 pyannote 精判。加载 wespeaker 仅数百 MB，
    比 pyannote 3.1（约 4GB 显存）轻一个量级。
    """
    import numpy as np
    from sklearn.cluster import AgglomerativeClustering

    matrix, _ = _extract_embeddings(whisper_segments, waveform, cfg)
    if matrix is None or len(matrix) < 2:
        return None
    labels = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=float(cfg["diarization"]["clustering_threshold"]),
        metric="cosine",
        linkage="average",
    ).fit_predict(matrix)
    groups = sorted(set(labels))
    if len(groups) <= 1:
        return True
    centers = np.stack([matrix[labels == g].mean(axis=0) for g in groups])
    centers = centers / (np.linalg.norm(centers, axis=1, keepdims=True) + 1e-9)
    sim_matrix = centers @ centers.T
    off_diag = sim_matrix[~np.eye(len(groups), dtype=bool)]
    return bool(off_diag.min() > SOLO_MERGE_SIM)


def _speakers_via_embedding(whisper_segments, waveform, cfg: dict):
    """本地声纹嵌入 + 层次聚类，无需 token。"""
    import numpy as np
    from sklearn.cluster import AgglomerativeClustering

    dia = cfg["diarization"]

    matrix, indices = _extract_embeddings(whisper_segments, waveform, cfg)
    if matrix is None:
        return None

    min_speakers = dia.get("min_speakers")
    max_speakers = dia.get("max_speakers")
    fixed = int(min_speakers) if (min_speakers and min_speakers == max_speakers) else None

    if fixed:
        labels = AgglomerativeClustering(
            n_clusters=fixed, metric="cosine", linkage="average"
        ).fit_predict(matrix)
    else:
        labels = AgglomerativeClustering(
            n_clusters=None,
            distance_threshold=float(dia["clustering_threshold"]),
            metric="cosine",
            linkage="average",
        ).fit_predict(matrix)

        found = len(set(labels))
        low = int(min_speakers) if min_speakers else 1
        high = int(max_speakers) if max_speakers else found
        if found < low or found > high:
            clamped = min(max(found, low), high)
            labels = AgglomerativeClustering(
                n_clusters=clamped, metric="cosine", linkage="average"
            ).fit_predict(matrix)

    result: list[str | None] = [None] * len(whisper_segments)
    for i, lab in zip(indices, labels):
        result[i] = f"SPK_{int(lab) + 1:02d}"
    return result


def assign_speakers(whisper_segments, waveform, cfg: dict) -> tuple[list[str], str]:
    """返回 (每段说话人标签, 使用的方法名)。

    pyannote 路径的优先级：HF token（官方门控权重）→ 魔搭本地管线（免 token）
    → 本地声纹聚类兜底。

    `single_speaker` 三态（project.json 顶层，老项目的布尔值自动归一化）：
        "on"   强制单人，跳过全部分离路径
        "off"  强制完整分离流程
        "auto" 默认。先用 wespeaker 轻量预判：单人 → 直接采用（省 pyannote
               约 4GB 显存）；多人 / 无法判断 → 走完整 pyannote 精判
    """
    dia = cfg["diarization"]
    method = str(dia.get("method", "auto")).lower()

    solo = cfg.get("single_speaker", "auto")
    if solo is True:
        solo = "on"
    elif solo is False:
        solo = "off"
    solo = str(solo).lower()

    if solo == "on" or method == "none" or not whisper_segments:
        return ["SPK_01"] * len(whisper_segments), "none(单主播模式)" if solo == "on" else "none(单说话人)"

    if solo == "auto":
        try:
            is_solo = _auto_detect_solo(whisper_segments, waveform, cfg)
        except Exception as exc:  # noqa: BLE001 - 预判失败不阻塞，回落完整流程
            print(f"[diar] 单主播预判失败，走完整分离：{type(exc).__name__}: {exc}")
            is_solo = None
        if is_solo is True:
            print("[diar] 自动判定：单人（wespeaker 预判），跳过 pyannote")
            return ["SPK_01"] * len(whisper_segments), "auto(单人)"
        if is_solo is False:
            print("[diar] 自动判定：多人，走完整 pyannote 分离")

    def _finalize(labels: list[str | None], name: str):
        """出口统一后处理：过拆收敛（失败不阻塞，保持原标签）+ 标签归一化。"""
        try:
            labels, n = _merge_oversplit(labels, whisper_segments, waveform, cfg)
            if n:
                print(f"[diar] 过拆收敛：合并 {n} 对类中心重合的说话人")
        except Exception as exc:  # noqa: BLE001 - 收敛失败不影响主流程
            print(f"[diar] 过拆收敛跳过：{type(exc).__name__}: {exc}")
        return _normalize_labels(labels), name

    token = hf_token(cfg)
    if method in ("auto", "pyannote") and token:
        try:
            from whisperx.diarize import DiarizationPipeline

            pipeline = DiarizationPipeline(
                model_name=dia["model"], token=token, device=cfg["asr"]["device"]
            )
            labels = _speakers_via_pyannote(pipeline, whisper_segments, waveform, cfg)
            if labels:
                return _finalize(labels, "pyannote")
        except Exception as exc:  # noqa: BLE001 - auto 模式下失败要退回本地
            if method == "pyannote":
                raise
            print(f"[diar] pyannote(HF) 不可用，尝试本地魔搭版：{type(exc).__name__}: {exc}")

    if method in ("auto", "pyannote"):
        local_dir = local_diarization_dir()
        if local_dir:
            try:
                pipeline = build_local_pipeline(local_dir, cfg["asr"]["device"])
                labels = _speakers_via_pyannote(pipeline, whisper_segments, waveform, cfg)
                if labels:
                    return _finalize(labels, "pyannote(魔搭本地)")
            except Exception as exc:  # noqa: BLE001
                if method == "pyannote":
                    raise
                print(f"[diar] pyannote(魔搭) 不可用，退回本地声纹聚类：{type(exc).__name__}: {exc}")

    if method == "pyannote" and not token:
        raise SystemExit(
            "diarization.method=pyannote 需要 HuggingFace token："
            "请设置环境变量 HF_TOKEN，或写进 project.json 的 diarization.token；"
            "或把魔搭权重放到 models/pyannote/speaker-diarization-3.1/。"
        )

    labels = _speakers_via_embedding(whisper_segments, waveform, cfg)
    if labels:
        return _finalize(labels, "embedding")
    return ["SPK_01"] * len(whisper_segments), "embedding(退化为单说话人)"


def _confidence(seg: dict) -> float:
    logprob = seg.get("avg_logprob")
    if logprob is None:
        return 1.0
    return round(min(1.0, max(0.0, math.exp(float(logprob)))), 4)


_PUNCT = (".", "?", "!", "。", "？", "！", ",", "，", ";", "；", ":", "：")
_MIN_PART_SECONDS = 1.0     # 均匀细分时单段下限
_MIN_BLOCK_SECONDS = 3.0    # 大停顿分块后，碎块合并进邻居的下限
_HARD_GAP = 0.8             # 词间停顿达到该值视为天然段边界


def _join_words(words: list[Word]) -> str:
    out = ""
    for w in words:
        t = w.w.strip()
        if not t:
            continue
        if out and t[0].isascii() and t[0].isalnum() and (
            (out[-1].isascii() and out[-1].isalnum()) or out[-1] in _PUNCT
        ):
            out += " "
        out += t
    return out


def _uniform_bounds(words: list[Word], a: int, b: int, max_sec: float) -> list[int] | None:
    """在 words[a:b] 内按停顿均匀选切点；不可切返回 None。"""
    part_dur = words[b - 1].end - words[a].start
    n_parts = math.ceil(part_dur / max_sec)
    if n_parts <= 1:
        return None

    target = part_dur / n_parts
    base_t = words[a].start
    candidates: list[tuple[int, float]] = []
    for i in range(a + 1, b):
        gap = words[i].start - words[i - 1].end
        punct = words[i - 1].w.rstrip().endswith(_PUNCT)
        # gap 封顶：长停顿只说明"此处可切"，不应独裁切点选择
        score = min(gap, 2.0) + (0.6 if punct else 0.0)
        if score > 0.15:
            candidates.append((i, score))
    if not candidates:
        return None

    bounds = [a]
    for p in range(1, n_parts):
        ideal = base_t + target * p
        best_i, best_rank = None, -1e9
        for i, score in candidates:
            if i <= bounds[-1]:
                continue
            rank = score - abs(words[i].start - ideal)
            if rank > best_rank:
                best_rank, best_i = rank, i
        if best_i is None:
            return None
        bounds.append(best_i)
    bounds.append(b)

    for x, y in zip(bounds, bounds[1:]):
        if words[y - 1].end - words[x].start < _MIN_PART_SECONDS:
            return None
    return bounds


def _split_bounds(words: list[Word], max_sec: float) -> list[int] | None:
    """返回切点索引（bounds[0]=0, bounds[-1]=len(words)），不可切时返回 None。

    三步：大停顿（>= _HARD_GAP）天然分块 -> 碎块（< _MIN_BLOCK_SECONDS）按
    "跨小间隙优先"合并进邻居 -> 仍超长的块按停顿均匀细分。
    大停顿边界优先保留，避免配音横跨原声的长静音区。
    """
    dur = words[-1].end - words[0].start
    if dur <= max_sec:
        return None

    hard = [0] + [i for i in range(1, len(words))
                  if words[i].start - words[i - 1].end >= _HARD_GAP] + [len(words)]
    blocks = [[a, b] for a, b in zip(hard, hard[1:])]

    changed = True
    while changed and len(blocks) > 1:
        changed = False
        for k, (a, b) in enumerate(blocks):
            if words[b - 1].end - words[a].start >= _MIN_BLOCK_SECONDS:
                continue
            left = blocks[k - 1] if k > 0 else None
            right = blocks[k + 1] if k + 1 < len(blocks) else None
            if left is None and right is None:
                break
            if right is None:
                target = k - 1
            elif left is None:
                target = k + 1
            else:
                gap_left = words[a].start - words[left[1] - 1].end
                gap_right = words[right[0]].start - words[b - 1].end
                target = k - 1 if gap_left <= gap_right else k + 1
            if target == k - 1:
                blocks[k - 1][1] = b
            else:
                blocks[k + 1][0] = a
            blocks.pop(k)
            changed = True
            break

    final = [0]
    for a, b in blocks:
        part = words[b - 1].end - words[a].start
        if b - a >= 2 and part > max_sec * 1.3:
            sub = _uniform_bounds(words, a, b, max_sec)
            if sub:
                final.extend(sub[1:])
                continue
        final.append(b)

    return final if len(final) > 2 else None


def split_segment(seg: Segment, max_sec: float) -> list[Segment]:
    """把超长段按词间停顿切成子段；无词级时间戳或不可切时原样返回。"""
    if max_sec <= 0 or not seg.words:
        return [seg]
    bounds = _split_bounds(seg.words, max_sec)
    if bounds is None:
        return [seg]

    out: list[Segment] = []
    for a, b in zip(bounds, bounds[1:]):
        part = seg.words[a:b]
        out.append(Segment(
            id=seg.id,
            speaker=seg.speaker,
            start=round(part[0].start, 3),
            end=round(part[-1].end, 3),
            text=_join_words(part) or seg.text,
            words=part,
            confidence=seg.confidence,
        ))
    return out


def build_transcript(project_id, source_lang, audio_rel, whisper_segments, speakers,
                     max_seg_sec: float = 0.0) -> Transcript:
    segments: list[Segment] = []
    counter = 0
    for i, seg in enumerate(whisper_segments):
        text = (seg.get("text") or "").strip()
        start, end = float(seg["start"]), float(seg["end"])
        if not text or end <= start:
            continue
        words = [
            Word(w=w["word"].strip(), start=round(float(w["start"]), 3), end=round(float(w["end"]), 3))
            for w in (seg.get("words") or [])
            if w.get("word") and w.get("start") is not None and w.get("end") is not None
        ]
        base = Segment(
            id="", speaker=speakers[i], start=round(start, 3), end=round(end, 3),
            text=text, words=words, confidence=_confidence(seg),
        )
        for piece in split_segment(base, max_seg_sec):
            counter += 1
            piece.id = f"seg_{counter:04d}"
            segments.append(piece)
    return Transcript(project_id, source_lang, audio_rel, segments)


def build_speakers(transcript: Transcript) -> dict:
    """给每个说话人挑最长的几段作参考音频（供 R1/R3 试听）。"""
    by_speaker: dict[str, list[Segment]] = {}
    for seg in transcript.segments:
        by_speaker.setdefault(seg.speaker, []).append(seg)

    out: dict[str, dict] = {}
    for speaker in sorted(by_speaker):
        ranked = sorted(by_speaker[speaker], key=lambda s: s.duration, reverse=True)[:3]
        out[speaker] = {
            "character": "",
            "ref_segments": [s.id for s in ranked],
            "notes": "",
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="阶段4：语音识别与说话人分离")
    ap.add_argument("project", help="项目目录")
    ap.add_argument("--force", action="store_true", help="强制重跑")
    ap.add_argument("--seg", action="append", help="（本阶段忽略，仅为与调度器参数对齐）")
    args = ap.parse_args()

    envs.ensure_env("env_asr", "whisperx")
    audio.use_bundled_ffmpeg()
    configure_hf()
    # silero-vad 走 torch.hub.load('snakers4/silero-vad')，torch 2.x 会先访问
    # GitHub API 解析默认分支再查缓存；改写为本地缓存目录加载，断网也能跑
    patch_torch_hub_local()

    project_dir = Path(args.project).resolve()
    project = load_project(project_dir)
    cfg = project["config"]
    from lib import gpu as gpu_mod

    cfg["asr"]["device"] = gpu_mod.effective_device(
        cfg["asr"]["device"], need_gb=6.0, stage="s04 ASR"
    )

    vocals = project_dir / INPUT_REL
    if not vocals.exists():
        raise SystemExit(f"缺少 {INPUT_REL}，请先运行 s03_denoise")

    work_dir = project_dir / "work" / "asr"
    asr_wav = work_dir / "asr_16k.wav"
    segments_path = project_dir / SEGMENTS_REL
    speakers_path = project_dir / SPEAKERS_REL

    def produce() -> list[str]:
        work_dir.mkdir(parents=True, exist_ok=True)
        prepare_16k(vocals, asr_wav, cfg["audio"]["asr_sample_rate"])
        result, waveform = run_asr(asr_wav, cfg)
        aligned = run_align(result, waveform, cfg)
        whisper_segments = aligned["segments"]
        speakers, method = assign_speakers(whisper_segments, waveform, cfg)
        transcript = build_transcript(
            project["project_id"], cfg["source_lang"], INPUT_REL, whisper_segments, speakers,
            max_seg_sec=float(cfg["asr"].get("max_segment_seconds") or 0.0),
        )
        save_transcript(project_dir, transcript, SEGMENTS_REL)
        save_json(speakers_path, build_speakers(transcript))
        longest = max((s.duration for s in transcript.segments), default=0.0)
        print(f"[asr] 分离方法={method}，共 {len(transcript.segments)} 段，{len(transcript.speakers)} 个说话人，最长段 {longest:.1f}s")
        return [SEGMENTS_REL, SPEAKERS_REL]

    ran = run_stage(
        project_dir, STAGE, [INPUT_REL], cfg, produce,
        config_keys=["audio", "asr", "diarization", "source_lang", "single_speaker"],
        force=args.force,
    )

    if ran or segments_path.exists():
        transcript = Transcript.from_dict(load_json(segments_path))
        print(f"说话人: {', '.join(transcript.speakers) or '（无）'}")
        print(f"提示：请核对 {SPEAKERS_REL}（R1 卡点），把 character 填成角色名。")


if __name__ == "__main__":
    main()
