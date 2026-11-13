import http.client
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
import pytest

from loupe.account import Reply
from loupe.embed import vector_path
from loupe.encoder import MODEL_ID
from loupe.hosted import account_key
from loupe.index import connect
from loupe.sync import SIGNED_OUT, Syncer, SyncError, WebClient
from loupe.vectors import VectorStore
from tests.test_hosted import PUBLIC_HOST, _call, _cookie, _sign_in, hosted
from tests.test_sorting import CONFIG, _library

__all__ = ["hosted"]
DIM = 4


def _with_vectors(index_path: Path, data_dir: Path) -> int:
    connection = connect(index_path)
    try:
        uuids = [row[0] for row in connection.execute("SELECT uuid FROM items ORDER BY uuid")]
        rows = np.random.default_rng(0).random((len(uuids), DIM)).astype(np.float16)
        VectorStore(vector_path(data_dir, MODEL_ID), DIM).write(0, rows)
        now = datetime.now(UTC).isoformat()
        with connection:
            connection.executemany(
                "INSERT INTO embeddings (model, uuid, vector_row, embedded_at) VALUES (?, ?, ?, ?)",
                [(MODEL_ID, uuid, row, now) for row, uuid in enumerate(uuids)],
            )
        return len(uuids)
    finally:
        connection.close()


class LocalTransport:
    """Sends to the test server with its public Host header, and counts preview uploads."""

    def __init__(self) -> None:
        self.previews_sent = 0

    def __call__(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str], timeout: float
    ) -> Reply:
        parts = urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        if method == "PUT" and parts.path.startswith("/sync/v1/previews/"):
            self.previews_sent += 1
        connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers={**headers, "Host": PUBLIC_HOST})
            response = connection.getresponse()
            return Reply(response.status, response.read())
        finally:
            connection.close()


def _mac(tmp_path: Path) -> tuple[Path, Path, int]:
    mac = tmp_path / "mac"
    mac.mkdir()
    index_path = _library(mac)
    data_dir = mac / "data"
    data_dir.mkdir()
    return index_path, data_dir, _with_vectors(index_path, data_dir)


def test_sync_sends_the_library_to_the_web(hosted, tmp_path: Path) -> None:
    port, accounts, root = hosted
    _, headers, _ = _sign_in(port)
    token = next(iter(accounts.tokens))
    index_path, data_dir, items = _mac(tmp_path)
    transport = LocalTransport()
    syncer = Syncer(
        f"http://localhost:{port}",
        lambda: token,
        data_dir,
        index_path,
        lambda: None,
        CONFIG,
        transport=transport,
    )
    syncer.run_steps(WebClient(syncer.web_url, token, transport))

    tenant = root / "accounts" / account_key("owner@example.com")
    assert len(list((tenant / "data" / "previews").glob("*.jpg"))) == items
    connection = connect(tenant / "data" / "loupe.sqlite")
    try:
        assert connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == items
        assert connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == items
        assert connection.execute("SELECT COUNT(*) FROM item_persons").fetchone()[0] == 0
    finally:
        connection.close()
    status, _, body = _call(port, "GET", "/api/library", headers={"Cookie": _cookie(headers)})
    assert status == 200
    assert json.loads(body)["items"] == items

    transport.previews_sent = 0
    syncer.run_steps(WebClient(syncer.web_url, token, transport))
    assert transport.previews_sent == 0


def test_start_needs_a_session(tmp_path: Path) -> None:
    syncer = Syncer(
        "http://localhost:1",
        lambda: None,
        tmp_path,
        tmp_path / "index.sqlite",
        lambda: None,
        CONFIG,
    )
    with pytest.raises(SyncError):
        syncer.start()


def test_rejected_token_reads_as_signed_out() -> None:
    def refuse(*_: object) -> Reply:
        return Reply(401, b'{"error": "Sign in again"}')

    client = WebClient("http://localhost:1", "stale", refuse)
    with pytest.raises(SyncError, match=SIGNED_OUT):
        client.call("GET", "/sync/v1/previews/missing")


def test_server_errors_carry_their_message() -> None:
    def refuse(*_: object) -> Reply:
        return Reply(400, b'{"error": "Previews must be JPEG"}')

    client = WebClient("http://localhost:1", "token", refuse)
    with pytest.raises(SyncError, match="Previews must be JPEG"):
        client.call("PUT", "/sync/v1/previews/x", b"")
