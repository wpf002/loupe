import http.client
import json
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from loupe.index import connect, upsert_excluded, upsert_item
from loupe.models import ExclusionReason
from loupe.server import TOKEN_HEADER, App, HttpError, make_server, parse_range
from loupe.settings import Settings
from loupe.sorting import Sorter
from loupe.studio import Studio
from loupe.week import DAYS, PlannedPost, PlannedWeek, PostFormat, WeekPlan, save_week
from tests.fixtures import UUID_A, UUID_B, make_brand, make_item
from tests.test_sorting import CONFIG, FakeEncoder, RecordingWriter, _library, _wait

MONDAY = date(2026, 11, 16)


def _week(monday: date) -> PlannedWeek:
    posts = [
        PlannedPost(
            day=day,
            pillar="CC Travel",
            format=PostFormat.CAROUSEL,
            angle=f"Angle {number}",
            asset_query="suitcase",
            caption=f"Caption {number}",
            slides=["One", "Two"],
        )
        for number, day in enumerate(DAYS)
    ]
    return PlannedWeek(
        start=monday, plan=WeekPlan(posts=posts), take_uuids={}, assets={0: [UUID_A, UUID_B]}
    )


def _planner(connection, monday: date) -> PlannedWeek:
    planned = _week(monday)
    save_week(connection, planned)
    return planned


def _no_client():
    raise AssertionError("Claude is not called in these tests")


def _no_original(uuid: str) -> Path:
    raise AssertionError("the library is not opened in these tests")


