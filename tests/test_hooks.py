from pathlib import Path

import pytest

from loupe.claude import ClaudeError, Request, image_block, user_content
from loupe.hooks import HOOK_COUNT, HookSet, clean_hooks, hook_request, hooks_for_take, load_hooks
from loupe.index import connect, upsert_item
from loupe.transcribe import Transcript, Word
from tests.fixtures import make_brand, make_item

TAKE = Transcript(language="en", words=(Word(" Pack", 0.0, 0.4), Word(" light.", 0.5, 0.9)))


class FakeClient:
    def __init__(self, hooks: list[str]) -> None:
        self.hooks = hooks
        self.requests: list[Request] = []

    def parse(self, request: Request, output_type: type[HookSet]) -> HookSet:
        self.requests.append(request)
        return output_type(hooks=self.hooks)


SIX = [f"Hook {number}" for number in range(1, 7)]


@pytest.fixture
def connection(tmp_path: Path):
    connection = connect(tmp_path / "index.sqlite")
    upsert_item(connection, make_item())
    connection.commit()
    yield connection
    connection.close()


def test_request_carries_voice_and_transcript(tmp_path: Path) -> None:
    request = hook_request("SYNTHETIC", make_brand(tmp_path), TAKE)
    assert "Pack light." in request.prompt
    assert "Second caption." in request.prompt
    assert "hustle" in request.prompt
    assert request.images == ()


def test_clean_hooks_dedupes_and_requires_six() -> None:
    assert clean_hooks(HookSet(hooks=[*SIX, "Hook 7"])) == tuple(SIX)
    with pytest.raises(ClaudeError, match="5 usable"):
        clean_hooks(HookSet(hooks=[*SIX[:5], " Hook 1 ", ""]))


def test_hooks_are_stored_and_reused(connection, tmp_path: Path) -> None:
    uuid = make_item().uuid
    client = FakeClient(SIX)
    brand = make_brand(tmp_path)
    assert hooks_for_take(connection, uuid, brand, TAKE, client) == tuple(SIX)
    assert hooks_for_take(connection, uuid, brand, TAKE, client) == tuple(SIX)
    assert len(client.requests) == 1
    client.hooks = [f"New {number}" for number in range(HOOK_COUNT)]
    hooks_for_take(connection, uuid, brand, TAKE, client, fresh=True)
    assert load_hooks(connection, uuid) == tuple(client.hooks)


def test_user_content_puts_images_before_text(tmp_path: Path) -> None:
    preview = tmp_path / "preview.jpeg"
    preview.write_bytes(b"\xff\xd8synthetic")
    blocks = user_content(Request("SYNTHETIC", "system", "prompt", images=(preview,)))
    assert [block["type"] for block in blocks] == ["image", "text"]
    with pytest.raises(ClaudeError):
        image_block(tmp_path / "clip.mov")
