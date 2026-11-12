"""M14: the local app. Serves the Brand, Hooks, Week and Everything tabs on 127.0.0.1.

Guards: bound to loopback only, Host header must name this server (stops DNS rebinding),
every POST needs the per-process token from the page plus a JSON body under the size cap,
UUIDs are checked by pattern, thumbnails are served for eligible items only, and render
files must resolve inside data/renders. Logs method, path and status only.
"""

import html
import json
import logging
import re
import secrets
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from loupe.index import connect, is_eligible
from loupe.studio import (
    Studio,
    StudioError,
    build_everything,
    email_path,
    make_reel,
    parse_monday,
    plan_or_load_week,
    planned_week,
    rendered_reels,
    renders_dir,
    take_list,
    write_hooks,
)
from loupe.week import PlannedWeek

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY_BYTES = 16_384
TOKEN_HEADER = "X-Loupe-Token"  # noqa: S105 header name, not a secret
UUID_PATTERN = re.compile(r"^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$")
RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")
STATIC_TYPES = {
    "theme.css": "text/css; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}
RENDER_TYPES = {".png": "image/png", ".mp4": "video/mp4", ".txt": "text/plain; charset=utf-8"}
FONT_TYPES = {".ttf": "font/ttf", ".otf": "font/otf", ".ttc": "font/collection"}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self'; media-src 'self'; font-src 'self'; "
        "style-src 'self'; script-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}
MIN_REEL_SECONDS = 5.0
MAX_REEL_SECONDS = 90.0

logger = logging.getLogger("loupe.server")

Json = dict[str, object]


class HttpError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class FileReply:
    path: Path
    content_type: str


Reply = Json | FileReply | str


@dataclass
class App:
    """Request handling with no socket code, so tests can call it directly."""

    studio: Studio
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    busy: threading.Lock = field(default_factory=threading.Lock)

    def connection(self) -> sqlite3.Connection:
        return connect(self.studio.settings.index_path)

    def get(self, path: str, query: dict[str, list[str]]) -> Reply:
        if path == "/":
            return self.index_page()
        if path.startswith("/static/"):
            return self.static_file(path.removeprefix("/static/"))
        if path in ("/fonts/display", "/fonts/body"):
            font = (
                self.studio.brand.fonts.display
                if path.endswith("display")
                else self.studio.brand.fonts.body
            )
            return FileReply(font, FONT_TYPES[font.suffix.lower()])
        if path.startswith("/media/thumb/"):
            return self.thumb(_uuid(path.removeprefix("/media/thumb/")))
        if path.startswith("/media/render/"):
            return self.render_file(unquote(path.removeprefix("/media/render/")))
        routes: dict[str, Callable[[], Json]] = {
            "/api/state": self.state,
            "/api/brand": self.brand,
            "/api/takes": self.takes,
            "/api/week": lambda: self.week(_first(query, "start")),
            "/api/everything": lambda: self.everything(_first(query, "start")),
        }
        if path in routes:
            return routes[path]()
        hooks = re.fullmatch(r"/api/takes/([^/]+)/hooks", path)
        if hooks:
            return self.hooks(_uuid(hooks.group(1)))
        raise HttpError(HTTPStatus.NOT_FOUND, "Not found")

    def post(self, path: str, body: Json) -> Json:
        if not self.busy.acquire(blocking=False):
            raise HttpError(HTTPStatus.CONFLICT, "Another job is running")
        try:
            if path == "/api/week":
                return self.plan(_optional_str(body, "start"), bool(body.get("fresh", False)))
            if path == "/api/everything":
                return self.build(_optional_str(body, "start"), bool(body.get("fresh", False)))
            take_route = re.fullmatch(r"/api/takes/([^/]+)/(hooks|reel)", path)
            if take_route and take_route.group(2) == "hooks":
                return self.new_hooks(_uuid(take_route.group(1)), bool(body.get("fresh", False)))
            if take_route:
                return self.reel(_uuid(take_route.group(1)), body)
            raise HttpError(HTTPStatus.NOT_FOUND, "Not found")
        finally:
            self.busy.release()

    def index_page(self) -> str:
        template = files("loupe.web").joinpath("app.html").read_text(encoding="utf-8")
        return template.replace("{{token}}", html.escape(self.token, quote=True))

    def static_file(self, name: str) -> FileReply:
        if name not in STATIC_TYPES:
            raise HttpError(HTTPStatus.NOT_FOUND, "Not found")
        return FileReply(Path(str(files("loupe.web").joinpath(name))), STATIC_TYPES[name])

    def thumb(self, uuid: str) -> FileReply:
        from loupe.sheets import Tile, thumbnail

        with _closing(self.connection()) as connection:
            if not is_eligible(connection, uuid):
                raise HttpError(HTTPStatus.NOT_FOUND, "Not found")
            row = connection.execute(
                "SELECT preview_path, media_type FROM items WHERE uuid = ?", (uuid,)
            ).fetchone()
        thumbs = self.studio.settings.data_dir / "review" / "thumbs"
        found = thumbnail(Tile(uuid, row[0], row[1]), thumbs)
        if found is None:
            raise HttpError(HTTPStatus.NOT_FOUND, "No preview")
        return FileReply(found, "image/jpeg")

    def render_file(self, relative: str) -> FileReply:
        root = renders_dir(self.studio.settings).resolve()
        target = (root / relative).resolve()
        content_type = RENDER_TYPES.get(target.suffix.lower())
        if not target.is_relative_to(root) or content_type is None or not target.is_file():
            raise HttpError(HTTPStatus.NOT_FOUND, "Not found")
        return FileReply(target, content_type)

    def render_url(self, path: Path) -> str:
        relative = path.resolve().relative_to(renders_dir(self.studio.settings).resolve())
        return f"/media/render/{relative.as_posix()}?v={path.stat().st_mtime_ns}"

    def state(self) -> Json:
        from loupe.week import latest_week

        with _closing(self.connection()) as connection:
            planned = latest_week(connection)
            takes = len(take_list(connection))
        return {
            "brand": self.studio.brand.name,
            "takes": takes,
            "week": planned.start.isoformat() if planned else None,
            "busy": self.busy.locked(),
        }

    def brand(self) -> Json:
        profile = self.studio.brand
        return {
            "name": profile.name,
            "colors": profile.colors.model_dump(),
            "fonts": {"display": profile.fonts.display.stem, "body": profile.fonts.body.stem},
            "voice": {
                "traits": list(profile.voice.traits),
                "avoid": list(profile.voice.avoid),
                "comment_word": profile.voice.comment_word,
                "samples": list(profile.voice.samples),
            },
        }

    def takes(self) -> Json:
        with _closing(self.connection()) as connection:
            rows = take_list(connection)
        return {
            "takes": [
                {
                    "uuid": row.uuid,
                    "seconds": round(row.seconds, 1),
                    "words": row.words,
                    "opening": row.opening,
                    "has_hooks": row.has_hooks,
                }
                for row in rows
            ]
        }

    def _reels(self, uuid: str) -> list[Json]:
        from loupe.reel import reel_path

        renders = renders_dir(self.studio.settings)
        return [
            {"hook": number, "url": self.render_url(reel_path(renders, uuid, number))}
            for number in rendered_reels(self.studio.settings, uuid)
        ]

    def hooks(self, uuid: str) -> Json:
        from loupe.hooks import load_hooks

        with _closing(self.connection()) as connection:
            stored = load_hooks(connection, uuid)
        return {"hooks": list(stored) if stored else None, "reels": self._reels(uuid)}

    def new_hooks(self, uuid: str, fresh: bool) -> Json:
        with _closing(self.connection()) as connection:
            lines = write_hooks(
                connection, uuid, self.studio.brand, self.studio.client_factory, fresh
            )
        return {"hooks": list(lines), "reels": self._reels(uuid)}

    def reel(self, uuid: str, body: Json) -> Json:
        hook = body.get("hook")
        length = body.get("length")
        if not isinstance(hook, int) or not isinstance(length, int | float):
            raise HttpError(HTTPStatus.BAD_REQUEST, "hook and length are required")
        if not MIN_REEL_SECONDS <= float(length) <= MAX_REEL_SECONDS:
            raise HttpError(HTTPStatus.BAD_REQUEST, "length must be 5 to 90 seconds")
        with _closing(self.connection()) as connection:
            target, seconds = make_reel(
                self.studio.settings,
                connection,
                self.studio.brand,
                uuid,
                hook,
                float(length),
                self.studio.find_original,
            )
        return {"url": self.render_url(target), "seconds": round(seconds, 1), "hook": hook}

    def week(self, start: str | None) -> Json:
        with _closing(self.connection()) as connection:
            try:
                planned = planned_week(connection, start)
            except StudioError:
                return {"week": None}
            return {"week": self.week_json(connection, planned)}

    def plan(self, start: str | None, fresh: bool) -> Json:
        from loupe.week import week_start

        monday = parse_monday(start) if start else week_start(date.today())
        with _closing(self.connection()) as connection:
            planned = plan_or_load_week(connection, monday, self.studio.planner, fresh)
            return {"week": self.week_json(connection, planned)}

    def week_json(self, connection: sqlite3.Connection, planned: PlannedWeek) -> Json:
        posts = []
        for index, post in enumerate(planned.plan.posts):
            assets = []
            for uuid in planned.assets.get(index, []):
                row = connection.execute(
                    "SELECT media_type FROM items WHERE uuid = ? AND excluded_reason IS NULL",
                    (uuid,),
                ).fetchone()
                if row is not None:
                    assets.append({"uuid": uuid, "media_type": row[0]})
            posts.append(
                {
                    "day": post.day,
                    "pillar": post.pillar,
                    "format": post.format.value,
                    "angle": post.angle,
                    "caption": post.caption,
                    "slides": post.slides,
                    "story_frames": post.story_frames,
                    "take": planned.take_uuids.get(index),
                    "assets": assets,
                }
            )
        return {"start": planned.start.isoformat(), "posts": posts}

    def everything(self, start: str | None) -> Json:
        from loupe.carousel import carousel_dir
        from loupe.newsletter import load_email
        from loupe.story import story_dir

        with _closing(self.connection()) as connection:
            try:
                planned = planned_week(connection, start)
            except StudioError:
                return {"start": None, "carousels": [], "stories": [], "email": None}
            draft = load_email(connection, planned.start.isoformat())
        renders = renders_dir(self.studio.settings)
        week_key = planned.start.isoformat()
        sets: dict[str, list[Json]] = {"carousel": [], "story": []}
        for post in planned.plan.posts:
            if post.format.value not in sets:
                continue
            maker = carousel_dir if post.format.value == "carousel" else story_dir
            folder = maker(renders, week_key, post.day)
            images = sorted(folder.glob("*.png")) if folder.is_dir() else []
            if images:
                sets[post.format.value].append(
                    {
                        "day": post.day,
                        "pillar": post.pillar,
                        "files": [self.render_url(image) for image in images],
                    }
                )
        text_file = email_path(self.studio.settings, planned)
        return {
            "start": week_key,
            "carousels": sets["carousel"],
            "stories": sets["story"],
            "email": draft.model_dump() if draft else None,
            "email_file": self.render_url(text_file) if text_file.is_file() else None,
        }

    def build(self, start: str | None, fresh: bool) -> Json:
        with _closing(self.connection()) as connection:
            planned = planned_week(connection, start)
            build_everything(
                self.studio.settings,
                connection,
                self.studio.brand,
                planned,
                self.studio.client_factory,
                fresh,
            )
        return self.everything(planned.start.isoformat())


class _closing:
    """Context manager that closes a sqlite connection without committing anything extra."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(self, *exc: object) -> None:
        self.connection.close()


def _uuid(value: str) -> str:
    if not UUID_PATTERN.fullmatch(value):
        raise HttpError(HTTPStatus.BAD_REQUEST, "Bad id")
    return value


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _optional_str(body: Json, key: str) -> str | None:
    value = body.get(key)
    if value is None or isinstance(value, str):
        return value or None
    raise HttpError(HTTPStatus.BAD_REQUEST, f"{key} must be a string")


def allowed_hosts(port: int) -> set[str]:
    return {f"{HOST}:{port}", f"localhost:{port}"}


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Inclusive byte range from a Range header, or None to send the whole file."""
    if not header:
        return None
    match = RANGE_PATTERN.fullmatch(header.strip())
    if match is None or not any(match.groups()):
        return None
    first, last = match.groups()
    if first == "":
        start = max(size - int(last), 0)
        end = size - 1
    else:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    if start > end or start >= size:
        raise HttpError(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Bad range")
    return start, end


def handler_for(app: App, port: int) -> type[BaseHTTPRequestHandler]:
    hosts = allowed_hosts(port)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Loupe"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """Silence the default line; _log writes method, path and status."""

        def _log(self, status: int) -> None:
            logger.info("%s %s %d", self.command, urlsplit(self.path).path, status)

        def _check_host(self) -> None:
            if self.headers.get("Host") not in hosts:
                raise HttpError(HTTPStatus.FORBIDDEN, "Wrong host")

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch(lambda: self._get())

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch(lambda: self._get(), head=True)

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch(lambda: app.post(urlsplit(self.path).path, self._json_body()))

        def _get(self) -> Reply:
            parts = urlsplit(self.path)
            return app.get(parts.path, parse_qs(parts.query))

        def _json_body(self) -> Json:
            token = self.headers.get(TOKEN_HEADER, "")
            if not secrets.compare_digest(token, app.token):
                raise HttpError(HTTPStatus.FORBIDDEN, "Missing token")
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                raise HttpError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON only")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise HttpError(HTTPStatus.BAD_REQUEST, "Bad length") from error
            if length > MAX_BODY_BYTES:
                raise HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Body too large")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                body = json.loads(raw)
            except json.JSONDecodeError as error:
                raise HttpError(HTTPStatus.BAD_REQUEST, "Bad JSON") from error
            if not isinstance(body, dict):
                raise HttpError(HTTPStatus.BAD_REQUEST, "Body must be an object")
            return body

        def _dispatch(self, action: Callable[[], Reply], head: bool = False) -> None:
            try:
                self._check_host()
                reply = action()
            except HttpError as error:
                self._send_json(error.status, {"error": error.message})
                return
            except StudioError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            except Exception:
                logger.exception("%s %s failed", self.command, urlsplit(self.path).path)
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal error"})
                return
            if isinstance(reply, FileReply):
                self._send_file(reply, head)
            elif isinstance(reply, str):
                self._send_bytes(HTTPStatus.OK, reply.encode(), "text/html; charset=utf-8", head)
            else:
                self._send_json(HTTPStatus.OK, reply, head)

        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self._log(status)

        def _send_bytes(self, status: int, body: bytes, content_type: str, head: bool) -> None:
            self._headers(status, content_type, len(body))
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _send_json(self, status: int, body: Json, head: bool = False) -> None:
            self._send_bytes(status, json.dumps(body).encode(), "application/json", head)

        def _send_file(self, reply: FileReply, head: bool) -> None:
            size = reply.path.stat().st_size
            try:
                byte_range = parse_range(self.headers.get("Range"), size)
            except HttpError as error:
                self._send_json(error.status, {"error": error.message})
                return
            start, end = byte_range if byte_range else (0, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT if byte_range else HTTPStatus.OK
            self._headers(status, reply.content_type, max(end - start + 1, 0))
            self.send_header("Accept-Ranges", "bytes")
            if byte_range:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head or size == 0:
                return
            with reply.path.open("rb") as source:
                source.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = source.read(min(remaining, 1 << 16))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    return Handler


def make_server(app: App, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((HOST, port), handler_for(app, port))
    server.daemon_threads = True
    return server


def serve(studio: Studio, port: int = DEFAULT_PORT) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    server = make_server(App(studio), port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("stopping")
    finally:
        server.server_close()
