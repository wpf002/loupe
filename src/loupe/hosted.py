"""The Loupe web service on Railway: the full app in a browser, one library per account.

Loupe.app syncs an account's library here (see `loupe.sync`). The browser signs in
with the account's Loupe email and password; the session token from the Loupe server
is kept in an HttpOnly cookie and checked against the Loupe server on each request
(cached for a minute). Each account's data lives under its own folder on the volume,
named by a hash of the email. Sorting and album writes need Photos, so they stay in
Loupe.app. Logs method, path, status and counts only.
"""

import gzip
import hashlib
import io
import json
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import numpy as np
from pydantic import BaseModel, Field, ValidationError, field_validator

from loupe.account import AccountClient, AccountError, validate_server_url
from loupe.brand import BrandProfile
from loupe.claude import ProxyAuth, StructuredClient
from loupe.config import PillarConfig
from loupe.mac import DEFAULT_PILLARS, no_originals, unavailable_planner
from loupe.server import (
    HEALTH_PATH,
    STATIC_TYPES,
    App,
    FileReply,
    HttpError,
    Json,
    LoupeHandler,
    Redirect,
    Reply,
    _credentials,
    public_hostname,
)
from loupe.settings import DEFAULT_ALBUM_FOLDER, Settings
from loupe.sorting import SortError
from loupe.studio import NO_BRAND, Studio, StudioError, claude_client, search_planner
from loupe.sync_index import SyncIndexError, import_index
from loupe.sync_store import (
    BRAND_FILE,
    FONT_SUFFIXES,
    FONTS_DIR,
    PILLARS_FILE,
    BrandUpload,
    SyncStoreError,
    check_uuid,
    missing_previews,
    missing_vectors,
    store_config,
    store_font,
    store_model,
    store_preview,
    store_vectors,
)

COOKIE_NAME = "__Host-loupe"
COOKIE_MAX_AGE_SECONDS = 30 * 24 * 3600
SESSION_CACHE_SECONDS = 60
SESSION_CACHE_SIZE = 4096
SIGN_IN_WINDOW_SECONDS = 15 * 60
SIGN_IN_ATTEMPTS = 10
SIGN_IN_TRACKED = 4096
HSTS = "max-age=31536000; includeSubDomains"
MAX_INDEX_UPLOAD = 256 * 1024 * 1024
MAX_INDEX_SQLITE = 1024 * 1024 * 1024
MAX_PREVIEW_UPLOAD = 4 * 1024 * 1024
MAX_VECTOR_UPLOAD = 16 * 1024 * 1024
MAX_MODEL_UPLOAD = 16 * 1024 * 1024
MAX_CONFIG_UPLOAD = 256 * 1024
MAX_FONT_UPLOAD = 8 * 1024 * 1024
COPY_CHUNK = 1 << 20
BIND_ALL = "0.0.0.0"  # noqa: S104 - container port, fronted by Railway's proxy
DEFAULT_PAGE = 500
LOGIN_PAGE = "login.html"
OPEN_STATIC = frozenset({"theme.css", "app.css", "login.js"})
SIGNED_OUT = "Sign in again"
NO_SORTING = "Sorting runs in Loupe.app on your Mac"

logger = logging.getLogger("loupe.hosted")


class HostedConfig(BaseModel):
    """Read from the environment at startup. A bad value stops the service."""

    root: Path
    server_url: str
    public_host: str
    port: int = Field(ge=1, le=65535)

    @field_validator("server_url")
    @classmethod
    def _server_url(cls, value: str) -> str:
        try:
            return validate_server_url(value)
        except AccountError as error:
            raise ValueError(str(error)) from error

    @field_validator("public_host")
    @classmethod
    def _public_host(cls, value: str) -> str:
        return public_hostname(value)

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "HostedConfig":
        return cls(
            root=Path(env.get("LOUPE_WEB_ROOT", "/data")),
            server_url=env.get("LOUPE_SERVER_URL", ""),
            public_host=env.get("LOUPE_PUBLIC_HOST") or env.get("RAILWAY_PUBLIC_DOMAIN", ""),
            port=int(env.get("PORT", "8080")),
        )

    @property
    def origin(self) -> str:
        return f"https://{self.public_host}"


# The session token of the request being served. Claude calls go through the Loupe
# server with it, so each account's usage counts against its own cap.
_request = threading.local()


