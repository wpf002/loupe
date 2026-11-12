from pathlib import Path

import numpy as np
import pytest

from loupe.audio import AudioError, decode_audio
from loupe.index import connect, upsert_item
from loupe.models import MediaType
from loupe.transcribe import (
    MIN_TALKING_WORDS,
    TranscribeSkip,
    Transcript,
    Word,
    load_transcript,
    pending_clips,
    talking_clips,
    transcribe_clips,
    words_from_whisper,
)
from tests.fixtures import make_item, synthetic_uuid

MODEL = "fake-whisper"


class FakeTranscriber:
    model_id = MODEL

    def __init__(self, word_count: int) -> None:
        self.word_count = word_count

    def transcribe(self, audio: np.ndarray) -> Transcript:
        words = tuple(Word(f" w{n}", n * 0.5, n * 0.5 + 0.4) for n in range(self.word_count))
        return Transcript(language="en", words=words)


def _video(number: int, **overrides: object):
    values = {
        "media_type": MediaType.VIDEO,
        "duration_seconds": 30.0,
        "original_is_local": True,
        "albums": (),
    }
    values.update(overrides)
    return make_item(synthetic_uuid(number), **values)


@pytest.fixture
def connection(tmp_path: Path):
    connection = connect(tmp_path / "index.sqlite")
    for item in (
        _video(1),
        _video(2),
        _video(3, original_is_local=False),
        _video(4, duration_seconds=1.0),
        _video(5, is_screen_recording=True),
        make_item(synthetic_uuid(6)),
    ):
        upsert_item(connection, item)
    connection.commit()
    yield connection
    connection.close()


def test_pending_clips_only_local_videos_in_window(connection) -> None:
    assert pending_clips(connection, None) == [synthetic_uuid(1), synthetic_uuid(2)]
    assert pending_clips(connection, 1) == [synthetic_uuid(1)]


def test_transcribe_saves_words_and_resumes(connection) -> None:
    pending = pending_clips(connection, None)
    paths = {synthetic_uuid(1): Path("/synthetic/one.mov")}
    result = transcribe_clips(
        connection, pending, paths, FakeTranscriber(MIN_TALKING_WORDS), loader=lambda _: np.zeros(1)
    )
    assert result.transcribed == 1
    assert result.talking == 1
    assert result.skipped == {TranscribeSkip.NOT_LOCAL: 1}
    assert pending_clips(connection, None) == []
    transcript = load_transcript(connection, synthetic_uuid(1))
    assert transcript is not None
    assert transcript.words[1] == Word(" w1", 0.5, 0.9)
    assert talking_clips(connection) == [synthetic_uuid(1)]


def test_short_transcript_is_not_talking(connection) -> None:
    def failing_loader(_: Path) -> np.ndarray:
        raise AudioError("no audio track")

    paths = {synthetic_uuid(1): Path("/a.mov"), synthetic_uuid(2): Path("/b.mov")}
    transcribe_clips(
        connection, [synthetic_uuid(1)], paths, FakeTranscriber(2), loader=lambda _: np.zeros(1)
    )
    result = transcribe_clips(
        connection, [synthetic_uuid(2)], paths, FakeTranscriber(2), loader=failing_loader
    )
    assert result.skipped == {TranscribeSkip.DECODE_FAILED: 1}
    assert talking_clips(connection) == []


def test_words_from_whisper_flattens_segments() -> None:
    result = {"segments": [{"words": [{"word": " Hi", "start": 0.0, "end": 0.3}]}, {}]}
    assert words_from_whisper(result) == (Word(" Hi", 0.0, 0.3),)


def test_decode_audio_missing_file(tmp_path: Path) -> None:
    with pytest.raises(AudioError):
        decode_audio(tmp_path / "absent.mov")


def test_decode_audio_reads_generated_tone(tmp_path: Path) -> None:
    import subprocess

    from loupe.audio import ffmpeg_exe

    clip = tmp_path / "tone.m4a"
    subprocess.run(  # noqa: S603
        [ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         str(clip)],
        check=True,
    )  # fmt: skip
    samples = decode_audio(clip)
    assert 15_000 < samples.size < 17_500
    assert float(np.abs(samples).max()) > 0.05
