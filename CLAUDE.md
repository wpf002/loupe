# Loupe

Sorts one person's Apple Photos library (about 200k photos and videos) into her
content pillars so she can grab assets for posts from her phone. Runs entirely on
this Mac, inside the macOS user account that is signed into her iCloud.

Her existing hand-made albums (names starting with "CC ", plus the extras in
`config/pillars.toml`) are the labeled examples. Everything else gets classified
against them.

## Hard Rules

1. The Photos library is read-only. Never delete, edit, move, hide, or re-title a
   photo or video. Never modify or remove an album the owner made.
2. The only write to Photos is adding items to albums inside the folder named by
   `LOUPE_ALBUM_FOLDER`. Loupe creates that folder and owns everything in it.
3. `write-albums` is a dry run unless `--apply` is passed. The dry run prints
   per-album counts.
4. Nothing under `data/` is ever committed. No photo, preview, embedding, face
   data, location, or transcript leaves this machine.
5. The Claude second pass is off by default, sits behind an explicit flag, and
   sends downsized previews only. Do not build it until asked.
6. No Apple ID, password, or token goes in the repo, the config, or a log line.
7. Every stage is keyed by photo UUID and is safe to re-run. A crash at item
   150,000 resumes at 150,000.
8. Skip hidden and recently deleted items. Screenshots, screen recordings and
   bursts are indexed and flagged but kept out of pillar albums.
9. Log counts and UUIDs. Never log file contents, faces, or GPS coordinates.
10. Loupe opens a Photos library only in the macOS account named by
    `LOUPE_OWNER_MACOS_USER`. Never weaken this check. The developer's own
    library is never read, scanned, or used as a stand-in.
11. Items matching `[exclusions]` in the config, and hidden items, are stored as
    UUID plus reason only. Every stage after scan reads its work list from
    `index.eligible_uuids()` and nothing else.

## Stack

- Python 3.12, managed with `uv`. Lint with `ruff`, test with `pytest`.
- `osxphotos` to read the library. `photoscript` (ships with it) to write albums.
- `open_clip` with a SigLIP checkpoint on the `mps` device for embeddings.
- `scikit-learn` one-vs-rest logistic regression on the embeddings.
- SQLite for the index. Float16 `.npy` memmap for vectors, row order tied to a
  UUID table.
- `typer` CLI, entry point `loupe`.
- `imageio-ffmpeg` supplies the ffmpeg binary. Do not assume Homebrew works in
  this user account.

Check the current osxphotos and open_clip docs before using an API. Do not
trust recalled signatures.

## Known Constraint

The library syncs with Optimize Mac Storage, so most originals are in iCloud.
Work from the local preview derivatives. For videos that are not downloaded,
embed the poster frame only. Frame sampling and transcription apply to videos
whose originals are local. Do not bulk-download originals without asking.

## Milestones

Build one at a time. Stop after each and show the output before starting the next.
Every command takes `--limit N` so it can be tested on a small slice.

- **Done: `loupe doctor`**, exclusions, local config override.
- **M1 `loupe scan`**: read the library into SQLite. Per item: uuid, date, media
  type, duration, album names, Apple labels, Apple quality score, preview path,
  flags (hidden, screenshot, burst, original is local). Print item totals and a
  count per pillar album.
- **M2 `loupe embed`**: embed every preview in batches on MPS. Resumable. Report
  throughput and anything skipped with the reason.
- **M3 `loupe train`**: fit the classifier on pillar-album members, with a held-out
  split. Print precision and recall per pillar. Report pillars that fall under
  `min_examples_per_pillar`.
- **M4 `loupe classify` and `loupe review`**: score the unlabeled items. Write a
  local HTML contact sheet per pillar showing confident picks and borderline
  ones. An item may land in more than one pillar.
- **M5 `loupe write-albums`**: create albums in the Loupe folder, one per pillar,
  and add confident picks in batches. Dry run by default.
- **M6 `loupe search "text"`**: text query against the embeddings, optional pillar
  and media-type filters, returns UUIDs and opens a contact sheet.

## Later, Not Now

- MCP server exposing `search_media(query, pillar, media_type)`.
- Transcription of talking clips.
- Corrections loop: items the owner removes from a Loupe album become negatives.

## Config

`config/pillars.toml` holds the owner's real config. `config/pillars.local.toml`
(gitignored) is merged over it when present: top-level keys replace, tables such
as `[exclusions]` merge key by key. Fill-in sheet: `docs/INTAKE.md`. Setup in the
owner's account: `docs/MOVE.md`. First command there: `loupe doctor`.

## Conventions

- Type hints everywhere. Small pure functions around a thin CLI.
- Parameterized SQL only.
- Validate every path and album name that comes from config before use.
- Tests use synthetic fixtures. Never put a real photo or real UUID in `tests/`.
