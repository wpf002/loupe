import http.client
import json
import threading
from collections.abc import Iterator
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from loupe.account import AccountError, SignedIn, Usage
from loupe.hosted import (
    COOKIE_NAME,
    SIGN_IN_ATTEMPTS,
    HostedConfig,
    SessionGate,
    SharedTextEncoder,
    Tenants,
    account_key,
    build_service,
    handler_for_service,
)
from tests.test_sorting import CONFIG

PUBLIC_HOST = "loupe-web.test"
ORIGIN = f"https://{PUBLIC_HOST}"
PASSWORD = "synthetic-password"  # noqa: S105 - test value


class FakeAccounts:
    """Stands in for the Loupe server. Tokens map to account emails."""

    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}
        self.usage_calls = 0
        self.signed_out: list[str] = []

    def sign_in(self, email: str, password: str) -> SignedIn:
        if password != PASSWORD:
            raise AccountError("Wrong email or password", 401)
        token = f"token-{len(self.tokens)}"
        self.tokens[token] = email
        return SignedIn(token=token, email=email)

    def sign_out(self, token: str) -> None:
        self.signed_out.append(token)
        self.tokens.pop(token, None)

    def usage(self, token: str) -> Usage:
        self.usage_calls += 1
        email = self.tokens.get(token)
        if email is None:
            raise AccountError("Sign in again", 401)
        return Usage(email=email, month="2026-11", used=0, cap=100)


@pytest.fixture
def hosted(tmp_path: Path) -> Iterator[tuple[int, FakeAccounts, Path]]:
    accounts = FakeAccounts()
    config = HostedConfig(
        root=tmp_path, server_url="https://server.test", public_host=PUBLIC_HOST, port=8080
    )
    service = build_service(config, SharedTextEncoder(lambda: None), accounts)  # type: ignore[arg-type]
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for_service(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], accounts, tmp_path
    finally:
        server.shutdown()
        server.server_close()