def request_token() -> str | None:
    return getattr(_request, "token", None)


def request_client_factory(server_url: str) -> Callable[[], StructuredClient]:
    def build() -> StructuredClient:
        token = request_token()
        if token is None:
            raise StudioError("Sign in to use Claude")
        return claude_client(ProxyAuth(server_url, token))

    return build


class SharedTextEncoder:
    """One SigLIP text tower for every account. Search only encodes text on the server."""

    def __init__(self, build: Callable[[], Any]) -> None:
        self._build = build
        self._lock = threading.Lock()
        self._encoder: Any = None

    def _instance(self) -> Any:
        with self._lock:
            if self._encoder is None:
                self._encoder = self._build()
            return self._encoder

    @property
    def model_id(self) -> str:
        from loupe.encoder import MODEL_ID

        return MODEL_ID

    def encode_texts(self, texts: list[str]) -> np.ndarray:
        encoder = self._instance()
        with self._lock:
            return encoder.encode_texts(texts)

    def encode(self, _batch: object) -> np.ndarray:
        raise SortError(NO_SORTING)

    def preprocess(self, _image: object) -> object:
        raise SortError(NO_SORTING)

    def preload(self) -> threading.Thread:
        def load() -> None:
            try:
                self._instance()
                logger.info("search encoder loaded")
            except Exception:
                logger.exception("search encoder failed to load")

        thread = threading.Thread(target=load, name="loupe-encoder-preload", daemon=True)
        thread.start()
        return thread


def cpu_text_encoder() -> Any:
    from loupe.encoder import SiglipEncoder

    return SiglipEncoder(device="cpu", precision="bf16", text_only=True)


def refusing_writer() -> Any:
    raise SortError(NO_SORTING)


