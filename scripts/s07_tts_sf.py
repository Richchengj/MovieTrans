"""阶段7（硅基流动 CosyVoice2 API 版）：云端克隆 TTS。

定位（ROADMAP F-1 / 触发式计划）：
    本机引擎（IndexTTS-2.5 / GPT-SoVITS）是首选；本引擎是「显卡被占/无卡、
    本地合成不可接受」时的逃生通道，以及批量时避免 GPU 独占的备选。
    注意：消耗硅基流动账户余额，且参考音频会上传到云端（隐私自酌）。

API 契约（https://docs.siliconflow.cn，2026-10-21 实测修正）：
    ① 上传克隆音色（每说话人一次）:
       POST {base}/uploads/audio/voice   multipart: file, model, customName, text
       （text = 参考音频的文字内容，必填；本项目从 profiles.ref_segments 的
        译文源句重建）-> {"uri": "speech:<uid>:<name>:<id>"}
    ② 合成:
       POST {base}/audio/speech          （OpenAI 兼容；旧路径
       {"model", "input", "voice", "response_format": "wav",   /audio/generations
        "sample_rate", "speed"}                                  已 404 下线）
       -> audio bytes

音色 URI 以 project.json 同级的 05_voices/<SPK>/siliconflow_voice.json 缓存
（记录参考音频 md5，参考音频变了才重新上传）。

实现纯标准库（urllib），不依赖任何 venv——env_base 即可跑。
与 IndexTTS/GSV 版产物格式一致：24k 单声道 wav，s08 变速兜底。
WebUI 配置页可选引擎、填 Key，并有「测试连通性」按钮（端到端小样合成）。

✅ 状态：2026-10-21 实测通过（账户余额充足）。zero2 六个极端段 6/6 合成
成功；修复三处 API 契约问题——合成端点 404（/audio/generations →
/audio/speech）、上传音色补参考文本 text、缓存音色失效时合成遇 5xx
自动重传换 URI（退避 2s→10s→30s）+ 上传后生效等待 8s。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "FunAudioLLM/CosyVoice2-0.5B"
TIMEOUT = 120
# 新上传音色的生效等待（秒）：实测立即合成会 500（code 50507）
VOICE_PROPAGATION_SLEEP = 8
VOICE_CACHE_NAME = "siliconflow_voice.json"


class SiliconFlowError(RuntimeError):
    pass


def _md5(path: Path) -> str:
    h = hashlib.md5()
    h.update(path.read_bytes())
    return h.hexdigest()


def _ref_text(ref: Path) -> str:
    """重建参考音频的文字内容（上传音色 API 的必填 text 字段）。

    参考音频路径约定：<project>/05_voices/<SPK>/reference.wav。
    文本 = profiles.json 该 SPK 的 ref_segments 对应译文源句拼接；
    任一环节缺失就返回空串（上传端会报错，好过静默上传无文本音色）。
    """
    try:
        project = ref.parent.parent.parent
        profiles = json.loads(
            (project / "05_voices" / "profiles.json").read_text(encoding="utf-8"))
        spk = profiles.get(ref.parent.name) or {}
        seg_ids = list(spk.get("ref_segments") or [])
        tr_path = project / "04_translate" / "translation.json"
        if tr_path.exists():
            segs = json.loads(tr_path.read_text(encoding="utf-8")).get("segments", [])
            by_id = {s["id"]: s for s in segs}
            texts = [by_id[i]["source"] for i in seg_ids
                     if i in by_id and by_id[i].get("source")]
            if texts:
                return " ".join(texts)
        asr_path = project / "03_asr" / "segments.json"
        if asr_path.exists():
            segs = json.loads(asr_path.read_text(encoding="utf-8")).get("segments", [])
            by_id = {s["id"]: s for s in segs}
            texts = [by_id[i]["text"] for i in seg_ids
                     if i in by_id and by_id[i].get("text")]
            if texts:
                return " ".join(texts)
    except Exception:  # noqa: BLE001 - 重建失败不阻塞，交给上传端报错
        pass
    return ""


def _post_json(base: str, api_key: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise SiliconFlowError(f"HTTP {exc.code} {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SiliconFlowError(f"网络错误 {path}: {exc}") from exc


def _post_multipart(base: str, api_key: str, path: str,
                    file_field: str, file_path: Path, fields: dict) -> dict:
    boundary = "----MovieTransBoundary7d1a2c"
    parts: list[bytes] = []
    for k, v in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n"
            .encode("utf-8"))
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{file_path.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
        .encode("utf-8"))
    parts.append(file_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise SiliconFlowError(f"HTTP {exc.code} {path}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SiliconFlowError(f"网络错误 {path}: {exc}") from exc


class SiliconFlowTTS:
    """与 IndexTTS2 引擎鸭子类型兼容：.infer(spk_audio_prompt=, text=, output_path=, ...)。

    duration_factor 语义对齐：factor = 预算/实际，<1 表示要更快 →
    映射为 API 的 speed = 1/factor（夹到 [0.5, 4.0]，官方 speed 范围）。
    """

    def __init__(self, cfg: dict):
        sf = cfg["tts"].get("siliconflow") or {}
        self.base = str(sf.get("base_url") or DEFAULT_BASE)
        self.model = str(sf.get("model") or DEFAULT_MODEL)
        self.sample_rate = int(cfg["tts"].get("sample_rate") or 24000)
        self.api_key = (
            sf.get("api_key")
            or (cfg.get("translation") or {}).get("api_key")
            or os_env_key()
        )
        if not self.api_key:
            raise SystemExit(
                "tts.engine=siliconflow 需要 API Key：project.json 的 "
                "tts.siliconflow.api_key（或复用 translation.api_key，"
                "或环境变量 SILICONFLOW_API_KEY）"
            )
        self._voice_cache: dict[str, str] = {}

    def _voice_uri(self, ref: Path, tag: str, force: bool = False) -> str:
        """上传参考音频获取 voice URI（md5 缓存，参考音频没变不重传）。

        现行 API（2026-10-21 实测，docs.siliconflow.cn）：上传必须带参考音频的
        文字内容 text，否则音色缺少文本锚点。文本从项目产物重建：
        profiles.json 的 ref_segments → translation.json 的 source 拼接。

        force=True 忽略缓存强制重传：实测云端个别音色 URI 会失效
        （合成持续 500 code 50507，重传得到的新 URI 立即恢复），
        调用方在合成遇 5xx 时用来换一个音色。
        """
        cache_file = ref.parent / VOICE_CACHE_NAME
        digest = _md5(ref)
        if not force and cache_file.exists():
            try:
                meta = json.loads(cache_file.read_text(encoding="utf-8"))
                if meta.get("md5") == digest and meta.get("voice_uri"):
                    return meta["voice_uri"]
            except Exception:  # noqa: BLE001 - 缓存坏了就走重新上传
                pass
        name = f"movietrans_{tag}_{digest[:10]}_{int(time.time()) % 100000}"
        resp = _post_multipart(
            self.base, self.api_key, "/uploads/audio/voice",
            "file", ref, {
                "model": self.model, "customName": name,
                "text": _ref_text(ref),
            },
        )
        uri = resp.get("voiceUri") or resp.get("uri") or ""
        if not uri:
            raise SiliconFlowError(f"上传音色未返回 voiceUri：{json.dumps(resp)[:300]}")
        cache_file.write_text(json.dumps({
            "md5": digest, "voice_uri": uri, "model": self.model,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        # 新音色不是即时生效：实测上传后立即合成会 500（code 50507），
        # 需等一段时间才可用——先睡一会儿，给后续 infer 的重试兜底
        time.sleep(VOICE_PROPAGATION_SLEEP)
        return uri

    def infer(self, spk_audio_prompt: str, text: str, output_path: str,
              lang: str = "zh", duration_factor: float = 1.0, **_: object) -> None:
        ref = Path(spk_audio_prompt)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        voice = self._voice_uri(ref, ref.parent.name)
        speed = max(0.5, min(4.0, 1.0 / max(float(duration_factor), 0.05)))
        # 现行端点（2026-10-21 实测）：/v1/audio/speech（OpenAI 兼容）。
        # 旧路径 /v1/audio/generations 已 404 下线
        body = {
            "model": self.model,
            "input": text,
            "voice": voice,
            "response_format": "wav",
            "sample_rate": self.sample_rate,
            "speed": round(speed, 2),
        }
        last_err: Exception | None = None
        # 新上传音色生效有延迟（实测 500 code 50507），退避逐次拉长：
        # 2s → 10s → 30s；首次 5xx 还强制重传音色（缓存 URI 可能已失效）
        for attempt, backoff in enumerate((2, 10, 30)):
            try:
                req = urllib.request.Request(
                    self.base.rstrip("/") + "/audio/speech",
                    data=json.dumps(body).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    data = r.read()
                if len(data) < 1000:
                    raise SiliconFlowError(f"响应过短（{len(data)} B），疑似错误信息")
                out.write_bytes(data)
                return
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                # 4xx 是请求/账户问题，重试无意义，直接抛
                if 400 <= exc.code < 500 and exc.code != 429:
                    raise SiliconFlowError(
                        f"HTTP {exc.code} /audio/speech: {detail}") from exc
                last_err = SiliconFlowError(f"HTTP {exc.code}: {detail}")
                if attempt == 0:
                    # 缓存音色可能已失效：重传换一个新 URI 再试
                    voice = self._voice_uri(ref, ref.parent.name, force=True)
                    body["voice"] = voice
            except (SiliconFlowError, urllib.error.URLError) as exc:
                last_err = exc
            time.sleep(backoff)
        raise SiliconFlowError(f"合成失败（{seg_hint(text)}）: {last_err}")


def os_env_key() -> str | None:
    return os.environ.get("SILICONFLOW_API_KEY")


def seg_hint(text: str) -> str:
    return (text[:20] + "…") if len(text) > 20 else text


def load_engine(cfg: dict) -> "SiliconFlowTTS":
    """s07_tts.load_engine 的 siliconflow 分支（鸭子类型与 IndexTTS2 一致）。"""
    return SiliconFlowTTS(cfg)
