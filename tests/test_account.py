"""Account client tests. A fake transport stands in for the Loupe server."""

import json

import pytest

from loupe.account import (
    AccountClient,
    AccountError,
    MemoryTokenStore,
    Reply,
    Session,
    validate_server_url,
)

SERVER = "https://loupe.example.test"
TOKEN = "synthetic-session-token"  # noqa: S105 - test value


class FakeServer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, dict[str, str]]] = []
        self.replies: dict[tuple[str, str], Reply] = {}

    def __call__(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
        payload = json.loads(body) if body else None
        self.calls.append((method, url, payload, headers))
        return self.replies.get((method, url), Reply(500, b""))

    def reply(self, method: str, path: str, status: int, payload: object) -> None:
        self.replies[(method, SERVER + path)] = Reply(status, json.dumps(payload).encode())


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
def session(server: FakeServer) -> Session:
    return Session(AccountClient(SERVER, server), MemoryTokenStore())


def _error(message: str) -> dict:
    return {"type": "error", "error": {"type": "invalid_request_error", "message": message}}


def test_server_url_must_be_https_or_local() -> None:
    assert validate_server_url(SERVER + "/") == SERVER
    assert validate_server_url("http://localhost:8080") == "http://localhost:8080"
    for bad in ("http://loupe.example.test", "ftp://x", "loupe.example.test", "https://"):
        with pytest.raises(AccountError):
            validate_server_url(bad)


def test_create_account_stores_token(server: FakeServer, session: Session) -> None:
    server.reply("POST", "/v1/accounts", 201, {"token": TOKEN, "email": "a@example.test"})
    assert session.create_account("a@example.test", "long enough password") == "a@example.test"
    assert session.token() == TOKEN
    method, url, payload, headers = server.calls[0]
    assert (method, url) == ("POST", SERVER + "/v1/accounts")
    assert payload == {"email": "a@example.test", "password": "long enough password"}
    assert "Authorization" not in headers


def test_short_password_is_refused_before_sending(server: FakeServer, session: Session) -> None:
    with pytest.raises(AccountError):
        session.create_account("a@example.test", "short")
    assert server.calls == []


def test_sign_in_errors_are_readable(server: FakeServer, session: Session) -> None:
    server.reply("POST", "/v1/sessions", 401, _error("invalid credentials"))
    with pytest.raises(AccountError, match="Wrong email or password"):
        session.sign_in("a@example.test", "long enough password")
    server.reply("POST", "/v1/accounts", 409, _error("taken"))
    with pytest.raises(AccountError, match="already exists"):
        session.create_account("a@example.test", "long enough password")
    server.reply("POST", "/v1/accounts", 400, _error("email: invalid email"))
    with pytest.raises(AccountError, match="invalid email"):
        session.create_account("nope", "long enough password")
    assert session.token() is None


def test_unexpected_reply_is_an_error(server: FakeServer, session: Session) -> None:
    server.reply("POST", "/v1/sessions", 200, {"nothing": True})
    with pytest.raises(AccountError, match="unexpected"):
        session.sign_in("a@example.test", "long enough password")


def test_current_sends_bearer_and_drops_rejected_token(
    server: FakeServer, session: Session
) -> None:
    assert session.current() is None
    server.reply("POST", "/v1/sessions", 200, {"token": TOKEN, "email": "a@example.test"})
    session.sign_in("a@example.test", "long enough password")
    usage = {"email": "a@example.test", "month": "2026-11", "used": 10, "cap": 100}
    server.reply("GET", "/v1/me", 200, usage)
    assert session.current().used == 10
    assert server.calls[-1][3]["Authorization"] == f"Bearer {TOKEN}"
    server.reply("GET", "/v1/me", 401, _error("expired"))
    assert session.current() is None
    assert session.token() is None


def test_sign_out_clears_token_even_if_server_fails(server: FakeServer, session: Session) -> None:
    server.reply("POST", "/v1/sessions", 200, {"token": TOKEN, "email": "a@example.test"})
    session.sign_in("a@example.test", "long enough password")
    session.sign_out()
    assert session.token() is None
    assert server.calls[-1][:2] == ("DELETE", SERVER + "/v1/sessions")


def test_proxy_client_uses_session_token_not_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("anthropic")
    from loupe.claude import AnthropicClient, ProxyAuth

    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-env-key")
    client = AnthropicClient(ProxyAuth(SERVER, TOKEN))._client
    assert str(client.base_url).rstrip("/") == SERVER
    assert client.api_key is None
    assert client.auth_headers == {"Authorization": f"Bearer {TOKEN}"}
