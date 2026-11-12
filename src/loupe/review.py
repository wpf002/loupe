"""M4 review: one contact sheet per pillar plus an index page, all under data/review/."""

import html
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from loupe.classify import Pick, count_picks, picks
from loupe.config import PillarConfig
from loupe.sheets import Section, Tile, page, relative_src, slug, thumbnail, write_sheet

CONFIDENT_SHOWN = 240
BORDERLINE_SHOWN = 120
COVER_TILES = 3


@dataclass(frozen=True)
class ReviewPages:
    index: Path
    pillars: dict[str, Path]


def _tiles(rows: list[Pick]) -> list[Tile]:
    return [Tile(row.uuid, row.preview_path, row.media_type, row.probability) for row in rows]


def _cover(paths: list[Path], review_dir: Path) -> str:
    images = "".join(f'<img src="{relative_src(p, review_dir)}" alt="">' for p in paths)
    return f'<div class="cover">{images}</div>'


def write_review(
    connection: sqlite3.Connection,
    pillars: tuple[str, ...],
    config: PillarConfig,
    review_dir: Path,
) -> ReviewPages:
    thumbs_dir = review_dir / "thumbs"
    pages: dict[str, Path] = {}
    cards: list[str] = []
    for pillar in pillars:
        confident_total = count_picks(connection, pillar, config.min_confidence, 1.01)
        borderline_total = count_picks(
            connection, pillar, config.review_floor, config.min_confidence
        )
        confident = picks(connection, pillar, config.min_confidence, 1.01, CONFIDENT_SHOWN)
        borderline = picks(
            connection, pillar, config.review_floor, config.min_confidence, BORDERLINE_SHOWN
        )
        path = review_dir / f"{slug(pillar)}.html"
        write_sheet(
            path,
            pillar,
            [
                Section("Confident", _tiles(confident), confident_total),
                Section("Borderline", _tiles(borderline), borderline_total),
            ],
            thumbs_dir,
            {"Confident": confident_total, "Borderline": borderline_total},
            home="index.html",
        )
        pages[pillar] = path
        covers = [t for t in (thumbnail(tile, thumbs_dir) for tile in _tiles(confident[:8])) if t]
        cards.append(
            f'<a class="pillar" href="{path.name}">{_cover(covers[:COVER_TILES], review_dir)}'
            f'<h3>{html.escape(pillar)}</h3><p class="num">{confident_total:,} confident · '
            f"{borderline_total:,} borderline</p></a>"
        )
    index = review_dir / "index.html"
    body = f'<div class="pillars">{"".join(cards)}</div>'
    index.write_text(page("Review", body, {"Pillars": len(pillars)}))
    return ReviewPages(index, pages)