def account_key(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def load_hosted_brand(config_dir: Path) -> BrandProfile | None:
    path = config_dir / BRAND_FILE
    if not path.is_file():
        return None
    fonts = config_dir / FONTS_DIR
    found = {role: _font_file(fonts, role) for role in ("display", "body")}
    if None in found.values():
        return None
    try:
        upload = BrandUpload.model_validate_json(path.read_bytes())
        return BrandProfile.model_validate({**upload.model_dump(mode="json"), "fonts": found})
    except ValidationError:
        logger.warning("synced brand is invalid")
        return None


def _font_file(fonts: Path, role: str) -> Path | None:
    candidates = (fonts / f"{role}.{suffix}" for suffix in sorted(FONT_SUFFIXES))
    return next((path for path in candidates if path.is_file()), None)


def load_hosted_pillars(config_dir: Path) -> PillarConfig:
    path = config_dir / PILLARS_FILE
    if not path.is_file():
        return DEFAULT_PILLARS
    try:
        return PillarConfig.model_validate_json(path.read_bytes())
    except ValidationError:
        logger.warning("synced pillars are invalid")
        return DEFAULT_PILLARS


@dataclass
class Tenant:
    """One account's folder on the volume and its App, rebuilt after a sync changes it."""

    email: str
    root: Path
    server_url: str
    encoder: SharedTextEncoder
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    sync_lock: threading.Lock = field(default_factory=threading.Lock)
    _app_lock: threading.Lock = field(default_factory=threading.Lock)
    _app: App | None = None

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def settings(self) -> Settings:
        return Settings(
            data_dir=self.data_dir,
            photos_library=None,
            album_folder=DEFAULT_ALBUM_FOLDER,
            owner_macos_user=None,
            server_url=self.server_url,
        )

    @property
    def previews_dir(self) -> Path:
        return self.data_dir / "previews"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "review" / "thumbs"

    def app(self) -> App:
        with self._app_lock:
            if self._app is None:
                self._app = self._build()
            return self._app

    def invalidate(self) -> None:
        with self._app_lock:
            self._app = None

    def _build(self) -> App:
        from loupe.sorting import Sorter

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        settings = self.settings
        brand = load_hosted_brand(self.config_dir)
        pillars = load_hosted_pillars(self.config_dir)
        client_factory = request_client_factory(self.server_url)
        encoder = self.encoder
        planner = (
            unavailable_planner(NO_BRAND)
            if brand is None
            else search_planner(settings, pillars, brand, client_factory, lambda: encoder)
        )
        studio = Studio(
            settings=settings,
            brand=brand,
            client_factory=client_factory,
            planner=planner,
            find_original=no_originals,
        )
        sorter = Sorter(
            self.data_dir, settings.index_path, pillars, lambda: encoder, refusing_writer
        )
        return App(studio, token=self.csrf, sorter=sorter, hosted_account=self.email)


class Tenants:
    def __init__(self, root: Path, server_url: str, encoder: SharedTextEncoder) -> None:
        self._root = root / "accounts"
        self._server_url = server_url
        self._encoder = encoder
        self._lock = threading.Lock()
        self._tenants: dict[str, Tenant] = {}

    def get(self, email: str) -> Tenant:
        key = account_key(email)
        with self._lock:
            tenant = self._tenants.get(key)
            if tenant is None:
                tenant = Tenant(email, self._root / key, self._server_url, self._encoder)
                self._tenants[key] = tenant
            return tenant


class SessionGate:
    """Checks a session token with the Loupe server. Results are cached briefly by hash."""

    def __init__(
        self, accounts: AccountClient, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._accounts = accounts
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, tuple[str, float]] = OrderedDict()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def email(self, token: str) -> str | None:
        key = self._key(token)
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[1] > now:
                return cached[0]
        try:
            email = self._accounts.usage(token).email
        except AccountError as error:
            if error.status == HTTPStatus.UNAUTHORIZED:
                self.forget(token)
                return None
            raise HttpError(HTTPStatus.BAD_GATEWAY, "Couldn't reach the Loupe server") from error
        with self._lock:
            self._cache[key] = (email, now + SESSION_CACHE_SECONDS)
            self._cache.move_to_end(key)
            while len(self._cache) > SESSION_CACHE_SIZE:
                self._cache.popitem(last=False)
        return email

    def forget(self, token: str) -> None:
        with self._lock:
            self._cache.pop(self._key(token), None)


class SignInLimiter:
    """Fixed window per client address, so one address can't guess passwords quickly."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: OrderedDict[str, tuple[float, int]] = OrderedDict()

    def allow(self, address: str) -> bool:
        now = self._clock()
        with self._lock:
            start, count = self._windows.get(address, (now, 0))
            if now - start > SIGN_IN_WINDOW_SECONDS:
                start, count = now, 0
            self._windows[address] = (start, count + 1)
            self._windows.move_to_end(address)
            while len(self._windows) > SIGN_IN_TRACKED:
                self._windows.popitem(last=False)
            return count < SIGN_IN_ATTEMPTS


def session_cookie(token: str) -> str:
    return (
        f"{COOKIE_NAME}={token}; Path=/; Max-Age={COOKIE_MAX_AGE_SECONDS}; "
        "HttpOnly; Secure; SameSite=Lax"
    )


def cleared_cookie() -> str:
    return f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"


@dataclass(frozen=True)
class CookieReply:
    body: Json
    cookie: str


@dataclass(frozen=True)
class Service:
    config: HostedConfig
    accounts: AccountClient
    gate: SessionGate
    tenants: Tenants
    limiter: SignInLimiter


class HostedHandler(LoupeHandler):
    service: Service

    # Request routing ----------------------------------------------------------------

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch(self._sync)

    def _get(self) -> Reply:
        path = urlsplit(self.path).path
        if path == HEALTH_PATH:
            return {"ok": True}
        if path == "/login":
            return files("loupe.web").joinpath(LOGIN_PAGE).read_text(encoding="utf-8")
        if path.startswith("/static/") and path.removeprefix("/static/") in OPEN_STATIC:
            name = path.removeprefix("/static/")
            return FileReply(Path(str(files("loupe.web").joinpath(name))), STATIC_TYPES[name])
        if path.startswith("/sync/"):
            return self._sync()
        if self._cookie_token() is None and path == "/":
            return Redirect("/login")
        try:
            return super()._get()
        except HttpError as error:
            if error.status == HTTPStatus.UNAUTHORIZED and path == "/":
                return Redirect("/login", (("Set-Cookie", cleared_cookie()),))
            raise

    def _post(self) -> Reply:
        path = urlsplit(self.path).path
        if path.startswith("/sync/"):
            return self._sync()
        self._check_origin()
        if path == "/auth/sign-in":
            return self._sign_in()
        if path == "/auth/sign-out":
            return self._sign_out()
        return super()._post()

    def _dispatch(self, action: Callable[[], Reply], head: bool = False) -> None:
        try:
            super()._dispatch(action, head)
        finally:
            _request.token = None

    def send_reply(self, reply: Reply, head: bool = False) -> None:
        if isinstance(reply, CookieReply):
            body = json.dumps(reply.body).encode()
            self._headers(HTTPStatus.OK, "application/json", len(body))
            self.send_header("Set-Cookie", reply.cookie)
            self.end_headers()
            if not head:
                self.wfile.write(body)
            return
        super().send_reply(reply, head)

    # Browser session ----------------------------------------------------------------

    def _cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie(raw)
        except CookieError:
            return None
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel is not None and morsel.value else None

    def _bearer_token(self) -> str | None:
        header = self.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        return token.strip() or None if scheme == "Bearer" else None

    def _tenant_for(self, token: str | None) -> Tenant:
        if token is None:
            raise HttpError(HTTPStatus.UNAUTHORIZED, SIGNED_OUT)
        email = self.service.gate.email(token)
        if email is None:
            raise HttpError(HTTPStatus.UNAUTHORIZED, SIGNED_OUT)
        _request.token = token
        return self.service.tenants.get(email)

    def resolve_app(self) -> App:
        return self._tenant_for(self._cookie_token()).app()

    def _check_origin(self) -> None:
        if self.headers.get("Origin") != self.service.config.origin:
            raise HttpError(HTTPStatus.FORBIDDEN, "Wrong origin")

    def _client_address(self) -> str:
        # Railway's proxy appends the caller's address last. Earlier entries can be forged.
        forwarded = self.headers.get("X-Forwarded-For", "")
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        return hops[-1] if hops else self.client_address[0]

    def _sign_in(self) -> Reply:
        if not self.service.limiter.allow(self._client_address()):
            raise HttpError(HTTPStatus.TOO_MANY_REQUESTS, "Too many tries. Wait 15 minutes.")
        email, password = _credentials(self._json_body(None))
        try:
            signed_in = self.service.accounts.sign_in(email, password)
        except AccountError as error:
            passed = error.status in (400, 401, 429)
            status = HTTPStatus(error.status) if passed else HTTPStatus.BAD_GATEWAY
            raise HttpError(status, str(error)) from error
        logger.info("signed in")
        return CookieReply({"ok": True}, session_cookie(signed_in.token))

    def _sign_out(self) -> Reply:
        token = self._cookie_token()
        self._json_body(None)
        if token is not None:
            self.service.gate.forget(token)
            try:
                self.service.accounts.sign_out(token)
            except AccountError as error:
                if error.status != HTTPStatus.UNAUTHORIZED:
                    raise HttpError(HTTPStatus.BAD_GATEWAY, str(error)) from error
        return CookieReply({"ok": True}, cleared_cookie())

    # Sync API, called by Loupe.app with the account's bearer token ------------------

    def _sync(self) -> Reply:
        tenant = self._tenant_for(self._bearer_token())
        parts = urlsplit(self.path)
        route = (self.command, parts.path.removeprefix("/sync/v1/"))
        query = parse_qs(parts.query)
        try:
            return self._sync_route(tenant, route, query)
        except (SyncStoreError, SyncIndexError) as error:
            raise HttpError(HTTPStatus.BAD_REQUEST, str(error)) from error

    def _sync_route(
        self, tenant: Tenant, route: tuple[str, str], query: dict[str, list[str]]
    ) -> Reply:
        method, path = route
        after = query.get("after", [""])[0]
        limit = _page_limit(query)
        if method == "GET" and path == "previews/missing":
            missing = missing_previews(
                tenant.settings.index_path, tenant.previews_dir, after, limit
            )
            return {"missing": missing}
        if method == "GET" and path == "vectors/missing":
            return {"missing": missing_vectors(tenant.settings.index_path, after, limit)}
        if method == "PUT" and path == "index":
            return self._sync_index(tenant)
        if method == "PUT" and path.startswith("previews/"):
            uuid = check_uuid(path.removeprefix("previews/"))
            body = self._raw_body(MAX_PREVIEW_UPLOAD)
            store_preview(
                tenant.settings.index_path, tenant.previews_dir, tenant.thumbs_dir, uuid, body
            )
            return {"ok": True}
        if method == "POST" and path == "vectors":
            with tenant.sync_lock:
                added = store_vectors(
                    tenant.settings.index_path, tenant.data_dir, self._raw_body(MAX_VECTOR_UPLOAD)
                )
            tenant.invalidate()
            return {"added": added}
        if method == "PUT" and path == "model":
            with tenant.sync_lock:
                store_model(tenant.data_dir, self._raw_body(MAX_MODEL_UPLOAD))
            tenant.invalidate()
            return {"ok": True}
        if method == "PUT" and path == "config":
            store_config(tenant.config_dir, self._raw_body(MAX_CONFIG_UPLOAD))
            tenant.invalidate()
            return {"ok": True}
        if method == "PUT" and path.startswith("fonts/"):
            role, _, suffix = path.removeprefix("fonts/").partition(".")
            store_font(tenant.config_dir, role, suffix.lower(), self._raw_body(MAX_FONT_UPLOAD))
            tenant.invalidate()
            return {"ok": True}
        raise HttpError(HTTPStatus.NOT_FOUND, "Not found")

    def _raw_body(self, limit: int) -> bytes:
        length = self._content_length(limit)
        return self.rfile.read(length) if length > 0 else b""

    def _sync_index(self, tenant: Tenant) -> Json:
        length = self._content_length(MAX_INDEX_UPLOAD)
        tenant.data_dir.mkdir(parents=True, exist_ok=True)
        packed = tenant.data_dir / ".upload.sqlite.gz"
        unpacked = tenant.data_dir / ".upload.sqlite"
        with tenant.sync_lock:
            try:
                _copy_exact(self.rfile, packed, length)
                _gunzip_capped(packed, unpacked, MAX_INDEX_SQLITE)
                items = import_index(tenant.settings.index_path, unpacked, tenant.previews_dir)
            finally:
                packed.unlink(missing_ok=True)
                unpacked.unlink(missing_ok=True)
        tenant.invalidate()
        logger.info("index synced: %d items", items)
        return {"items": items}


def _page_limit(query: dict[str, list[str]]) -> int:
    raw = query.get("limit", [str(DEFAULT_PAGE)])[0]
    try:
        return int(raw)
    except ValueError as error:
        raise HttpError(HTTPStatus.BAD_REQUEST, "Bad limit") from error


def _copy_exact(source: io.BufferedIOBase, target: Path, length: int) -> None:
    remaining = length
    with target.open("wb") as handle:
        while remaining > 0:
            chunk = source.read(min(remaining, COPY_CHUNK))
            if not chunk:
                raise HttpError(HTTPStatus.BAD_REQUEST, "Upload ended early")
            handle.write(chunk)
            remaining -= len(chunk)


def _gunzip_capped(source: Path, target: Path, limit: int) -> None:
    written = 0
    try:
        with gzip.open(source, "rb") as packed, target.open("wb") as handle:
            while chunk := packed.read(COPY_CHUNK):
                written += len(chunk)
                if written > limit:
                    raise HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Library is too large")
                handle.write(chunk)
    except (OSError, EOFError) as error:
        raise HttpError(HTTPStatus.BAD_REQUEST, "Upload isn't gzip") from error


def handler_for_service(service: Service) -> type[HostedHandler]:
    return type(
        "Handler",
        (HostedHandler,),
        {
            "service": service,
            "hosts": frozenset({service.config.public_host}),
            "extra_headers": {"Strict-Transport-Security": HSTS},
        },
    )


def build_service(
    config: HostedConfig, encoder: SharedTextEncoder, accounts: AccountClient | None = None
) -> Service:
    client = accounts or AccountClient(config.server_url)
    return Service(
        config=config,
        accounts=client,
        gate=SessionGate(client),
        tenants=Tenants(config.root, config.server_url, encoder),
        limiter=SignInLimiter(),
    )


def run(env: dict[str, str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    config = HostedConfig.from_env(dict(os.environ if env is None else env))
    config.root.mkdir(parents=True, exist_ok=True)
    encoder = SharedTextEncoder(cpu_text_encoder)
    encoder.preload()
    handler = handler_for_service(build_service(config, encoder))
    # Railway routes the public domain to this port inside the container.
    server = ThreadingHTTPServer((BIND_ALL, config.port), handler)
    server.daemon_threads = True
    logger.info("serving on port %d", config.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
