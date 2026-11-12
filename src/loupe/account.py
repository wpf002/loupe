"""Loupe account sign-in for the Mac app.

Talks to the Loupe server (`server/`). The session token is kept in the macOS
Keychain, never in a file, a log line or the config. Passwords are sent once over
HTTPS and never stored.
"""

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError

REQUEST_TIMEOUT_SECONDS = 15.0
KEYCHAIN_SERVICE = "app.loupe.session"
MIN_PASSWORD_LENGTH = 12
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})
JSON_TYPE = "application/json"


class AccountError(Exception):
    """Raised with a message that is safe to show the user. Never holds a token."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SignedIn(BaseModel):
    token: str
    email: str


class Usage(BaseModel):
    email: str
    month: str
    used: int
    cap: int


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes


Transport = Callable[[str, str, bytes | None, dict[str, str]], Reply]


def validate_server_url(url: str) -> str:
    """HTTPS, or plain HTTP to this Mac for local development."""
    parts = urlsplit(url.strip())
    if parts.scheme == "https" and parts.hostname:
        return url.strip().rstrip("/")
    if parts.scheme == "http" and parts.hostname in LOCAL_HOSTS:
        return url.strip().rstrip("/")
    raise AccountError("LOUPE_SERVER_URL must be https://, or http://localhost for development")


def urllib_transport(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)  # noqa: S310 - URL checked by validate_server_url
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
            return Reply(response.status, response.read())
    except urllib.error.HTTPError as error:
        return Reply(error.code, error.read())
    except (urllib.error.URLError, TimeoutError) as error:
        raise AccountError("could not reach the Loupe server") from error


def _server_message(body: bytes) -> str | None:
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    error = parsed.get("error") if isinstance(parsed, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return message if isinstance(message, str) else None


HTTP_UNAUTHORIZED = 401
STATUS_MESSAGES = {
    HTTP_UNAUTHORIZED: "Wrong email or password",
    409: "An account with that email already exists",
    429: "Too many tries. Wait a minute and try again.",
}


class AccountClient:
    def __init__(self, server_url: str, transport: Transport = urllib_transport) -> None:
        self.server_url = validate_server_url(server_url)
        self._transport = transport

    def _call(self, method: str, path: str, payload: dict | None, token: str | None) -> bytes:
        headers = {"Accept": JSON_TYPE}
        body = None
        if payload is not None:
            headers["Content-Type"] = JSON_TYPE
            body = json.dumps(payload).encode()
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        reply = self._transport(method, self.server_url + path, body, headers)
        if 200 <= reply.status < 300:
            return reply.body
        message = STATUS_MESSAGES.get(reply.status)
        if message is None and reply.status == 400:
            message = _server_message(reply.body)
        raise AccountError(message or f"Loupe server returned HTTP {reply.status}", reply.status)

    def _signed_in(self, body: bytes) -> SignedIn:
        try:
            return SignedIn.model_validate_json(body)
        except ValidationError as error:
            raise AccountError("Loupe server sent an unexpected reply") from error

    def create_account(self, email: str, password: str) -> SignedIn:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise AccountError(f"Use at least {MIN_PASSWORD_LENGTH} characters")
        body = self._call("POST", "/v1/accounts", {"email": email, "password": password}, None)
        return self._signed_in(body)

    def sign_in(self, email: str, password: str) -> SignedIn:
        body = self._call("POST", "/v1/sessions", {"email": email, "password": password}, None)
        return self._signed_in(body)

    def sign_out(self, token: str) -> None:
        self._call("DELETE", "/v1/sessions", None, token)

    def usage(self, token: str) -> Usage:
        body = self._call("GET", "/v1/me", None, token)
        try:
            return Usage.model_validate_json(body)
        except ValidationError as error:
            raise AccountError("Loupe server sent an unexpected reply") from error


# Token storage -----------------------------------------------------------------


class TokenStore(Protocol):
    def load(self) -> str | None: ...

    def save(self, token: str) -> None: ...

    def clear(self) -> None: ...


class MemoryTokenStore:
    """For tests."""

    def __init__(self) -> None:
        self.token: str | None = None

    def load(self) -> str | None:
        return self.token

    def save(self, token: str) -> None:
        self.token = token

    def clear(self) -> None:
        self.token = None


class KeychainTokenStore:
    """One generic-password item per macOS user, readable only while the Mac is unlocked."""

    def __init__(self, macos_user: str) -> None:
        import Security

        self._security = Security
        self._account = macos_user

    def _query(self) -> dict:
        security = self._security
        return {
            security.kSecClass: security.kSecClassGenericPassword,
            security.kSecAttrService: KEYCHAIN_SERVICE,
            security.kSecAttrAccount: self._account,
        }

    def load(self) -> str | None:
        security = self._security
        query = self._query() | {
            security.kSecReturnData: True,
            security.kSecMatchLimit: security.kSecMatchLimitOne,
        }
        status, data = security.SecItemCopyMatching(query, None)
        if status == security.errSecItemNotFound:
            return None
        if status != security.errSecSuccess or data is None:
            raise AccountError(f"Keychain read failed (status {status})")
        return bytes(data).decode()

    def save(self, token: str) -> None:
        security = self._security
        self.clear()
        item = self._query() | {
            security.kSecValueData: token.encode(),
            security.kSecAttrAccessible: security.kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
        }
        status, _ = security.SecItemAdd(item, None)
        if status != security.errSecSuccess:
            raise AccountError(f"Keychain write failed (status {status})")

    def clear(self) -> None:
        security = self._security
        status = security.SecItemDelete(self._query())
        if status not in (security.errSecSuccess, security.errSecItemNotFound):
            raise AccountError(f"Keychain delete failed (status {status})")


class Session:
    """The signed-in state the app reads. Holds the token only in memory and the Keychain."""

    def __init__(self, client: AccountClient, store: TokenStore) -> None:
        self.client = client
        self._store = store

    def token(self) -> str | None:
        return self._store.load()

    def create_account(self, email: str, password: str) -> str:
        signed_in = self.client.create_account(email, password)
        self._store.save(signed_in.token)
        return signed_in.email

    def sign_in(self, email: str, password: str) -> str:
        signed_in = self.client.sign_in(email, password)
        self._store.save(signed_in.token)
        return signed_in.email

    def sign_out(self) -> None:
        token = self._store.load()
        self._store.clear()
        if token is not None:
            try:
                self.client.sign_out(token)
            except AccountError:
                # The local token is gone either way; the server expires it on schedule.
                return

    def current(self) -> Usage | None:
        """The signed-in account, or None. A token the server rejects is dropped."""
        token = self._store.load()
        if token is None:
            return None
        try:
            return self.client.usage(token)
        except AccountError as error:
            if error.status == HTTP_UNAUTHORIZED:
                self._store.clear()
                return None
            raise
