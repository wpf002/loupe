"""Sync from Loupe.app to the Loupe web service, so the account works from any browser.

Runs in Loupe.app, which is the only place that reads Photos. Sends, in order: the
eligible index (no person names, no excluded UUIDs), brand and pillar settings, fonts,
the pillar model, 1080px JPEG previews, then vectors. Each step asks the server what it
is missing, so a stopped sync resumes where it left off. Logs counts only.
"""

import gzip
import io
import json
import logging
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlencode

import numpy as np

from loupe.account import Reply, validate_server_url
from loupe.brand import BrandProfile
from loupe.config import Exclusions, PillarConfig
from loupe.embed import vector_path
from loupe.encoder import MODEL_ID
from loupe.index import connect
from loupe.previews import SkipReason, load_preview
from loupe.sync_index import export_index
from loupe.train import model_path
from loupe.vectors import VectorStore

PREVIEW_EDGE = 1080
PREVIEW_QUALITY = 82
PREVIEW_THREADS = 4
PAGE_SIZE = 500
VECTOR_BATCH = 2000
SHORT_TIMEOUT_SECONDS = 30
UPLOAD_TIMEOUT_SECONDS = 600
NOT_SIGNED_IN = "Sign in first"
SIGNED_OUT = "Your session ended. Sign in again."
UNREACHABLE = "Couldn't reach the Loupe website"
TOO_OLD = "Update Loupe.app, then sync again"

logger = logging.getLogger("loupe.sync")

Transport = Callable[[str, str, bytes | None, dict[str, str], float], Reply]


class SyncError(Exception):
    """A sync step failed. The message is safe to show."""


class SyncStage(StrEnum):
    IDLE = "idle"
    LIBRARY = "library"
    SETTINGS = "settings"
    PREVIEWS = "previews"
    VECTORS = "vectors"
    DONE = "done"
    FAILED = "failed"


RUNNING = frozenset({SyncStage.LIBRARY, SyncStage.SETTINGS, SyncStage.PREVIEWS, SyncStage.VECTORS})


@dataclass(frozen=True)
class SyncProgress:
    stage: SyncStage = SyncStage.IDLE
    done: int = 0
    total: int = 0
    skipped: int = 0
    error: str | None = None


def urllib_transport(
    method: str, url: str, body: bytes | None, headers: dict[str, str], timeout: float
) -> Reply:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)  # noqa: S310 - URL checked by validate_server_url
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return Reply(response.status, response.read())
    except urllib.error.HTTPError as error:
        return Reply(error.code, error.read())
    except (urllib.error.URLError, TimeoutError) as error:
        raise SyncError(UNREACHABLE) from error


def _message(reply: Reply) -> str:
    try:
        parsed = json.loads(reply.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return f"Loupe website returned HTTP {reply.status}"
    error = parsed.get("error") if isinstance(parsed, dict) else None
    return error if isinstance(error, str) else f"Loupe website returned HTTP {reply.status}"


class WebClient:
    """The Sync API on the web service. Authenticates with the account's session token."""

    def __init__(self, web_url: str, token: str, transport: Transport = urllib_transport) -> None:
        self.web_url = validate_server_url(web_url)
        self._token = token
        self._transport = transport

    def call(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/octet-stream",
        timeout: float = SHORT_TIMEOUT_SECONDS,
    ) -> bytes:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type
        reply = self._transport(method, self.web_url + path, body, headers, timeout)
        if reply.status == 401:
            raise SyncError(SIGNED_OUT)
        if not 200 <= reply.status < 300:
            raise SyncError(_message(reply))
        return reply.body

    def missing(self, kind: str) -> Iterator[list[str]]:
        """Pages of UUIDs the server still needs. `kind` is previews or vectors."""
        after = ""
        while True:
            query = urlencode({"after": after, "limit": PAGE_SIZE})
            page = json.loads(self.call("GET", f"/sync/v1/{kind}/missing?{query}"))
            uuids = page.get("missing", []) if isinstance(page, dict) else []
            if not uuids:
                return
            yield [str(uuid) for uuid in uuids]
            after = str(uuids[-1])


def config_payload(brand: BrandProfile | None, pillars: PillarConfig) -> bytes:
    """Brand without font paths, pillars without exclusions. Exclusions never leave the Mac."""
    brand_part = None
    if brand is not None:
        brand_part = brand.model_dump(mode="json", exclude={"fonts"})
    pillar_part = replace_exclusions(pillars).model_dump(mode="json")
    return json.dumps({"brand": brand_part, "pillars": pillar_part}).encode()


def replace_exclusions(pillars: PillarConfig) -> PillarConfig:
    return pillars.model_copy(update={"exclusions": Exclusions()})


def preview_jpeg(preview_path: str | None) -> bytes | None:
    image = load_preview(preview_path, PREVIEW_EDGE)
    if isinstance(image, SkipReason):
        return None
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=PREVIEW_QUALITY)
    return buffer.getvalue()


