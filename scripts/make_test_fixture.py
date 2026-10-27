"""测试素材生成器（H-3）：合成带 ground truth 的英文对白场景。

用 IndexTTS2.5 合成可控的英文测试素材——已知几个说话人、谁说哪句（ground truth
随素材导出为 `<scene>.expected.json`），供 diarization / auto 判定 / QC 的回归测试。

场景（--list 查看）：
    A_mono  单人独白（1 音色 6 句）   -> auto 应判 1 人
    B_duo   双人对白（2 音色 8 句）   -> auto 应判 2 人
    C_trio  三人轮替（3 音色 9 句）   -> ⚠️ 音色 A/C 同源（同一讲师两段参考），
                                        diarization 会过拆（已知特性，作 R1 合并演示）

用法：
    python scripts/make_test_fixture.py --list               # 列场景与 ground truth
    python scripts/make_test_fixture.py                      # 合成全部（幂等，已存在跳过）
    python scripts/make_test_fixture.py --scenes A_mono,B_duo --refresh
    python scripts/make_test_fixture.py --make-projects      # 顺手建好测试项目（auto+继承翻译）

合成需 IndexTTS 环境：脚本自动 reexec 到 env_tts（与 s07 同模式）。
参考音频默认取已有测试项目的 05_voices（可 --ref A=路径 覆盖）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from lib import audio  # noqa: E402
from lib import envs as envs_mod  # noqa: E402
from lib.config import load_project  # noqa: E402

OUT_ROOT = PROJECT_ROOT / "work" / "fixtures"

# 音色来源（可被 --ref 覆盖）：指向已验证的测试项目参考音频
DEFAULT_REFS = {
    "A": "projects/20261018_smoke/05_voices/SPK_01/reference.wav",
    "B": "projects/20261018_duo/05_voices/SPK_02/reference.wav",
    "C": "projects/20261018_duo/05_voices/SPK_01/reference.wav",
}

SCENES: dict[str, dict] = {
    "A_mono": {
        "expected_speakers": 1,
        "voices": ["A"],
        "lines": [
            ("A", "The city was quiet that night. Too quiet, if you ask me."),
            ("A", "I had been following that gray sedan for three blocks."),
            ("A", "Every instinct I had said walk away. But I never listen."),
            ("A", "The rain made the streetlights look like broken glass."),
            ("A", "Somewhere in that building, my answer was waiting."),
            ("A", "I checked my gun, took a breath, and walked in."),
        ],
        "notes": "单人独白，noir 风格",
    },
    "B_duo": {
        "expected_speakers": 2,
        "voices": ["A", "B"],
        "lines": [
            ("A", "You said the package would be here by midnight."),
            ("B", "It will be. Sit down and stop looking out the window."),
            ("A", "I don't sit when I'm waiting for liars."),
            ("B", "Careful. That mouth of yours writes checks."),
            ("A", "I've cashed bigger ones than you."),
            ("B", "Maybe. But not tonight. Tonight you need me."),
            ("A", "Then talk. The clock is running."),
            ("B", "Dock seven, warehouse twelve. Ask for the man with the red scarf."),
        ],
        "notes": "双人对白，轮替清晰",
    },
    "C_trio": {
        "expected_speakers": 3,
        "voices": ["A", "B", "C"],
        "lines": [
            ("A", "Alright team, listen up. We get one shot at this."),
            ("B", "The vault opens at six. Guards change at five forty."),
            ("C", "And the cameras? I can loop them for ninety seconds."),
            ("A", "Ninety seconds is all we need. Nobody improvises."),
            ("B", "What about the alarm on the side door?"),
            ("C", "Handled. I wired it yesterday. It reads all clear."),
            ("A", "Good. We move on my signal, not before."),
            ("B", "And if something goes wrong in there?"),
            ("A", "Nothing goes wrong. That's the plan."),
        ],
        "notes": "三人轮替。⚠️ 已知特性：音色 A/C 同源（同一讲师两段参考），"
                 "diarization 会过拆（实测 4 类）——可作 R1 人工合并工作流的演示素材",
    },
    "D_fast": {
        "expected_speakers": 2,
        "voices": ["A", "B"],
        "lines": [
            ("A", "Move!"),
            ("B", "Which way?"),
            ("A", "Left! Go left!"),
            ("B", "The door's locked!"),
            ("A", "Window. Now."),
            ("B", "Twelve floors up!"),
            ("A", "You got a better idea?"),
            ("B", "No! Jump or fight?"),
            ("A", "Both. Jump, then fight."),
            ("B", "You're insane."),
            ("A", "I'm alive. Jump!"),
            ("B", "Fine. On three."),
        ],
        "notes": "压力测试：超短句急速轮替（每句约 0.5-1.5s + 0.3s 间隙，"
                 "说话人每 1-2 秒切换）——考察 auto 判定与聚类是否把快速对白"
                 "过合并成一人（SOLO_MERGE_SIM 风险场景）",
    },
}


def _refs(overrides: dict[str, str]) -> dict[str, Path]:
    refs: dict[str, Path] = {}
    for k, v in DEFAULT_REFS.items():
        p = Path(overrides.get(k, v))
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        refs[k] = p
    return refs


def reexec_if_needed() -> None:
    """合成需要 IndexTTS（env_tts）；不在该环境则自动转交。"""
    py = envs_mod.env_python("env_tts")
    if Path(sys.executable).resolve() == py.resolve():
        return
    env = dict(os.environ, DUB_REEXEC_FIXTURE="1")
    env.pop("PYTHONPATH", None)
    print(f"[fixture] 转交 env_tts 执行: {py}")
    raise SystemExit(subprocess.call(
        [str(py), str(Path(__file__).resolve()), *sys.argv[1:]], cwd=str(PROJECT_ROOT), env=env,
    ))


def synth_scene(name: str, scene: dict, refs: dict[str, Path], refresh: bool) -> Path:
    out_dir = OUT_ROOT
    out_dir.mkdir(parents=True, exist_ok=True)
    gsv_tools = PROJECT_ROOT / "tools" / "index-tts"
    if str(gsv_tools) not in sys.path:
        sys.path.insert(0, str(gsv_tools))
    from indextts.infer_v2_5 import IndexTTS2  # noqa: E402 - reexec/chdir 之后才 import

    model_dir = PROJECT_ROOT / "models" / "indextts2.5"
    os.environ["HF_HUB_CACHE"] = str(model_dir / "hf_cache")
    engine = IndexTTS2(
        cfg_path=str(model_dir / "config.yaml"),
        model_dir=str(model_dir),
        use_bf16=True, device="cuda", use_qwen_emo=False,
    )
    gap = out_dir / "_gap.wav"
    if not gap.exists():
        subprocess.run(
            [audio.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "0.3", str(gap)],
            check=True,
        )
    parts = []
    for i, (spk, text) in enumerate(scene["lines"], 1):
        out = out_dir / f"{name}_{spk}_{i:02d}.wav"
        if refresh or not out.exists():
            engine.infer(
                spk_audio_prompt=str(refs[spk]), text=text, output_path=str(out),
                lang="en", duration_factor=1.0, verbose=False, text_normalization=True,
            )
            print(f"  {name} {spk} #{i}: ok", flush=True)
        parts.append(out)
    lst = out_dir / f"{name}_concat.txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write(f"file '{p.as_posix()}'\nfile '{gap.as_posix()}'\n")
    dst = out_dir / f"{name}.wav"
    subprocess.run(
        [audio.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(dst)],
        check=True,
    )
    return dst


def write_ground_truth(name: str, scene: dict, refs: dict[str, Path]) -> Path:
    data = {
        "scene": name,
        "expected_speakers": scene["expected_speakers"],
        "voices": {k: str(v) for k, v in refs.items() if k in scene["voices"]},
        "lines": [{"idx": i, "speaker": spk, "text": text}
                  for i, (spk, text) in enumerate(scene["lines"], 1)],
        "notes": scene["notes"],
    }
    dst = OUT_ROOT / f"{name}.expected.json"
    dst.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return dst


def make_projects(names: list[str]) -> None:
    smoke = load_project(PROJECT_ROOT / "projects" / "20261018_smoke")
    for name in names:
        pid = f"20261018_fx_{name}"
        src = OUT_ROOT / f"{name}.wav"
        if not src.exists():
            print(f"[skip] {pid}：素材不存在（先合成）")
            continue
        if (PROJECT_ROOT / "projects" / pid).exists():
            print(f"[skip] {pid}：项目已存在（删除后可重建）")
            continue
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "new_project.py"), pid, str(src)],
            cwd=str(PROJECT_ROOT), check=True,
        )
        path = PROJECT_ROOT / "projects" / pid / "project.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["config"]["single_speaker"] = "auto"
        cfg["config"]["translation"] = smoke["config"]["translation"]
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[fixture] 项目已配置: {pid}（auto + 继承翻译）")


def main() -> None:
    global OUT_ROOT
    ap = argparse.ArgumentParser(description="测试素材生成器（带 ground truth 的英文对白场景）")
    ap.add_argument("--scenes", default="A_mono,B_duo,C_trio", help="逗号分隔的场景名")
    ap.add_argument("--refresh", action="store_true", help="强制重合成（默认已存在则跳过）")
    ap.add_argument("--make-projects", action="store_true", help="合成后自动建好测试项目")
    ap.add_argument("--out", default=str(OUT_ROOT), help="输出目录")
    ap.add_argument("--ref", action="append", default=[], metavar="K=路径",
                    help="覆盖音色参考，如 --ref A=D:\\refs\\a.wav")
    ap.add_argument("--list", action="store_true", help="列出场景与 ground truth")
    args = ap.parse_args()

    OUT_ROOT = Path(args.out)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    if args.list:
        for name, sc in SCENES.items():
            print(f"{name}: 预期 {sc['expected_speakers']} 说话人，{len(sc['lines'])} 句 —— {sc['notes']}")
        return

    names = [s.strip() for s in args.scenes.split(",") if s.strip()]
    bad = [n for n in names if n not in SCENES]
    if bad:
        raise SystemExit(f"未知场景: {bad}（--list 查看）")
    overrides = dict(kv.split("=", 1) for kv in args.ref)
    refs = _refs(overrides)
    missing = [k for k, p in refs.items()
               if any(k in sc["voices"] for sc in SCENES.values()) and not p.exists()]
    if missing:
        raise SystemExit(f"音色参考缺失: {missing}（--ref K=路径 指定，或确认默认项目的 05_voices 存在）")

    reexec_if_needed()
    audio.use_bundled_ffmpeg()
    for name in names:
        print(f"[fixture] 合成 {name}...", flush=True)
        dst = synth_scene(name, SCENES[name], refs, args.refresh)
        gt = write_ground_truth(name, SCENES[name], refs)
        print(f"[fixture] {name} -> {dst}（ground truth: {gt.name}）", flush=True)

    if args.make_projects:
        make_projects(names)


if __name__ == "__main__":
    main()
