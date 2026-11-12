"""M13: one newsletter draft for a planned week, written by Claude in her voice.

Sends her brand voice and the week's angles and captions. No UUIDs or previews.
Drafts are stored by week start date, so a re-run reuses them unless asked for a fresh one.
"""

import sqlite3
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from loupe.brand import BrandProfile
from loupe.claude import MODEL, ClaudeError, Request, StructuredClient
from loupe.hooks import voice_brief
from loupe.week import PlannedWeek

MAX_SUBJECT_CHARACTERS = 60
MAX_PREVIEW_CHARACTERS = 110


class EmailDraft(BaseModel):
    subject: str = Field(description=f"At most {MAX_SUBJECT_CHARACTERS} characters.")
    preview: str = Field(description=f"Inbox preview line, at most {MAX_PREVIEW_CHARACTERS}.")
    body: str = Field(description="Plain text with blank lines between paragraphs.")


SYSTEM = (
    "You write one weekly email for one creator to her subscribers, in her own voice. "
    "Match her past captions in rhythm, vocabulary and punctuation. Plain text, no emoji."
)


def email_request(week: PlannedWeek, brand: BrandProfile) -> Request:
    posts = "\n".join(
        f"- {post.day}, {post.pillar}, {post.format.value}: {post.angle}\n  Caption: {post.caption}"
        for post in week.plan.posts
    )
    prompt = (
        f"{voice_brief(brand)}\n\n"
        f"This week's posts:\n{posts}\n\n"
        "Write the email that goes out Monday. Pull the strongest two or three ideas from "
        "the week into something worth reading on its own, then point readers to the posts. "
        f"End with 'Reply {brand.voice.comment_word}' as the call to action."
    )
    return Request(subject=f"email-{week.start.isoformat()}", system=SYSTEM, prompt=prompt)


def clean_draft(draft: EmailDraft) -> EmailDraft:
    subject, preview, body = draft.subject.strip(), draft.preview.strip(), draft.body.strip()
    if not subject or not body:
        raise ClaudeError("Claude returned an email without a subject or body")
    if len(subject) > MAX_SUBJECT_CHARACTERS:
        raise ClaudeError(f"Claude's subject line is {len(subject)} characters")
    return EmailDraft(subject=subject, preview=preview[:MAX_PREVIEW_CHARACTERS], body=body)


def load_email(connection: sqlite3.Connection, week_start: str) -> EmailDraft | None:
    row = connection.execute(
        "SELECT draft_json FROM emails WHERE week_start = ?", (week_start,)
    ).fetchone()
    return None if row is None else EmailDraft.model_validate_json(row[0])


def save_email(connection: sqlite3.Connection, week_start: str, draft: EmailDraft) -> None:
    connection.execute(
        """
        INSERT INTO emails (week_start, model, draft_json, created_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(week_start) DO UPDATE SET model = excluded.model,
            draft_json = excluded.draft_json, created_at = excluded.created_at
        """,
        (week_start, MODEL, draft.model_dump_json(), datetime.now(UTC).isoformat()),
    )
    connection.commit()


def email_for_week(
    connection: sqlite3.Connection,
    week: PlannedWeek,
    brand: BrandProfile,
    client: StructuredClient,
    fresh: bool = False,
) -> EmailDraft:
    key = week.start.isoformat()
    stored = None if fresh else load_email(connection, key)
    if stored is not None:
        return stored
    draft = clean_draft(client.parse(email_request(week, brand), EmailDraft))
    save_email(connection, key, draft)
    return draft


def email_text(draft: EmailDraft) -> str:
    return f"Subject: {draft.subject}\nPreview: {draft.preview}\n\n{draft.body}\n"
