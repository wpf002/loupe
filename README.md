# Loupe

Sorts an Apple Photos library into content pillars using local embeddings, then
writes the results back as albums that sync to the owner's phone.

## What It Does

1. Reads the Photos library on this Mac.
2. Embeds every photo and video preview with a local vision model.
3. Learns the pillars from albums the owner already made.
4. Classifies the rest and builds a review page.
5. Adds confident picks to albums in a `Loupe` folder in Photos.
6. Searches the library by plain-text description.
7. Transcribes talking clips, cuts dead air, and renders captioned reels.
8. Writes hooks with Claude in the owner's voice.
9. Plans the week with Claude and picks assets from the library.
10. Renders carousel slides in the brand fonts and colours.

Photos are never edited or deleted. The only data sent off the machine is the owner's
text, transcripts and downsized previews, to the Claude API, from `loupe hooks` on.

## Requirements

- Apple Silicon Mac, logged into the macOS user that has the library.
- iCloud Photos finished syncing, with Optimize Mac Storage on.
- Python 3.12 and `uv`.
- Full Disk Access for the terminal app (System Settings > Privacy & Security).
- Automation permission for Photos, requested the first time albums are written.

## Setup

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
cp .env.example .env
uv run loupe --help
```

In the owner's macOS account, follow `docs/MOVE.md`. Set `LOUPE_OWNER_MACOS_USER`
in `.env`, then:

```bash
uv run loupe doctor
```

## Usage

```bash
uv run loupe scan --limit 5000
uv run loupe embed
uv run loupe train
uv run loupe classify
uv run loupe review
uv run loupe write-albums            # dry run
uv run loupe write-albums --apply
uv run loupe search "laughing on a rooftop at sunset"
uv run loupe brand --open            # needs config/brand.toml
uv run loupe transcribe              # needs `uv sync --extra transcribe`
uv run loupe cut                     # lists talking takes
uv run loupe cut UUID
uv run loupe hooks UUID              # needs ANTHROPIC_API_KEY in .env
uv run loupe reel UUID --hook 2 --length 30
uv run loupe week --open
uv run loupe carousel --day Wed
```

## Configuration

- `config/pillars.toml`: which albums count as pillars, confidence thresholds.
- `config/pillars.local.toml`: optional, gitignored, merged over `pillars.toml`.
- `[exclusions]` in the config: albums, people, date ranges and UUIDs Loupe never touches.
- `.env`: data directory, library path, owner's macOS account, Photos folder name.
- `docs/INTAKE.md`: what's still needed from the owner.

## Undo

Delete the `Loupe` folder in Photos. The albums go away. The photos stay.

## Development

```bash
uv run ruff check .
uv run pytest
```

## Status

`doctor` and M1 to M12 (scan through carousel) built, tested on synthetic fixtures only. Claude calls use `claude-opus-5-5` with server-side refusal fallbacks on. First `embed` run downloads the SigLIP2 weights (several GB) to the Hugging Face cache. Build order is in `CLAUDE.md`.
