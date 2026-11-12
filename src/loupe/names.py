"""Validation for names that come from config and end up in Photos or SQL."""

MAX_NAME_LENGTH = 255


def validate_album_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("album name is empty")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"album name is longer than {MAX_NAME_LENGTH} characters")
    if any(not character.isprintable() for character in name):
        raise ValueError(f"album name has a control character: {name!r}")
    return name
