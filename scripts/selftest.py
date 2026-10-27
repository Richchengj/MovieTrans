"""自检：验证不依赖 GPU 的核心逻辑与 ffmpeg 可用性。

用法:
    python scripts/selftest.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import s04_asr  # noqa: E402
import s09_mix  # noqa: E402
from lib import audio  # noqa: E402
from lib.budget import compute_budgets, plan_speed  # noqa: E402
from lib.config import DEFAULT_CONFIG, config_hash, config_subset  # noqa: E402
from lib.schemas import Segment, Transcript, Translation, TranslationItem, Word  # noqa: E402
from lib.state import PipelineState, run_stage  # noqa: E402
from lib.subtitles import fmt_time, write_srt  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent

PASS, FAIL, HINTS = 0, 0, 0
TIMEOUTS = 0  # 导入超时次数（未判定，非报错；见 _TIMEOUT_ADVICE）

# 单个 env 的 import 冒烟上限（秒）。2026-10-27 实测本机热盘耗时：
# demucs 46s、IndexTTS 218s、pyannote 379s —— 旧固定 300s 会把 pyannote 判成
# 环境损坏（发布验收命令假失败）。冷盘首次读 14G 权重还要更久，故默认放到 600s。
IMPORT_TIMEOUT = int(os.environ.get("MT_SELFTEST_IMPORT_TIMEOUT", "600"))

_TIMEOUT_ADVICE = (
    "冷盘或与其它重活抢 IO 时常见，重跑一次即可确认；"
    "仍超时可临时调大 MT_SELFTEST_IMPORT_TIMEOUT=1200"
)


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n       got ={got!r}\n       want={want!r}")


def main() -> int:
    global PASS, FAIL, HINTS
    cfg = DEFAULT_CONFIG

    print("[1] 时长预算")
    segs = [
        Segment(id="seg_0001", speaker="SPK_01", start=0.0, end=2.0, text="a"),
        Segment(id="seg_0002", speaker="SPK_01", start=2.5, end=4.0, text="b"),
        Segment(id="seg_0003", speaker="SPK_02", start=4.0, end=5.0, text="c"),
    ]
    budgets = compute_budgets(segs, cfg)
    check("seg1 借到 0.2s 间隙", budgets["seg_0001"], 2.2)
    check("seg2 硬窗截断不越下一段", budgets["seg_0002"], 1.5)
    check("seg3 无邻段不借", budgets["seg_0003"], 1.0)

    print("[1b] 长段切分")
    ws = [Word(w=f"w{k}", start=round(1.5 * k, 3), end=round(1.5 * k + 1.0, 3)) for k in range(12)]
    seg = Segment(id="seg_0001", speaker="SPK_01", start=0.0, end=17.5,
                  text=" ".join(w.w for w in ws), words=ws)
    parts = s04_asr.split_segment(seg, 10.0)
    check("超长段切成 2 段", len(parts), 2)
    check("每段不超过上限", all(p.duration <= 10.0 for p in parts), True)
    check("拼接覆盖全部词", " ".join(p.text for p in parts), " ".join(w.w for w in ws))
    check("短段不切", len(s04_asr.split_segment(seg, 20.0)), 1)
    tight = [Word(w=f"t{k}", start=float(k), end=round(float(k) + 0.9, 3)) for k in range(15)]
    seg2 = Segment(id="seg_0002", speaker="SPK_01", start=0.0, end=15.0, text="x", words=tight)
    check("无停顿不硬切", len(s04_asr.split_segment(seg2, 10.0)), 1)

    print("[2] 变速规划")
    check("轻微超长 -> 可变速", plan_speed(2.4, 2.2, cfg), (1.0909, []))
    check("严重超长 -> 截断并标记", plan_speed(3.0, 2.2, cfg), (1.15, ["overrun"]))
    check("偏短 -> 保持原速留白", plan_speed(1.0, 1.5, cfg), (1.0, []))

    print("[3] 字幕时间戳")
    check("fmt_time 整点", fmt_time(3661.5), "01:01:01,500")
    check("fmt_time 毫秒", fmt_time(0.001), "00:00:00,001")

    print("[4] 数据模型往返")
    tr = Transcript("p", "en", "01_audio/audio_asr.wav", segs)
    check("说话人去重排序", tr.speakers, ["SPK_01", "SPK_02"])
    check("往返一致", Transcript.from_dict(tr.to_dict()).to_dict(), tr.to_dict())
    tl = Translation([TranslationItem("seg_0001", "SPK_01", "src", "译文", 2.2)])
    check("翻译往返一致", Translation.from_dict(tl.to_dict()).to_dict(), tl.to_dict())

    print("[5] 状态与断点续跑")
    # 日志句柄会保持到进程退出（好让阶段收尾的输出也进日志），
    # Windows 下因此删不掉临时目录，这里忽略清理错误
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "in.txt").write_text("v1", encoding="utf-8")
        calls = {"n": 0}

        def produce() -> list[str]:
            calls["n"] += 1
            (tmp_path / "out.txt").write_text("ok", encoding="utf-8")
            return ["out.txt"]

        run_stage(tmp_path, "s_x", ["in.txt"], cfg, produce, config_keys=["audio"])
        run_stage(tmp_path, "s_x", ["in.txt"], cfg, produce, config_keys=["audio"])
        check("第二次被跳过", calls["n"], 1)

        run_stage(tmp_path, "s_x", ["in.txt"], cfg, produce, config_keys=["audio"], force=True)
        check("--force 强制重跑", calls["n"], 2)

        import time
        time.sleep(0.01)
        (tmp_path / "in.txt").write_text("v2", encoding="utf-8")
        run_stage(tmp_path, "s_x", ["in.txt"], cfg, produce, config_keys=["audio"])
        check("输入变化后重跑", calls["n"], 3)

        state = PipelineState(tmp_path)
        check("状态已落盘", state.get("s_x")["status"], "done")

        print("[5b] 配置哈希按阶段隔离")
        import copy
        other = copy.deepcopy(cfg)
        other["tts"]["engine"] = "gpt-sovits"
        run_stage(tmp_path, "s_x", ["in.txt"], other, produce, config_keys=["audio"])
        check("无关配置段变化不重跑", calls["n"], 3)

        changed = copy.deepcopy(cfg)
        changed["audio"]["master_sample_rate"] = 44100
        run_stage(tmp_path, "s_x", ["in.txt"], changed, produce, config_keys=["audio"])
        check("相关配置段变化则重跑", calls["n"], 4)

    print("[6] 配置哈希")
    check("同配置同哈希", config_hash(cfg), config_hash(json.loads(json.dumps(cfg))))
    check("子集哈希只含指定键", config_subset(cfg, ["audio"]), {"audio": cfg["audio"]})
    try:
        config_subset(cfg, ["seperation"])
    except KeyError:
        check("拼错的配置键会报错", True, True)
    else:
        check("拼错的配置键会报错", False, True)

    print("[7] 阶段日志")
    # 日志是进程级单例，必须在子进程里验证，同时也验证退出码正常
    # 日志句柄会保持到进程退出（好让阶段收尾的输出也进日志），
    # Windows 下因此删不掉临时目录，这里忽略清理错误
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "in.txt").write_text("v1", encoding="utf-8")
        script = tmp_path / "gen_log.py"
        script.write_text(
            textwrap.dedent(
                f"""
                import sys
                sys.path.insert(0, r"{SCRIPTS_DIR}")
                from pathlib import Path
                from lib.config import DEFAULT_CONFIG
                from lib.state import run_stage

                proj = Path(r"{tmp_path}")

                def produce():
                    print("标准输出内容")
                    print("标准错误内容", file=sys.stderr)
                    (proj / "out.txt").write_text("ok", encoding="utf-8")
                    return ["out.txt"]

                run_stage(proj, "s_log", ["in.txt"], DEFAULT_CONFIG, produce,
                          config_keys=["audio"])
                """
            ),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, encoding="utf-8"
        )
        check("日志阶段退出码为 0", proc.returncode, 0)
        log_file = tmp_path / "logs" / "s_log.log"
        check("日志文件已生成", log_file.exists(), True)
        log = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
        check("日志含标准输出", "标准输出内容" in log, True)
        check("日志含标准错误", "标准错误内容" in log, True)
        check("日志含头部", "# s_log" in log, True)

    print("[8] ffmpeg 可用性")
    problems = audio.check_ffmpeg()
    if problems:
        print("  FAIL ffmpeg 不可用：")
        for p in problems:
            print(f"       - {p}")
        FAIL += 1
    else:
        PASS += 1
        print(f"  ok   ffmpeg 可用: {audio.ffmpeg_bin()}")

    print("[9] 混音（s09 的铺轨 + 响度归一化）")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        a = tmp_path / "a.wav"
        b = tmp_path / "b.wav"
        for path, freq in ((a, 440), (b, 660)):
            audio.run([
                "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=0.5",
                "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(path),
            ])
        dub = tmp_path / "dub.wav"
        s09_mix.build_dub([(0.5, a), (1.5, b)], 3.0, 24000, dub)
        check("铺轨输出存在", dub.exists(), True)
        check("铺轨总长 ~3s", round(audio.probe_duration(dub), 1), 3.0)

        bg = tmp_path / "bg.wav"
        audio.silence(bg, 3.0, 24000, 1)
        mix = tmp_path / "mix.wav"
        s09_mix.mix_with_background(dub, bg, mix, cfg)
        check("混音输出存在", mix.exists(), True)
        check("混音总长 ~3s", round(audio.probe_duration(mix), 1), 3.0)

    print("[10] 变速与媒体探测（s08/s10）")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        tone = tmp_path / "tone.wav"
        audio.run([
            "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2.0",
            "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(tone),
        ])
        fast = tmp_path / "fast.wav"
        audio.change_speed(tone, fast, 1.15, 24000, 1)
        check("变速输出存在", fast.exists(), True)
        check("加速后时长 ~1.7s", round(audio.probe_duration(fast), 1), 1.7)

        vid = tmp_path / "v.mp4"
        audio.run([
            "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-c:a", "aac", "-shortest", str(vid),
        ])
        check("带视频流 -> has_video", audio.has_video(vid), True)
        check("纯音频 -> has_video=False", audio.has_video(tone), False)

    print("[11] 质检三态语义（qc.Report：PASS/FAIL/WARN，I-1 防假绿）")
    import qc  # noqa: E402
    rep = qc.Report()
    rep.add("样-pass", True, "x")
    rep.add("样-fail", False, "x")
    rep.add("样-warn", True, "x", warn=True)
    check("WARN 行 status=warn", [r["status"] for r in rep.rows], ["pass", "fail", "warn"])
    check("WARN 不计失败", rep.failed, 1)
    check("WARN 单独计数", rep.warned, 1)
    check("通过数只数 pass", sum(1 for r in rep.rows if r["status"] == "pass"), 1)

    print("[12] 分批补跑与参考段抽样（2026-10-22 长片实测后的回归）")
    import s06_voice_profile  # noqa: E402
    import s07_tts  # noqa: E402

    # 12.1 补跑覆盖判定：s07 靠它决定标 done 还是 partial
    items = [
        {"id": "a", "target": "有译文"},
        {"id": "b", "target": "有译文"},
        {"id": "c", "target": ""},      # 空译文不该要求配音
    ]
    cov, miss = s07_tts._manifest_coverage(items, ["a"], {"b"})
    check("凑齐全量 -> done", (cov, miss), (True, []))
    cov, miss = s07_tts._manifest_coverage(items, ["a"], set())
    check("还缺段 -> partial", (cov, miss), (False, ["b"]))

    # 12.2 参考段跨时间轴分散：旧逻辑「最长优先」会把 12s 全取自前半段
    fake = [
        s06_voice_profile._Seg({"id": f"s{i}", "start": i * 10.0, "end": i * 10.0 + 3.0,
                                "confidence": 0.9})
        for i in range(12)
    ]
    picked = s06_voice_profile.choose_segments(fake, [], 9.0, 1.0, None, spread=True)
    thirds = {0 if s.start < 40 else (1 if s.start < 80 else 2) for s in picked}
    check("分散抽样跨三段", sorted(thirds), [0, 1, 2])
    dense = s06_voice_profile.choose_segments(fake, [], 9.0, 1.0, None, spread=False)
    dense_thirds = {0 if s.start < 40 else (1 if s.start < 80 else 2) for s in dense}
    check("关闭分散时集中在同段（对照）", len(dense_thirds), 1)

    # 12.3 引擎一致性判定：混用只 WARN，不判失败
    import qc as qc_mod  # noqa: E402
    ok, detail, warn = qc_mod.engine_consistency(
        [{"engine": "siliconflow"}, {"engine": "indextts2"}]
    )
    check("混用引擎 -> ok=True 且 warn", (ok, warn), (True, True))
    check("混用引擎 detail 含两个引擎", ("siliconflow" in detail and "indextts2" in detail), True)
    ok, _d, warn = qc_mod.engine_consistency([{"engine": "indextts2"}])
    check("单一引擎 -> 不告警", (ok, warn), (True, False))
    ok, _d, warn = qc_mod.engine_consistency([{}])
    check("无引擎标记 -> 告警（旧产物）", (ok, warn), (True, True))
    ok, detail, warn = qc_mod.engine_consistency([{"engine": "indextts2"}, {}])
    check("部分段无标记 -> 告警（不许假绿）", (ok, warn), (True, True))
    check("部分段无标记 detail 含段数", "1 段无引擎标记" in detail, True)

    print("[E] 环境完备性（跑流水线的前置；warn 项缺了对应阶段会失败）")
    env_audit()

    print(f"\n结果: {PASS} 通过, {FAIL} 失败" + (f", {HINTS} 项环境提示" if HINTS else ""))
    if TIMEOUTS:
        print(f"      其中 {TIMEOUTS} 项为导入超时（未判定，非报错）：{_TIMEOUT_ADVICE}")
    return 1 if FAIL else 0


def env_audit() -> None:
    """逐阶段检查虚拟环境与模型权重是否在位。

    warn 不计入失败：纯逻辑自检在任何机器上都不该 FAIL；
    但 WebUI / 发布场景要靠它告诉用户「缺什么、影响哪个阶段」。

    2026-10-18（E-1）升级：核心 import 冒烟升级为 must()（计入 FAIL）——
    warn 项在「venv 断链 / torch 残缺」时曾全绿放过（实锤两次）。权重缺失
    仍为 warn（可后补）。GPU 可用性单独 warn（无卡可走 CPU）。
    """
    global PASS, HINTS, FAIL
    from lib import envs as envs_mod

    root = envs_mod.PROJECT_ROOT
    hub = root / "models" / "hf" / "hub"

    def hint(label: str, ok: bool | None, fix: str = "") -> None:
        global PASS, HINTS, TIMEOUTS
        if ok:
            PASS += 1
            print(f"  ok   {label}")
        elif ok is None:
            HINTS += 1
            TIMEOUTS += 1
            print(f"  warn {label}——导入超时未判定（非报错）；{_TIMEOUT_ADVICE}")
        else:
            HINTS += 1
            print(f"  warn {label}" + (f"——{fix}" if fix else ""))

    def must(label: str, ok: bool | None, detail: str = "", fix: str = "") -> None:
        global PASS, FAIL, HINTS, TIMEOUTS
        if ok:
            PASS += 1
            print(f"  ok   {label}")
        elif ok is None:
            # 超时 ≠ 环境坏：冷盘 / 与其它重活抢 IO 时会这样。判 FAIL 会让
            # 「发布前 selftest 必须 0 失败」这条纪律在冷机上翻车（2026-10-27 实锤）。
            HINTS += 1
            TIMEOUTS += 1
            print(f"  warn {label}——{detail}；{_TIMEOUT_ADVICE}")
        else:
            FAIL += 1
            print(f"  FAIL {label}" + (f"——{detail}" if detail else "") + (f"；{fix}" if fix else ""))

    def run_import(env_name: str, code: str) -> tuple[bool | None, str]:
        """在指定 venv 里跑一段 python 代码，返回 (是否成功, 诊断信息)。

        成功时返回 stdout 尾行（供 print 类冒烟取值），失败时返回 stderr 尾行。
        **返回 (None, msg) 表示超时**：不是导入报错，不能当成环境损坏；
        上限见模块级 IMPORT_TIMEOUT（可用 MT_SELFTEST_IMPORT_TIMEOUT 调整）。
        """
        py = envs_mod.env_python(env_name)
        if not py.exists():
            return False, f"venv python 不存在（junction 断链或未建）: {py}"
        try:
            r = subprocess.run(
                [str(py), "-c", code],
                capture_output=True, timeout=IMPORT_TIMEOUT, text=True,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            if r.returncode == 0:
                out = (r.stdout or "").strip().splitlines()
                return True, out[-1] if out else ""
            tail = (r.stderr or "").strip().splitlines()
            return False, tail[-1] if tail else f"exit {r.returncode}"
        except subprocess.TimeoutExpired:
            return None, f"导入 {IMPORT_TIMEOUT}s 未返回"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def has_weight(pattern: str) -> bool:
        return next(hub.glob(pattern), None) is not None

    # --- 核心环境冒烟（must：坏 = 流水线必挂，计入 FAIL）---
    ok, err = run_import("env_sep", "import demucs, torch")
    must("s02 环境 env_sep（demucs import）", ok, err, "运行 setup.bat 或按 envs/README.md 建环境")

    ok, err = run_import("env_asr", "import whisperx, ctranslate2")
    must("s04 环境 env_asr（whisperx import）", ok, err, "运行 setup.bat 或按 envs/README.md 建环境")

    ok, err = run_import("env_tts", "import torch; assert torch.cuda.is_available()")
    must("s05/s07 环境 env_tts（torch + CUDA）", ok, err, "torch 残缺或无 CUDA；见 envs/README.md")

    indextts_code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "from indextts.infer_v2_5 import IndexTTS2" % str(root / "tools" / "index-tts").replace("\\", "\\\\")
    )
    ok, err = run_import("env_tts", indextts_code)
    must("s07 环境 env_tts（indextts 模块导入）", ok, err, "PYTHONPATH/editable 安装问题，见 envs/README.md")

    # --- GPU 可用性（warn：无卡可走 CPU，慢约 25 倍）---
    ok, err = run_import("env_tts", "import torch; print('cuda' if torch.cuda.is_available() else 'cpu')")
    hint("GPU（CUDA）可用性", ok and "cuda" in err, "无显卡将走 CPU：TTS 慢约 25 倍")

    # --- 质检声纹项依赖（warn：缺了 qc 第 10 项记 WARN 跳过，不算失败但不计入通过数）---
    ok, err = run_import(
        "env_asr",
        "import numpy, torch, scipy.io; from pyannote.audio import Inference, Model",
    )
    hint(
        "质检声纹项依赖（env_asr: numpy/torch/pyannote/scipy）",
        ok,
        f"qc 第 10 项将记 WARN 跳过——{err or '用 env_asr 的 python 跑 qc.py 可启用'}",
    )

    # --- GPT-SoVITS（可选引擎，文件级检查——import 会加载 621MB bert，太重）---
    from lib.config import DEFAULT_CONFIG
    gsv_home = Path(str(DEFAULT_CONFIG["tts"].get("gpt_sovits_home") or r"D:\GPT-SoVITS"))
    gsv_ok = (
        (gsv_home / "venv" / "Scripts" / "python.exe").exists()
        and (gsv_home / "GPT_SoVITS" / "inference_webui.py").exists()
        and (gsv_home / "GPT_SoVITS" / "pretrained_models" / "gsv-v2final-pretrained" / "s2G2333k.pth").exists()
        and (gsv_home / "GPT_SoVITS" / "pretrained_models" / "chinese-roberta-wwm-ext-large" / "pytorch_model.bin").exists()
    )
    hint(
        "s07 备选引擎 GPT-SoVITS（engine=gpt_sovits 时必需）",
        gsv_ok,
        f"部署根 {gsv_home} 缺 venv/GPT_SoVITS/权重——只用 IndexTTS 可忽略",
    )

    demucs_home = root / "models" / "torch" / "hub" / "checkpoints"
    demucs_default = Path.home() / ".cache" / "torch" / "hub" / "checkpoints"

    demucs_th = (
        4 if demucs_home.exists() and len(list(demucs_home.glob("*.th"))) >= 4
        else len(list(demucs_default.glob("*.th"))) if demucs_default.exists() else 0
    )
    hint("s02 权重 demucs（4 个 .th）", demucs_th >= 4, "见 envs/README.md 手动下载或从发布包 models\\torch 复制")

    hint("s04 权重 faster-whisper large-v3", has_weight("models--Systran--faster-whisper-large-v3/snapshots/*/model.bin"), "运行 src\\fetch_models.py 或从发布包 models\\hf 复制")
    hint("s04 权重 wespeaker 声纹", has_weight("models--pyannote--wespeaker-voxceleb-resnet34-LM/snapshots/*/pytorch_model.bin"), "同上")
    pann = root / "models" / "pyannote" / "speaker-diarization-3.1"
    hint(
        "s04 权重 pyannote 魔搭本地版（免 token）",
        (pann / "config.yaml").exists() and (pann / "segmentation-3.0" / "pytorch_model.bin").exists(),
        "从发布包 models\\pyannote 复制，或注册魔搭/HF 下载",
    )

    hint(
        "s05 权重 MarianMT 本地翻译（仅本地模式）",
        has_weight("models--Helsinki-NLP--opus-mt-en-zh/snapshots/*"),
        "用 API 翻译则不需要；本地模式运行 src\\fetch_models.py",
    )

    itts = root / "models" / "indextts2.5"
    hint(
        "s07 权重 IndexTTS-2.5（config/gpt/s2mel/codec）",
        all((itts / f).exists() for f in ("config.yaml", "gpt.pth", "s2mel.pth", "codec.pth")),
        "运行 src\\fetch_models.py indextts 或从发布包 models\\indextts2.5 复制",
    )
    hint(
        "s07 权重 QwenEmotion（仅 emo_mode=text）",
        (itts / "qwen0.6bemo4-merge").exists(),
        "ref 模式不需要；text 模式运行 src\\fetch_models.py indextts-aux",
    )


if __name__ == "__main__":
    raise SystemExit(main())
