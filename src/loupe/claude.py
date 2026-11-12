"""Claude API access for the generation stages (M10 on).

Reads ANTHROPIC_API_KEY from the environment (load_settings loads .env). Sends only
her text, transcripts and downsized previews. Logs counts and UUIDs, never content.
"""

import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16_000
REQUEST_TIMEOUT_SECONDS = 300.0
MAX_RETRIES = 3
PREVIEW_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}

logger = logging.getLogger("loupe.claude")
Output = TypeVar("Output", bound=BaseModel)


class ClaudeError(Exception):
    """Raised when a Claude call fails or is refused. The message never holds content."""


@dataclass(frozen=True)
class Request:
    """One structured call. `subject` is a UUID or stage name, used only for logs."""

    subject: str
    system: str
    prompt: str
    images: tuple[Path, ...] = ()


class StructuredClient(Protocol):
    def parse(self, request: Request, output_type: type[Output]) -> Output: ...


def image_block(path: Path) -> dict[str, object]:
    media_type = PREVIEW_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise ClaudeError(f"preview type {path.suffix} is not sent")
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def user_content(request: Request) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = [image_block(path) for path in request.images]
    blocks.append({"type": "text", "text": request.prompt})
    return blocks


class AnthropicClient:
    """Claude Opus 5.5 with structured output and server-side refusal fallbacks."""

    def __init__(self) -> None:
        import anthropic

        self._anthropic = anthropic
        try:
            self._client = anthropic.Anthropic(
                timeout=REQUEST_TIMEOUT_SECONDS, max_retries=MAX_RETRIES
            )
        except anthropic.AnthropicError as error:
            raise ClaudeError("ANTHROPIC_API_KEY is missing. Add it to .env.") from error

    def parse(self, request: Request, output_type: type[Output]) -> Output:
        errors = self._anthropic
        try:
            response = self._client.beta.messages.parse(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                thinking={"type": "adaptive"},
                system=request.system,
                messages=[{"role": "user", "content": user_content(request)}],
                output_format=output_type,
            )
        except errors.AuthenticationError as error:
            raise ClaudeError("Claude rejected the API key in .env") from error
        except errors.RateLimitError as error:
            raise ClaudeError("Claude rate limit hit; try again in a minute") from error
        except errors.APIStatusError as error:
            raise ClaudeError(f"Claude returned HTTP {error.status_code}") from error
        except errors.APIConnectionError as error:
            raise ClaudeError("could not reach the Claude API") from error
        logger.info(
            "claude subject=%s images=%d input_tokens=%d output_tokens=%d stop=%s",
            request.subject,
            len(request.images),
            response.usage.input_tokens,
            response.usage.output_tokens,
            response.stop_reason,
        )
        if response.stop_reason == "refusal":
            raise ClaudeError(f"Claude declined the request for {request.subject}")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise ClaudeError(f"Claude returned no usable output for {request.subject}")
        return response.parsed_output