def vector_batch(uuids: list[str], vectors: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    np.savez(
        buffer,
        model=np.array(MODEL_ID),
        uuids=np.array(uuids),
        vectors=vectors.astype(np.float16),
    )
    return buffer.getvalue()


class Syncer:
    """One sync at a time, in a background thread. The Library tab polls `status`."""

    def __init__(
        self,
        web_url: str,
        token: Callable[[], str | None],
        data_dir: Path,
        index_path: Path,
        brand: Callable[[], BrandProfile | None],
        pillars: PillarConfig,
        transport: Transport = urllib_transport,
    ) -> None:
        self.web_url = validate_server_url(web_url)
        self._token = token
        self._data_dir = data_dir
        self._index_path = index_path
        self._brand = brand
        self._pillars = pillars
        self._transport = transport
        self._lock = threading.Lock()
        self._progress = SyncProgress()

    def progress(self) -> SyncProgress:
        with self._lock:
            return self._progress

    def _set(self, **changes: object) -> None:
        with self._lock:
            self._progress = replace(self._progress, **changes)  # type: ignore[arg-type]

    def status(self) -> dict[str, object]:
        return self._describe(self.progress())

    def _describe(self, progress: SyncProgress) -> dict[str, object]:
        return {
            "stage": progress.stage.value,
            "done": progress.done,
            "total": progress.total,
            "skipped": progress.skipped,
            "error": progress.error,
            "url": self.web_url,
        }

    def start(self) -> dict[str, object]:
        token = self._token()
        if token is None:
            raise SyncError(NOT_SIGNED_IN)
        with self._lock:
            if self._progress.stage in RUNNING:
                return self._describe(self._progress)
            self._progress = SyncProgress(stage=SyncStage.LIBRARY)
        thread = threading.Thread(target=self._run, args=(token,), name="loupe-sync", daemon=True)
        thread.start()
        return self.status()

    def _run(self, token: str) -> None:
        client = WebClient(self.web_url, token, self._transport)
        try:
            self.run_steps(client)
        except SyncError as error:
            self._set(stage=SyncStage.FAILED, error=str(error))
            return
        except Exception:
            logger.exception("sync failed")
            self._set(stage=SyncStage.FAILED, error="Sync stopped. Try again.")
            return
        self._set(stage=SyncStage.DONE)

    def run_steps(self, client: WebClient) -> None:
        self._set(stage=SyncStage.LIBRARY)
        items = self.send_index(client)
        logger.info("sync: index sent, %d eligible items", items)
        self._set(stage=SyncStage.SETTINGS)
        self.send_settings(client)
        self._set(stage=SyncStage.PREVIEWS, done=0, total=items, skipped=0)
        sent, skipped = self.send_previews(client)
        logger.info("sync: %d previews sent, %d skipped", sent, skipped)
        self._set(stage=SyncStage.VECTORS, done=0, total=items, skipped=0)
        vectors = self.send_vectors(client)
        logger.info("sync: %d vectors sent", vectors)

    def send_index(self, client: WebClient) -> int:
        with tempfile.TemporaryDirectory(prefix="loupe-sync-") as scratch:
            exported = Path(scratch) / "index.sqlite"
            items = export_index(self._index_path, exported)
            packed = Path(scratch) / "index.sqlite.gz"
            with exported.open("rb") as source, gzip.open(packed, "wb") as target:
                shutil.copyfileobj(source, target)
            client.call(
                "PUT",
                "/sync/v1/index",
                packed.read_bytes(),
                "application/gzip",
                UPLOAD_TIMEOUT_SECONDS,
            )
        return items

    def send_settings(self, client: WebClient) -> None:
        brand = self._brand()
        client.call(
            "PUT", "/sync/v1/config", config_payload(brand, self._pillars), "application/json"
        )
        if brand is not None:
            for role, font in (("display", brand.fonts.display), ("body", brand.fonts.body)):
                suffix = font.suffix.lower().removeprefix(".")
                client.call("PUT", f"/sync/v1/fonts/{role}.{suffix}", font.read_bytes())
        model = model_path(self._data_dir)
        if model.is_file():
            client.call("PUT", "/sync/v1/model", model.read_bytes(), timeout=UPLOAD_TIMEOUT_SECONDS)

    def _preview_paths(self, uuids: list[str]) -> dict[str, str | None]:
        connection = connect(self._index_path)
        try:
            marks = ",".join("?" * len(uuids))
            rows = connection.execute(
                f"SELECT uuid, preview_path FROM items WHERE uuid IN ({marks})",  # noqa: S608 placeholders only
                uuids,
            ).fetchall()
        finally:
            connection.close()
        return {row[0]: row[1] for row in rows}

    def send_previews(self, client: WebClient) -> tuple[int, int]:
        sent = skipped = 0

        def send_one(uuid: str, preview_path: str | None) -> bool:
            body = preview_jpeg(preview_path)
            if body is None:
                return False
            client.call("PUT", f"/sync/v1/previews/{uuid}", body, "image/jpeg")
            return True

        with ThreadPoolExecutor(PREVIEW_THREADS) as pool:
            for page in client.missing("previews"):
                paths = self._preview_paths(page)
                results = pool.map(send_one, page, [paths.get(uuid) for uuid in page])
                for ok in results:
                    sent += ok
                    skipped += not ok
                self._set(done=sent, skipped=skipped)
        return sent, skipped

    def _vector_rows(self, uuids: list[str]) -> dict[str, int]:
        connection = connect(self._index_path)
        try:
            marks = ",".join("?" * len(uuids))
            rows = connection.execute(
                "SELECT uuid, vector_row FROM embeddings "  # noqa: S608 placeholders only
                f"WHERE model = ? AND uuid IN ({marks})",
                (MODEL_ID, *uuids),
            ).fetchall()
        finally:
            connection.close()
        return {row[0]: int(row[1]) for row in rows}

    def send_vectors(self, client: WebClient) -> int:
        path = vector_path(self._data_dir, MODEL_ID)
        if not path.is_file():
            return 0
        store = VectorStore(path)
        sent = 0
        pending: list[str] = []
        for page in client.missing("vectors"):
            pending.extend(page)
            if len(pending) >= VECTOR_BATCH:
                sent += self._send_vector_batch(client, store, pending)
                pending = []
                self._set(done=sent)
        if pending:
            sent += self._send_vector_batch(client, store, pending)
            self._set(done=sent)
        return sent

    def _send_vector_batch(self, client: WebClient, store: VectorStore, uuids: list[str]) -> int:
        rows = self._vector_rows(uuids)
        ready = [uuid for uuid in uuids if uuid in rows]
        if not ready:
            return 0
        vectors = store.read([rows[uuid] for uuid in ready])
        client.call(
            "POST", "/sync/v1/vectors", vector_batch(ready, vectors), timeout=UPLOAD_TIMEOUT_SECONDS
        )
        return len(ready)
