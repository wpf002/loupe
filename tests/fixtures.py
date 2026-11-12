"""Synthetic items. No real photo, name or UUID appears here."""

from dataclasses import replace
from datetime import UTC, datetime

from loupe.models import LibraryItem, MediaType

UUID_A = "00000000-0000-4000-8000-00000000000A"
UUID_B = "00000000-0000-4000-8000-00000000000B"
UUID_C = "00000000-0000-4000-8000-00000000000C"


def make_item(uuid: str = UUID_A, **overrides: object) -> LibraryItem:
    base = LibraryItem(
        uuid=uuid,
        taken_at=datetime(2024, 5, 10, 12, 0, tzinfo=UTC),
        media_type=MediaType.PHOTO,
        duration_seconds=None,
        apple_score=0.5,
        preview_path="/synthetic/preview.jpeg",
        albums=("CC Travel",),
        labels=("Beach",),
        persons=("Test Person",),
        is_hidden=False,
        is_screenshot=False,
        is_screen_recording=False,
        is_burst=False,
        original_is_local=False,
    )
    return replace(base, **overrides)


def synthetic_uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012X}"


def seed_vectors(connection: object, store: object, model_id: str, vectors: dict) -> None:
    """Write vectors for already-scanned UUIDs, as `loupe embed` would."""
    for row, (uuid, vector) in enumerate(vectors.items()):
        store.write(row, vector[None, :])  # type: ignore[attr-defined]
        connection.execute(  # type: ignore[attr-defined]
            "INSERT INTO embeddings (model, uuid, vector_row, embedded_at) VALUES (?, ?, ?, ?)",
            (model_id, uuid, row, "2024-01-01T00:00:00+00:00"),
        )
    connection.commit()  # type: ignore[attr-defined]
