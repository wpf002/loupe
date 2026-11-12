"""Audio decode through the ffmpeg binary that imageio-ffmpeg ships. No Homebrew needed."""

import subprocess  # noqa: S404 - runs only the bundled ffmpeg with a fixed argument list
from pathlib import Path

import imageio_ffmpeg
import numpy as np

SAMPLE_RATE = 16_000
DECODE_TIMEOUT_SECONDS = 600
PCM_SCALE = 32768.0


class AudioError(Exception):
    """Raised when ffmpeg cannot decode a clip."""


def ffmpeg_exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def decode_audio(path: Path, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Mono float32 samples in [-1, 1]. Reads the file; never writes next to it."""
    if not path.is_file():
        raise AudioError("original not on disk")
    command = [
        ffmpeg_exe(), "-nostdin", "-v", "error", "-i", str(path),
        "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-",
    ]  # fmt: skip
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command, capture_output=True, timeout=DECODE_TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired as error:
        raise AudioError("ffmpeg timed out") from error
    if completed.returncode != 0:
        raise AudioError(f"ffmpeg exited with {completed.returncode}")
    samples = np.frombuffer(completed.stdout, dtype=np.int16)
    if samples.size == 0:
        raise AudioError("no audio track")
    return samples.astype(np.float32) / PCM_SCALE
