"""M8: local speech-to-text with word timestamps for videos whose originals are on disk.

Transcript text is her content. It stays in the local index and is never logged.
"""

import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from loupe.audio import AudioError, decode_audio
from loupe.index import eligible_uuids

WHISPER_REPO = "mlx-community/whisper-large-v3-turbo"
MIN_CLIP_SECONDS = 3.0
MAX_CLIP_SECONDS = 600.0
MIN_TALKING_WORDS = 8


class TranscribeSkip(StrEnum):
    NOT_LOCAL = "original not local"
    DECODE_FAILED = "audio decode failed"


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Transcript:
    language: str | None
    words: tuple[Word, ...]

    @property
    def text(self) -> str:
        return "".join(word.text for word in self.words).strip()

    @property
    def is_talking(self) -> bool:
        return len(self.words) >= MIN_TALKING_WORDS


class Transcriber(Protocol):
    model_id: str

    def transcribe(self, audio: np.ndarray) -> Transcript: ...


@dataclass(frozen=True)
class TranscribeResult:
    transcribed: int
    talking: int
    skipped: dict[str, int]


def words_from_whisper(result: dict[str, Any]) -> tuple[Word, ...]:
    """Flatten mlx-whisper segments into words. Words keep their leading space."""
    return tuple(
        Word(text=str(word["word"]), start=float(word["start"]), end=float(word["end"]))
        for segment in result.get("segments", [])
        for word in segment.get("words", [])
    )


class MlxTranscriber:
    """Whisper on Apple silicon through mlx-whisper (checked against 0.4.3)."""

    def __init__(self, repo: str = WHISPER_REPO) -> None:
        import mlx_whisper

        self._transcribe = mlx_whisper.transcribe
        self.model_id = repo

    def transcribe(self, audio: np.ndarray) -> Transcript:
        result = self._transcribe(
            audio, path_or_hf_repo=self.model_id, word_timestamps=True, verbose=None
        )
        return Transcript(language=result.get("language"), words=words_from_whisper(result))


def pending_clips(connection: sqlite3.Connection, limit: int | None) -> list[str]:
    """Eligible local videos in the length window, not yet transcribed or skipped."""
    rows = connection.execute(
        """
        SELECT uuid FROM items
        WHERE excluded_reason IS NULL AND media_type = 'video'
          AND is_screen_recording = 0 AND original_is_local = 1
          AND duration_seconds BETWEEN ? AND ?
          AND uuid NOT IN (SELECT uuid FROM transcripts)
          AND uuid NOT IN (SELECT uuid FROM transcribe_skips)
        """,
        (MIN_CLIP_SECONDS, MAX_CLIP_SECONDS),
    ).fetchall()
    candidates = {row[0] for row in rows}
    pending = [uuid for uuid in eligible_uuids(connection) if uuid in candidates]
    return pending if limit is None else pending[:limit]


def _save(connection: sqlite3.Connection, uuid: str, model_id: str, transcript: Transcript) -> None:
    connection.execute(
        """
        INSERT INTO transcripts
            (uuid, model, language, text, words_json, word_count, is_talking, transcribed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uuid) DO UPDATE SET model = excluded.model, language = excluded.language,
            text = excluded.text, words_json = excluded.words_json,
            word_count = excluded.word_count, is_talking = excluded.is_talking,
            transcribed_at = excluded.transcribed_at
        """,
        (
            uuid,
            model_id,
            transcript.language,
            transcript.text,
            json.dumps([asdict(word) for word in transcript.words]),
            len(transcript.words),
            int(transcript.is_talking),
            datetime.now(UTC).isoformat(),
        ),
    )


def _skip(connection: sqlite3.Connection, uuid: str, reason: TranscribeSkip) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO transcribe_skips (uuid, reason) VALUES (?, ?)", (uuid, reason)
    )


def transcribe_clips(
    connection: sqlite3.Connection,
    uuids: Iterable[str],
    original_paths: dict[str, Path],
    transcriber: Transcriber,
    loader: Callable[[Path], np.ndarray] = decode_audio,
    on_item: Callable[[], None] | None = None,
) -> TranscribeResult:
    transcribed = talking = 0
    skipped: dict[str, int] = {}
    for uuid in uuids:
        path = original_paths.get(uuid)
        reason: TranscribeSkip | None = None
        if path is None:
            reason = TranscribeSkip.NOT_LOCAL
        else:
            try:
                transcript = transcriber.transcribe(loader(path))
            except AudioError:
                reason = TranscribeSkip.DECODE_FAILED
        if reason is None:
            _save(connection, uuid, transcriber.model_id, transcript)
            transcribed += 1
            talking += int(transcript.is_talking)
        else:
            _skip(connection, uuid, reason)
            skipped[reason] = skipped.get(reason, 0) + 1
        connection.commit()
        if on_item is not None:
            on_item()
    return TranscribeResult(transcribed=transcribed, talking=talking, skipped=skipped)


def load_transcript(connection: sqlite3.Connection, uuid: str) -> Transcript | None:
    row = connection.execute(
        "SELECT language, words_json FROM transcripts WHERE uuid = ?", (uuid,)
    ).fetchone()
    if row is None:
        return None
    words = tuple(Word(**word) for word in json.loads(row[1]))
    return Transcript(language=row[0], words=words)


def talking_clips(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        """
        SELECT transcripts.uuid FROM transcripts JOIN items ON items.uuid = transcripts.uuid
        WHERE transcripts.is_talking = 1 AND items.excluded_reason IS NULL
        ORDER BY items.taken_at DESC
        """
    ).fetchall()
    return [row[0] for row in rows]
