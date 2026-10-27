"""中间产物的数据模型与读写（编辑层优先）。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


def load_json(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_edited(project_dir, rel_path: str) -> Path:
    """编辑层优先：存在 X.edited.json 就用它，否则用 X.json。"""
    p = Path(project_dir) / rel_path
    edited = p.with_name(f"{p.stem}.edited{p.suffix}")
    return edited if edited.exists() else p


@dataclass
class Word:
    w: str
    start: float
    end: float


@dataclass
class Segment:
    id: str
    speaker: str
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    confidence: float = 1.0
    flags: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"] = round(self.start, 3)
        d["end"] = round(self.end, 3)
        d["confidence"] = round(self.confidence, 4)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        return cls(
            id=d["id"],
            speaker=d.get("speaker", ""),
            start=float(d["start"]),
            end=float(d["end"]),
            text=d.get("text", ""),
            words=[Word(**w) for w in d.get("words", [])],
            confidence=float(d.get("confidence", 1.0)),
            flags=list(d.get("flags", [])),
        )


@dataclass
class Transcript:
    project_id: str
    source_lang: str
    audio: str
    segments: list[Segment] = field(default_factory=list)

    @property
    def speakers(self) -> list[str]:
        seen: list[str] = []
        for seg in self.segments:
            if seg.speaker not in seen:
                seen.append(seg.speaker)
        return sorted(seen)

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "source_lang": self.source_lang,
            "audio": self.audio,
            "speakers": self.speakers,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Transcript":
        return cls(
            project_id=d.get("project_id", ""),
            source_lang=d.get("source_lang", ""),
            audio=d.get("audio", ""),
            segments=[Segment.from_dict(s) for s in d.get("segments", [])],
        )


@dataclass
class TranslationItem:
    id: str
    speaker: str
    source: str
    target: str
    duration_budget: float
    target_est_duration: float = 0.0
    strategy: str = "native"
    notes: str = ""


@dataclass
class Translation:
    segments: list[TranslationItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"segments": [asdict(s) for s in self.segments]}

    @classmethod
    def from_dict(cls, d: dict) -> "Translation":
        return cls(segments=[TranslationItem(**s) for s in d.get("segments", [])])


def load_transcript(project_dir, rel_path: str = "03_asr/segments.json") -> Transcript:
    return Transcript.from_dict(load_json(resolve_edited(project_dir, rel_path)))


def save_transcript(project_dir, transcript: Transcript, rel_path: str = "03_asr/segments.json") -> str:
    save_json(Path(project_dir) / rel_path, transcript.to_dict())
    return rel_path


def load_translation(project_dir, rel_path: str = "04_translate/translation.json") -> Translation:
    return Translation.from_dict(load_json(resolve_edited(project_dir, rel_path)))


def save_translation(project_dir, translation: Translation, rel_path: str = "04_translate/translation.json") -> str:
    save_json(Path(project_dir) / rel_path, translation.to_dict())
    return rel_path
