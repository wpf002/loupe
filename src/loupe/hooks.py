"""M10: six hook openings for one take, written in her voice by Claude.

Sends the transcript and her brand voice. Hooks are stored per UUID, so a re-run
reuses them unless asked for fresh ones.
"""

import json
import sqlite3
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from loupe.brand import BrandProfile
from loupe.claude import MODEL, ClaudeError, Request, StructuredClient
from loupe.transcribe import Transcript

HOOK_COUNT = 6
MAX_HOOK_WORDS = 14


class HookSet(BaseModel):
    hooks: list[str] = Field(description=f"Exactly {HOOK_COUNT} distinct opening lines.")


def voice_brief(brand: BrandProfile) -> str:
    samples = "\n".join(f"- {sample}" for sample in brand.voice.samples)
    avoid = ", ".join(brand.voice.avoid) or "nothing listed"
    return (
        f"Creator: {brand.name}\n"
        f"Voice traits: {', '.join(brand.voice.traits)}\n"
        f"Never use: {avoid}\n"
        f"Her past captions:\n{samples}"
    )


SYSTEM = (
    "You write short-form video hooks for one creator, in her own voice. "
    "Match her past captions in rhythm, vocabulary and punctuation. No emoji, no hashtags."
)


def hook_request(uuid: str, brand: BrandProfile, transcript: Transcript) -> Request:
    prompt = (
        f"{voice_brief(brand)}\n\n"
        f"Transcript of the take:\n{transcript.text}\n\n"
        f"Write {HOOK_COUNT} different hook openings for this take. Each is one line, "
        f"at most {MAX_HOOK_WORDS} words, true to what she says in the take. Vary the "
        "angle: a question, a bold claim, a number, a mistake, a before/after, a secret."
    )
    return Request(subject=uuid, system=SYSTEM, prompt=prompt)


def clean_hooks(hook_set: HookSet) -> tuple[str, ...]:
    hooks = tuple(dict.fromkeys(hook.strip() for hook in hook_set.hooks if hook.strip()))
    if len(hooks) < HOOK_COUNT:
        raise ClaudeError(f"Claude returned {len(hooks)} usable hooks, expected {HOOK_COUNT}")
    return hooks[:HOOK_COUNT]


def load_hooks(connection: sqlite3.Connection, uuid: str) -> tuple[str, ...] | None:
    row = connection.execute("SELECT hooks_json FROM hooks WHERE uuid = ?", (uuid,)).fetchone()
    return None if row is None else tuple(json.loads(row[0]))


def save_hooks(connection: sqlite3.Connection, uuid: str, hooks: tuple[str, ...]) -> None:
    connection.execute(
        """
        INSERT INTO hooks (uuid, model, hooks_json, created_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(uuid) DO UPDATE SET model = excluded.model,
            hooks_json = excluded.hooks_json, created_at = excluded.created_at
        """,
        (uuid, MODEL, json.dumps(list(hooks)), datetime.now(UTC).isoformat()),
    )
    connection.commit()


def hooks_for_take(
    connection: sqlite3.Connection,
    uuid: str,
    brand: BrandProfile,
    transcript: Transcript,
    client: StructuredClient,
    fresh: bool = False,
) -> tuple[str, ...]:
    stored = None if fresh else load_hooks(connection, uuid)
    if stored is not None:
        return stored
    hooks = clean_hooks(client.parse(hook_request(uuid, brand, transcript), HookSet))
    save_hooks(connection, uuid, hooks)
    return hooks