@dataclass
class Running:
    app: App
    port: int

    def request(
        self, method: str, path: str, body: object = None, headers: dict | None = None
    ) -> tuple[int, dict, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        sent = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        payload = None if body is None else json.dumps(body).encode()
        connection.request(method, path, body=payload, headers=sent)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), data

    def post(self, path: str, body: object, **headers: str) -> tuple[int, dict, bytes]:
        sent = {"Content-Type": "application/json", TOKEN_HEADER: self.app.token, **headers}
        return self.request("POST", path, body, sent)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def running(tmp_path: Path) -> Iterator[Running]:
    settings = Settings(
        data_dir=tmp_path / "data",
        photos_library=None,
        album_folder="Loupe",
        owner_macos_user="nobody",
    )
    settings.data_dir.mkdir()
    preview = tmp_path / "preview.jpeg"
    Image.new("RGB", (64, 48), "#a8681a").save(preview)
    connection = connect(settings.index_path)
    upsert_item(connection, make_item(UUID_A, preview_path=str(preview)))
    upsert_excluded(connection, UUID_B, ExclusionReason.PERSON)
    connection.commit()
    connection.close()
    studio = Studio(settings, make_brand(tmp_path), _no_client, _planner, _no_original)
    app = App(studio)
    port = _free_port()
    server = make_server(app, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield Running(app, port)
    server.shutdown()
    server.server_close()


def test_index_page_carries_token_and_security_headers(running: Running) -> None:
    status, headers, body = running.request("GET", "/")
    assert status == 200
    assert running.app.token in body.decode()
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_wrong_host_is_refused(running: Running) -> None:
    status, _, _ = running.request("GET", "/api/state", headers={"Host": "evil.test:8765"})
    assert status == 403


def test_post_needs_token(running: Running) -> None:
    status, _, _ = running.post("/api/week", {}, **{TOKEN_HEADER: "wrong"})
    assert status == 403


def test_post_needs_json(running: Running) -> None:
    status, _, _ = running.post("/api/week", {}, **{"Content-Type": "text/plain"})
    assert status == 415


def test_bad_uuid_is_rejected(running: Running) -> None:
    status, _, _ = running.request("GET", "/api/takes/not-a-uuid/hooks")
    assert status == 400


def test_render_path_cannot_escape(running: Running) -> None:
    status, _, _ = running.request("GET", "/media/render/..%2F..%2Findex.sqlite")
    assert status == 404


def test_static_files_are_whitelisted(running: Running) -> None:
    assert running.request("GET", "/static/app.js")[0] == 200
    assert running.request("GET", "/static/server.py")[0] == 404


def test_thumbnail_only_for_eligible_items(running: Running) -> None:
    status, headers, _ = running.request("GET", f"/media/thumb/{UUID_A}")
    assert status == 200
    assert headers["Content-Type"] == "image/jpeg"
    assert running.request("GET", f"/media/thumb/{UUID_B}")[0] == 404


def test_state_and_brand(running: Running) -> None:
    status, _, body = running.request("GET", "/api/state")
    assert status == 200
    assert json.loads(body) == {
        "brand": "Test Studio",
        "takes": 0,
        "week": None,
        "busy": False,
        "library": False,
    }
    brand = json.loads(running.request("GET", "/api/brand")[2])
    assert brand["colors"]["accent"] == "#A8681A"
    assert brand["voice"]["comment_word"] == "GUIDE"


def test_plan_week_then_read_it_back_without_excluded_assets(running: Running) -> None:
    status, _, body = running.post("/api/week", {"start": MONDAY.isoformat()})
    assert status == 200
    week = json.loads(body)["week"]
    assert week["start"] == MONDAY.isoformat()
    assert week["posts"][0]["assets"] == [{"uuid": UUID_A, "media_type": "photo"}]
    again = json.loads(running.request("GET", "/api/week")[2])
    assert again["week"]["posts"][0]["caption"] == "Caption 0"


def test_plan_rejects_non_monday(running: Running) -> None:
    status, _, body = running.post("/api/week", {"start": "2026-11-17"})
    assert status == 400
    assert "Monday" in json.loads(body)["error"]


def test_busy_lock_returns_conflict(running: Running) -> None:
    running.app.busy.acquire()
    try:
        status, _, _ = running.post("/api/week", {})
    finally:
        running.app.busy.release()
    assert status == 409


def test_everything_is_empty_before_a_week(running: Running) -> None:
    body = json.loads(running.request("GET", "/api/everything")[2])
    assert body == {"start": None, "carousels": [], "stories": [], "email": None}


def test_parse_range() -> None:
    assert parse_range(None, 100) is None
    assert parse_range("bytes=0-9", 100) == (0, 9)
    assert parse_range("bytes=90-", 100) == (90, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)
    assert parse_range("bytes=0-500", 100) == (0, 99)
    with pytest.raises(HttpError):
        parse_range("bytes=200-300", 100)


def test_range_request_returns_partial_content(running: Running) -> None:
    status, headers, body = running.request("GET", "/static/app.js", headers={"Range": "bytes=0-9"})
    assert status == 206
    assert len(body) == 10
    assert headers["Content-Range"].startswith("bytes 0-9/")


@pytest.fixture
def sorting(tmp_path: Path) -> Iterator[Running]:
    settings = Settings(
        data_dir=tmp_path / "data",
        photos_library=None,
        album_folder="Loupe",
        owner_macos_user="nobody",
    )
    settings.data_dir.mkdir()
    index_path = _library(tmp_path)
    sorter = Sorter(settings.data_dir, index_path, CONFIG, FakeEncoder, RecordingWriter)
    studio = Studio(settings, make_brand(tmp_path), _no_client, _planner, _no_original)
    app = App(studio, sorter=sorter)
    port = _free_port()
    server = make_server(app, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Running(app, port)
    server.shutdown()
    server.server_close()


def _json(response: tuple[int, dict, bytes]) -> tuple[int, dict]:
    status, _, data = response
    return status, json.loads(data)


def test_library_routes_are_absent_without_a_sorter(running: Running) -> None:
    assert _json(running.request("GET", "/api/state"))[1]["library"] is False
    assert running.request("GET", "/api/library")[0] == 404
    assert running.post("/api/library/sort", {})[0] == 404


def test_sort_then_search_over_http(sorting: Running) -> None:
    assert _json(sorting.request("GET", "/api/state"))[1]["library"] is True
    status, body = _json(sorting.request("GET", "/api/library"))
    assert (status, body["sorted"], len(body["pillars"])) == (200, False, 2)
    assert sorting.request("GET", "/api/search?q=red")[0] == 400
    status, body = _json(sorting.post("/api/library/sort", {}))
    assert status == 200
    _wait(sorting.app.sorter)
    status, body = _json(sorting.request("GET", "/api/search?q=red&media=photo&pillar=CC+Travel"))
    assert status == 200
    assert body["hits"] and set(body["hits"][0]) == {"uuid", "media_type"}


def test_library_jobs_need_the_token(sorting: Running) -> None:
    assert sorting.post("/api/library/sort", {}, **{TOKEN_HEADER: "wrong"})[0] == 403
    assert sorting.post("/api/library/albums", {})[0] == 400


def test_search_needs_query_text(sorting: Running) -> None:
    assert sorting.request("GET", "/api/search?q=%20")[0] == 400
    assert sorting.request("GET", "/api/search?q=" + "a" * 201)[0] == 400