def _call(
    port: int,
    method: str,
    path: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(
            method, path, body=body, headers={"Host": PUBLIC_HOST, **(headers or {})}
        )
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _sign_in(port: int, email: str = "owner@example.com", password: str = PASSWORD):
    body = json.dumps({"email": email, "password": password}).encode()
    headers = {"Content-Type": "application/json", "Origin": ORIGIN}
    return _call(port, "POST", "/auth/sign-in", body, headers)


def _cookie(headers: dict[str, str]) -> str:
    value = headers["Set-Cookie"].split(";")[0]
    assert value.startswith(f"{COOKIE_NAME}=")
    return value


def test_root_without_session_redirects_to_login(hosted) -> None:
    port, _, _ = hosted
    status, headers, _ = _call(port, "GET", "/")
    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/login"
    status, _, body = _call(port, "GET", "/login")
    assert status == HTTPStatus.OK
    assert b"Sign In" in body


def test_api_without_session_is_401(hosted) -> None:
    port, _, _ = hosted
    status, _, _ = _call(port, "GET", "/api/setup")
    assert status == HTTPStatus.UNAUTHORIZED


def test_wrong_host_is_refused(hosted) -> None:
    port, _, _ = hosted
    status, _, _ = _call(port, "GET", "/login", headers={"Host": "evil.test"})
    assert status == HTTPStatus.FORBIDDEN


def test_sign_in_sets_a_locked_down_cookie(hosted) -> None:
    port, _, _ = hosted
    status, headers, _ = _sign_in(port)
    assert status == HTTPStatus.OK
    cookie = headers["Set-Cookie"]
    for attribute in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
        assert attribute in cookie
    assert "Strict-Transport-Security" in headers


def test_sign_in_needs_the_site_origin(hosted) -> None:
    port, _, _ = hosted
    body = json.dumps({"email": "owner@example.com", "password": PASSWORD}).encode()
    headers = {"Content-Type": "application/json", "Origin": "https://evil.test"}
    status, _, _ = _call(port, "POST", "/auth/sign-in", body, headers)
    assert status == HTTPStatus.FORBIDDEN


def test_wrong_password_is_401(hosted) -> None:
    port, _, _ = hosted
    status, headers, _ = _sign_in(port, password="wrong")  # noqa: S106 - test value
    assert status == HTTPStatus.UNAUTHORIZED
    assert "Set-Cookie" not in headers


def test_sign_in_is_rate_limited_per_address(hosted) -> None:
    port, _, _ = hosted
    for _ in range(SIGN_IN_ATTEMPTS):
        _sign_in(port, password="wrong")  # noqa: S106 - test value
    status, _, _ = _sign_in(port)
    assert status == HTTPStatus.TOO_MANY_REQUESTS


def test_signed_in_browser_gets_its_own_account(hosted) -> None:
    port, _, root = hosted
    _, headers, _ = _sign_in(port)
    status, _, body = _call(port, "GET", "/api/setup", headers={"Cookie": _cookie(headers)})
    assert status == HTTPStatus.OK
    setup = json.loads(body)
    assert setup["mode"] == "hosted"
    assert setup["account"]["email"] == "owner@example.com"
    status, _, page = _call(port, "GET", "/", headers={"Cookie": _cookie(headers)})
    assert status == HTTPStatus.OK
    assert b"loupe-token" in page
    assert (root / "accounts" / account_key("owner@example.com")).is_dir()


def test_expired_cookie_on_root_clears_it(hosted) -> None:
    port, _, _ = hosted
    status, headers, _ = _call(port, "GET", "/", headers={"Cookie": f"{COOKIE_NAME}=stale"})
    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/login"
    assert "Max-Age=0" in headers["Set-Cookie"]


def test_app_posts_still_need_the_page_token(hosted) -> None:
    port, _, _ = hosted
    _, headers, _ = _sign_in(port)
    request = {"Cookie": _cookie(headers), "Content-Type": "application/json", "Origin": ORIGIN}
    status, _, _ = _call(port, "POST", "/api/library/sort", b"{}", request)
    assert status == HTTPStatus.FORBIDDEN


def test_sorting_is_refused_on_the_web(hosted) -> None:
    port, _, _ = hosted
    _, headers, _ = _sign_in(port)
    cookie = _cookie(headers)
    _, _, page = _call(port, "GET", "/", headers={"Cookie": cookie})
    token = page.split(b'name="loupe-token" content="')[1].split(b'"')[0].decode()
    request = {
        "Cookie": cookie,
        "Content-Type": "application/json",
        "Origin": ORIGIN,
        "X-Loupe-Token": token,
    }
    status, _, _ = _call(port, "POST", "/api/library/sort", b"{}", request)
    assert status == HTTPStatus.NOT_FOUND


def test_sign_out_revokes_and_clears(hosted) -> None:
    port, accounts, _ = hosted
    _, headers, _ = _sign_in(port)
    cookie = _cookie(headers)
    request = {"Cookie": cookie, "Content-Type": "application/json", "Origin": ORIGIN}
    status, reply, _ = _call(port, "POST", "/auth/sign-out", b"{}", request)
    assert status == HTTPStatus.OK
    assert "Max-Age=0" in reply["Set-Cookie"]
    assert accounts.signed_out == [cookie.split("=", 1)[1]]
    status, _, _ = _call(port, "GET", "/api/setup", headers={"Cookie": cookie})
    assert status == HTTPStatus.UNAUTHORIZED


def test_sync_needs_a_bearer_token(hosted) -> None:
    port, _, _ = hosted
    status, _, _ = _call(port, "GET", "/sync/v1/previews/missing")
    assert status == HTTPStatus.UNAUTHORIZED
    _, headers, _ = _sign_in(port)
    status, _, _ = _call(
        port, "GET", "/sync/v1/previews/missing", headers={"Cookie": _cookie(headers)}
    )
    assert status == HTTPStatus.UNAUTHORIZED


def test_sync_config_lands_in_the_account_folder(hosted) -> None:
    port, accounts, root = hosted
    _sign_in(port)
    token = next(iter(accounts.tokens))
    body = json.dumps({"brand": None, "pillars": CONFIG.model_dump(mode="json")}).encode()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    status, _, reply = _call(port, "PUT", "/sync/v1/config", body, headers)
    assert status == HTTPStatus.OK, reply
    config_dir = root / "accounts" / account_key("owner@example.com") / "config"
    assert (config_dir / "pillars.json").is_file()


def test_sync_refuses_previews_for_unknown_items(hosted) -> None:
    port, accounts, _ = hosted
    _sign_in(port)
    token = next(iter(accounts.tokens))
    uuid = "00000000-0000-4000-8000-000000000001"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"}
    status, _, _ = _call(port, "PUT", f"/sync/v1/previews/{uuid}", b"\xff\xd8", headers)
    assert status == HTTPStatus.BAD_REQUEST


def test_sync_index_must_be_gzip(hosted) -> None:
    port, accounts, _ = hosted
    _sign_in(port)
    token = next(iter(accounts.tokens))
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/gzip"}
    status, _, _ = _call(port, "PUT", "/sync/v1/index", b"not gzip", headers)
    assert status == HTTPStatus.BAD_REQUEST


def test_gate_caches_by_token() -> None:
    accounts = FakeAccounts()
    accounts.tokens["abc"] = "owner@example.com"
    gate = SessionGate(accounts)  # type: ignore[arg-type]
    assert gate.email("abc") == "owner@example.com"
    assert gate.email("abc") == "owner@example.com"
    assert accounts.usage_calls == 1
    assert gate.email("missing") is None


def test_gate_entry_expires() -> None:
    now = [0.0]
    accounts = FakeAccounts()
    accounts.tokens["abc"] = "owner@example.com"
    gate = SessionGate(accounts, clock=lambda: now[0])  # type: ignore[arg-type]
    gate.email("abc")
    now[0] = 120.0
    accounts.tokens.clear()
    assert gate.email("abc") is None


def test_accounts_get_separate_folders(tmp_path: Path) -> None:
    tenants = Tenants(tmp_path, "https://server.test", SharedTextEncoder(lambda: None))
    first = tenants.get("One@Example.com")
    second = tenants.get("two@example.com")
    assert first.root != second.root
    assert tenants.get("one@example.com") is first
    assert "example" not in first.root.name
