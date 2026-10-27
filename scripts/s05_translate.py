"""阶段5：长度受控翻译（英文 -> 中文）。

产物:
    04_translate/translation.json   译文 + 时长预算
    04_translate/review.csv         人工复核表（R2 卡点）

翻译后端（`translation.provider`）：
    deepseek / openai / openai_compatible  走 LLM API（质量好，需要 API Key）
    local                                   本地 MarianMT（opus-mt-en-zh），无需 Key

API Key 解析顺序：project.json 的 `translation.api_key` -> 环境变量
`DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `DUB_LLM_API_KEY`。
没有 Key 且 `translation.fallback_local` 为 true 时，自动退回本地模型，
保证链路能跑通（译文质量会明显下降，务必在 R2 复核）。

时长预算是 SPEC 5.1：原句时长 + 可借用间隙。译文按每秒 `chars_per_second`
个汉字估算时长，据此标记 strategy：
    native    译文放得下
    compress  略超预算，需要压缩
    review    明显超预算或翻译失败，交人工

用法:
    python scripts/s05_translate.py projects/20261008_demo_clip [--force] [--seg seg_0001]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from lib import envs
from lib.budget import compute_budgets
from lib.config import load_project
from lib.schemas import (
    Translation,
    TranslationItem,
    load_json,
    load_transcript,
    resolve_edited,
    save_translation,
)
from lib.state import run_stage

STAGE = "s05_translate"
INPUT_REL = "03_asr/segments.json"
OUTPUT_REL = "04_translate/translation.json"
REVIEW_REL = "04_translate/review.csv"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HF_HOME = PROJECT_ROOT / "models" / "hf"
HF_HUB_DIR = HF_HOME / "hub"

API_PROVIDERS = {"deepseek", "openai", "openai_compatible"}
DEFAULT_BASE_URLS = {
    "deepseek": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
}
API_KEY_ENV = ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "DUB_LLM_API_KEY")

SYSTEM_PROMPT = (
    "你是资深的影视配音翻译。把英文对白翻译成自然、口语化的简体中文，用于配音（不是字幕）。\n"
    "硬性要求：\n"
    "1. 只输出一个 JSON 对象，键是句子 id，值是译文；不要任何解释、注释或 markdown 代码块。\n"
    "2. 译文要地道、口语化，贴合角色语气，不要逐字直译。\n"
    "3. 每句译文尽量不超过给定的 max_chars（按每秒约 {cps} 个汉字估算）；意思优先，必要时精简。\n"
    "4. 人名、专有名词在全片保持统一。\n"
    "5. 译文里不要出现英文原句。"
)


def api_key(cfg: dict) -> str | None:
    configured = cfg["translation"].get("api_key")
    if configured:
        return configured
    for name in API_KEY_ENV:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _api_body(tr: dict, messages: list[dict]) -> dict:
    """构造 chat/completions 请求体。

    Qwen3 / GLM 系列是混合思考模型，硅基流动上默认开思考——实测翻译批处理会
    生成超长 reasoning 直到超时（2026-10-20 BCS 实锤：150s 无响应）。这两系
    统一注入 enable_thinking=false；deepseek 等其它模型不传，避免兼容性问题。
    """
    body = {"model": tr["model"], "messages": messages, "temperature": 0.2}
    model = str(tr["model"])
    if "Qwen3" in model or "GLM" in model or "glm" in model:
        body["enable_thinking"] = False
    return body


def base_url(cfg: dict) -> str | None:
    tr = cfg["translation"]
    url = tr.get("base_url") or DEFAULT_BASE_URLS.get(tr["provider"])
    if not url:
        raise SystemExit(
            f"translation.base_url 未配置：provider={tr['provider']!r} 不是内置后端，"
            "请在 project.json 的 config.translation.base_url 填 API 地址"
            "（如 https://api.siliconflow.cn/v1）"
        )
    return url


def estimate_duration(text: str, chars_per_second: float) -> float:
    length = len(re.sub(r"\s", "", text or ""))
    if chars_per_second <= 0:
        return 0.0
    return round(length / chars_per_second, 3)


def max_chars(budget: float, chars_per_second: float, ratio: float) -> int:
    return max(1, int(budget * chars_per_second * ratio))

def _post_json(url: str, payload: dict, key: str, timeout: float) -> dict:
    """POST 并解析 JSON，带指数退避重试（零干预必需：网络抖动/网关超时
    不应让整条流水线报废——s05 是唯一强依赖外网的阶段）。
    重试 4 次（共 5 次尝试），退避 2/4/8/16s；4xx 参数类错误不重试。
    """
    data = json.dumps(payload).encode("utf-8")
    last_exc: Exception | None = None
    for attempt in range(5):
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code < 500 and exc.code != 429:
                raise  # 参数/鉴权错误，重试无意义
        except Exception as exc:  # noqa: BLE001 - 超时/连接类一律可重试
            last_exc = exc
        if attempt < 4:
            wait = 2 ** (attempt + 1)
            print(f"[translate] API 第 {attempt + 1} 次失败（{type(last_exc).__name__}），{wait}s 后重试")
            time.sleep(wait)
    raise last_exc  # type: ignore[misc]


def _parse_targets(content: str) -> dict[str, str]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    if isinstance(parsed, dict) and "translations" in parsed and isinstance(parsed["translations"], dict):
        parsed = parsed["translations"]
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items()}


GLOSSARY_PROMPT = (
    "你是影视翻译顾问。从下面的英文对白中提取人名与关键专有名词（地名/品牌/机构），"
    "给出全片统一的简体中文译名。只输出一个 JSON 对象，键是英文原名，值是中文译名；"
    "没有专有名词时输出 {}。人名按影视译名惯例，地名品牌用通行译名；"
    "对白中同一名称出现多次只需一个条目。不要输出任何解释。"
)


def _glossary_suffix(glossary: dict[str, str] | None) -> str:
    """把人名表拼成 system prompt 后缀；空表返回空串。"""
    if not glossary:
        return ""
    return "\n6. 人名与专有名词统一译名表（必须严格遵守，全片一致）：\n" + json.dumps(
        glossary, ensure_ascii=False
    )


def load_or_extract_glossary(
    items: list[dict], cfg: dict, key: str, project_dir: Path, force: bool
) -> dict[str, str]:
    """人名表：有缓存用缓存，否则用一次 LLM 调用从全片对白提取并落盘。

    失败不阻塞翻译主流程（返回空表，退回提示词里的「保持统一」软约束）。
    """
    tr = cfg["translation"]
    if not tr.get("glossary", True):
        return {}
    path = project_dir / "04_translate" / "glossary.json"
    if path.exists() and not force:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                print(f"[translate] 人名表：复用 {len(data)} 条（{path.name}）")
                return {str(k): str(v) for k, v in data.items()}
        except Exception:  # noqa: BLE001 - 坏缓存当作没有
            pass

    sources: list[str] = []
    seen: set[str] = set()
    for it in items:
        text = it["source"].strip()
        if text and text not in seen:
            seen.add(text)
            sources.append(text)
    # 控制提取调用的体积：全片对白很长时取前 200 行 / 6000 字符
    while sum(len(s) for s in sources) > 6000 or len(sources) > 200:
        sources.pop()
    if not sources:
        return {}

    try:
        url = base_url(cfg).rstrip("/") + "/chat/completions"
        body = _api_body(
            tr,
            [
                {"role": "system", "content": GLOSSARY_PROMPT},
                {"role": "user", "content": json.dumps(sources, ensure_ascii=False)},
            ],
        )
        data = _post_json(url, body, key, float(tr["timeout"]))
        glossary = {
            k: v
            for k, v in _parse_targets(data["choices"][0]["message"]["content"]).items()
            if k and v and k != "translations"
        }
    except Exception as exc:  # noqa: BLE001 - 提取失败不阻塞
        print(f"[translate] 人名表提取失败（继续用软约束）：{type(exc).__name__}: {exc}")
        return {}

    if glossary:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(glossary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[translate] 人名表：提取 {len(glossary)} 条 -> {path.name}")
    return glossary


def _translate_api(
    items: list[dict], cfg: dict, key: str, glossary: dict[str, str] | None = None
) -> tuple[dict[str, str], list[str]]:
    """返回 (id->译文, 失败的 id 列表)。"""
    tr = cfg["translation"]
    url = base_url(cfg).rstrip("/") + "/chat/completions"
    cps = _effective_cps(cfg)
    ratio = float(tr["max_length_ratio"])

    results: dict[str, str] = {}
    failed: list[str] = []

    batch_size = int(tr["batch_size"])
    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        payload_items = [
            {
                "id": it["id"],
                "speaker": it["speaker"],
                "source": it["source"],
                "budget_seconds": it["budget"],
                "max_chars": max_chars(it["budget"], cps, ratio),
            }
            for it in batch
        ]
        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT.format(cps=cps) + _glossary_suffix(glossary),
            },
            {"role": "user", "content": json.dumps(payload_items, ensure_ascii=False)},
        ]
        body = _api_body(tr, messages)

        got: dict[str, str] = {}
        for attempt in (1, 2):
            try:
                response = _post_json(url, body, key, float(tr["timeout"]))
                content = response["choices"][0]["message"]["content"]
                got = _parse_targets(content)
                break
            except (urllib.error.URLError, KeyError, IndexError, ValueError) as exc:
                print(f"[translate] 第 {i // batch_size + 1} 批第 {attempt} 次失败：{type(exc).__name__}: {exc}")

        for it in batch:
            target = got.get(it["id"], "").strip()
            if target:
                results[it["id"]] = target
            else:
                failed.append(it["id"])

        # 首轮译文超预算的，自动追加一轮压缩重试
        overlong = [
            it for it in batch
            if got.get(it["id"], "").strip()
            and estimate_duration(got[it["id"]], cps) > it["budget"]
        ]
        if overlong:
            _compress_overlong(overlong, cfg, key, got, glossary)
            for it in overlong:
                target = got.get(it["id"], "").strip()
                if target:
                    results[it["id"]] = target
    return results, failed


def _compress_overlong(
    overlong: list[dict], cfg: dict, key: str, got: dict[str, str],
    glossary: dict[str, str] | None = None,
) -> None:
    """对超预算的译文向 LLM 循环压缩（原地写回 got），最多 3 轮。

    首轮提示里 max_chars 只是软约束，LLM 偶尔超字数；这些句子若直接落到
    review，会被 s07 的 atempo 硬压 x1.4~2.9（听感"说话特别快"）。循环压缩
    把绝大多数句子压进预算：仍超长的保持 review 交人工（s07 兜底压时间轴）。
    """
    tr = cfg["translation"]
    cps = _effective_cps(cfg)
    url = base_url(cfg).rstrip("/") + "/chat/completions"

    def _over(txt: str, limit: int) -> bool:
        return len(re.sub(r"\s", "", txt)) > limit

    for round_no in (1, 2, 3):
        pending = []
        for it in overlong:
            limit = max_chars(it["budget"], cps, 1.0)
            if _over(got[it["id"]], limit):
                pending.append((it, limit))
        if not pending:
            return
        print(f"[translate] 压缩第 {round_no} 轮：{len(pending)} 段仍超预算")
        payload_items = [
            {
                "id": it["id"],
                "speaker": it["speaker"],
                "source": it["source"],
                "current_translation": got[it["id"]],
                "hard_max_chars": limit,
            }
            for it, limit in pending
        ]
        extra = ""
        if round_no >= 2:
            extra = (
                f"注意：这是第 {round_no} 轮压缩，上一版仍超字数。必须砍得更狠——"
                "宁可损失细节也要压进 hard_max_chars，主谓宾保住即可。\n"
            )
        messages = [
            {
                "role": "system",
                "content": (
                    "你是影视配音翻译。下面每句译文都太长，超出配音时长预算，需要压缩。\n"
                    + extra
                    + "只输出一个 JSON 对象，键是句子 id，值是压缩后的译文；不要任何解释。\n"
                    "硬性要求：译文长度（按字数）绝不超过 hard_max_chars；意思不能丢，"
                    "优先砍语气词和重复修饰，保留核心信息。"
                ),
            },
            {"role": "user", "content": json.dumps(payload_items, ensure_ascii=False)},
        ]
        if glossary:
            messages[0]["content"] += _glossary_suffix(glossary)
        body = _api_body(tr, messages)
        shorter: dict[str, str] = {}
        for attempt in (1, 2):
            try:
                response = _post_json(url, body, key, float(tr["timeout"]))
                content = response["choices"][0]["message"]["content"]
                shorter = _parse_targets(content)
                break
            except (urllib.error.URLError, KeyError, IndexError, ValueError) as exc:
                print(f"[translate] 压缩重试第 {attempt} 次失败：{type(exc).__name__}: {exc}")
                shorter = {}
        for it, limit in pending:
            alt = shorter.get(it["id"], "").strip()
            if not alt:
                continue
            if len(re.sub(r"\s", "", alt)) <= limit:
                got[it["id"]] = alt
                print(f"[translate] {it['id']} 压缩成功：{len(alt)} 字 ≤ {limit}")
            else:
                got[it["id"]] = alt  # 采纳更短版本（即使仍超），下一轮继续压
                print(f"[translate] {it['id']} 压缩后仍超（{len(alt)} > {limit}）")


def _effective_cps(cfg: dict) -> float:
    """估算用的有效语速：基础语速 × 合成提速系数。

    s07 合成时用 1/tempo_boost 的 duration_factor 提速，实际念白密度约为
    chars_per_second × tempo_boost 字/秒；估算不乘上它会把本来放得下的
    译文误标成 compress/review。
    """
    base = float(cfg["translation"]["chars_per_second"])
    boost = max(1.0, float(cfg["tts"].get("tempo_boost") or 1.0))
    return base * boost


def real_snapshot(model_name: str) -> str:
    """本地快照的真实路径（解析 models 目录的 junction）。"""
    from huggingface_hub import snapshot_download

    return os.path.realpath(
        snapshot_download(model_name, cache_dir=str(HF_HUB_DIR), local_files_only=True)
    )


def local_tokenizer_dir(model_name: str) -> str:
    """返回 sentencepiece 能打开的 ASCII 分词器目录。

    sentencepiece 的 C++ 层用窄字符打开文件，路径里含非 ASCII 字符（如项目根
    为中文路径时）会报 “No such file or directory”。`models` 是指向
    `C:\\dub_models` 的 junction，所以先解析真实路径；若仍含非 ASCII，
    把分词器文件复制到临时 ASCII 目录。（2026-10-18 起项目根为 ASCII 的
    `D:\\MovieTrans`，此分支主要作为兜底保留。）
    """
    snapshot = real_snapshot(model_name)
    if snapshot.isascii():
        return snapshot

    tmp = Path(tempfile.mkdtemp(prefix="marian_tok_"))
    for name in ("vocab.json", "source.spm", "target.spm", "tokenizer_config.json", "config.json"):
        src = Path(snapshot) / name
        if src.exists():
            shutil.copyfile(src, tmp / name)
    return str(tmp)


def _translate_local(items: list[dict], cfg: dict) -> dict[str, str]:
    """本地 MarianMT 兜底翻译，完全离线。"""
    import torch
    from transformers import MarianMTModel, MarianTokenizer

    tr = cfg["translation"]
    model_name = tr["local_model"]
    device = "cuda" if torch.cuda.is_available() else "cpu"

    snapshot = real_snapshot(model_name)
    tokenizer = MarianTokenizer.from_pretrained(local_tokenizer_dir(model_name))
    model = MarianMTModel.from_pretrained(snapshot).to(device)
    model.eval()

    results: dict[str, str] = {}
    batch_size = int(tr["batch_size"])
    sources = [it["source"] for it in items]
    for i in range(0, len(items), batch_size):
        chunk = items[i:i + batch_size]
        texts = sources[i:i + batch_size]
        batch = tokenizer(texts, return_tensors="pt", padding=True, truncation=True, max_length=512)
        batch = {k: v.to(device) for k, v in batch.items()}
        with torch.no_grad():
            generated = model.generate(**batch, max_new_tokens=512)
        decoded = tokenizer.batch_decode(generated, skip_special_tokens=True)
        for it, text in zip(chunk, decoded):
            results[it["id"]] = text.strip()
    return results


def write_review_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 人工用 Excel 打开，用 BOM 避免中文乱码
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["id", "speaker", "source", "target", "duration_budget",
                        "target_est_duration", "strategy", "notes"],
        )
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="阶段5：长度受控翻译")
    ap.add_argument("project", help="项目目录")
    ap.add_argument("--force", action="store_true", help="强制重跑")
    ap.add_argument("--seg", action="append", help="只翻译指定段落（可重复）")
    args = ap.parse_args()

    project_dir = Path(args.project).resolve()
    project = load_project(project_dir)
    cfg = project["config"]
    tr = cfg["translation"]

    transcript = load_transcript(project_dir, INPUT_REL)
    if not transcript.segments:
        raise SystemExit(f"{INPUT_REL} 里没有段落，请先运行 s04_asr")

    provider = tr["provider"]
    key = api_key(cfg) if provider in API_PROVIDERS else None
    fallback_note = ""
    if provider in API_PROVIDERS and key:
        backend = "api"
    elif tr["fallback_local"]:
        backend = "local"
        if provider in API_PROVIDERS:
            fallback_note = (
                f"[translate] ⚠ 未找到 {provider} 的 API Key，"
                f"自动退回本地模型 {tr['local_model']}（译文质量明显下降，"
                "新项目常见：new_project 建出的 project.json 没带 key）"
            )
    else:
        raise SystemExit(
            f"provider={provider} 需要 API Key：请设置环境变量 {'/'.join(API_KEY_ENV)}，"
            "或写进 project.json 的 translation.api_key；也可把 translation.fallback_local 设为 true。"
        )

    if backend == "local":
        # 先切环境再打印：切环境会用目标环境的 python 重跑本脚本，
        # 提示放前面会被打印两次。
        envs.ensure_env("env_tts", "sentencepiece")
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        os.environ["HF_HOME"] = str(HF_HOME)
    if fallback_note:
        print(fallback_note)

    budgets = compute_budgets(transcript.segments, cfg)
    cps = _effective_cps(cfg)
    ratio = float(tr["max_length_ratio"])

    selected = set(args.seg) if args.seg else None
    items = [
        {
            "id": seg.id,
            "speaker": seg.speaker,
            "source": seg.text,
            "budget": budgets.get(seg.id, seg.duration),
        }
        for seg in transcript.segments
        if selected is None or seg.id in selected
    ]
    if not items:
        raise SystemExit("没有可翻译的段落（检查 --seg 是否写错）")

    # 只翻译指定段落时，保留其余段落的既有译文
    existing: dict[str, TranslationItem] = {}
    if selected is not None:
        existing_path = resolve_edited(project_dir, OUTPUT_REL)
        if existing_path.exists():
            for item in Translation.from_dict(load_json(existing_path)).segments:
                existing[item.id] = item

    def produce() -> list[str]:
        gl: dict[str, str] = {}
        if backend == "api":
            gl = load_or_extract_glossary(items, cfg, key, project_dir, args.force)
            targets, failed = _translate_api(items, cfg, key, gl)
            # 云端漏译兜底：LLM 偶尔整批返回空/漏掉个别 id（2026-10-22 实测
            # 12 分钟片漏 6 段），这些段若留空，s07 会跳过 → 成片直接缺句。
            # fallback_local=true 时用本地 MarianMT 补上（质量一般但胜过空白），
            # 仍补不上的保持 review + "翻译失败，请手工补译"。
            missing = [it for it in items if not targets.get(it["id"], "").strip()]
            if missing and bool(cfg["translation"].get("fallback_local")):
                local = _translate_local(missing, cfg)
                filled = {k: v for k, v in local.items() if v.strip()}
                targets.update(filled)
                left = len(missing) - len(filled)
                print(f"[translate] 云端漏译 {len(missing)} 段，本地兜底补 {len(filled)} 段"
                      + (f"，仍缺 {left} 段" if left else ""))
        else:
            targets, failed = _translate_local(items, cfg), []

        rows: list[dict] = []
        segments: list[TranslationItem] = []
        for item in items:
            target = targets.get(item["id"], "")
            est = estimate_duration(target, cps)
            budget = item["budget"]

            notes = ""
            if not target:
                strategy = "review"
                notes = "翻译失败，请手工补译"
            elif est <= budget:
                strategy = "native"
            elif est <= budget * ratio:
                strategy = "compress"
                notes = "略超预算，建议精简"
            else:
                strategy = "review"
                notes = "明显超预算，请人工压缩"

            segments.append(
                TranslationItem(
                    id=item["id"], speaker=item["speaker"], source=item["source"],
                    target=target, duration_budget=budget, target_est_duration=est,
                    strategy=strategy, notes=notes,
                )
            )
            rows.append({
                "id": item["id"], "speaker": item["speaker"], "source": item["source"],
                "target": target, "duration_budget": budget, "target_est_duration": est,
                "strategy": strategy, "notes": notes,
            })

        for item in existing.values():
            if item.id not in {s.id for s in segments}:
                segments.append(item)

        segments.sort(key=lambda s: s.id)
        save_translation(project_dir, Translation(segments), OUTPUT_REL)
        write_review_csv(project_dir / REVIEW_REL, rows)

        counts: dict[str, int] = {}
        for row in rows:
            counts[row["strategy"]] = counts.get(row["strategy"], 0) + 1
        label = "local（⚠ 回退，无 Key）" if (backend == "local" and fallback_note) else backend
        print(f"[translate] 后端={label}，{len(segments)} 段，分布={counts}")
        return [OUTPUT_REL, REVIEW_REL]

    resolved_input = resolve_edited(project_dir, INPUT_REL)
    input_rel = str(resolved_input.relative_to(project_dir)).replace("\\", "/")

    ran = run_stage(
        project_dir, STAGE, [input_rel], cfg, produce,
        config_keys=["translation", "align", "source_lang", "target_lang"], force=args.force,
    )

    if ran or (project_dir / OUTPUT_REL).exists():
        print(f"提示：请核对 {REVIEW_REL}（R2 卡点），改好后另存为 "
              f"{OUTPUT_REL.replace('.json', '.edited.json')}")


if __name__ == "__main__":
    main()
