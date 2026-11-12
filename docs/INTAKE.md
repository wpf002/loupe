# Intake

What's still needed from the library owner before the first full run. Fill in the
blanks with her answers, then copy them into `config/pillars.toml`.

## Consent

- [ ] Agreed to Loupe reading her Photos library on this Mac: ________
- [ ] Agreed to Loupe creating albums inside a `Loupe` folder in her Photos: ________
- [ ] Date: ________

## Confirmed Pillar Albums

Existing albums that hold good examples of each pillar. Each needs 30+ items.

| Pillar | Album Name (exact, as in Photos) | Approx. Items |
| --- | --- | --- |
| | | |
| | | |
| | | |
| | | |
| | | |

## Pillars With No Album Yet

| Pillar | What Belongs In It | Who Builds The Seed Album |
| --- | --- | --- |
| | | |
| | | |

## Albums To Ignore

Albums that start with `CC ` but should not be used as pillars.

- ________
- ________

## Exclusions

Never embedded, classified, searched, reviewed or written to an album.

Albums:

- ________
- ________

People (exactly as named in Photos; unnamed faces can't be matched):

- ________
- ________

Date ranges (inclusive):

| Start (YYYY-MM-DD) | End (YYYY-MM-DD) | Note |
| --- | --- | --- |
| | | |
| | | |

Specific items (Photos UUIDs):

- ________

## Course Videos

- Where they live now: ________
- Where they should live after this: ________
- In her Photos library: yes / no
- Approx. count and total size: ________

## Copy Into `config/pillars.toml`

```toml
label_album_prefixes = ["CC "]
include_albums = []        # Confirmed Pillar Albums without the prefix
exclude_albums = []        # Albums To Ignore

[exclusions]
albums = []
persons = []
date_ranges = []           # { start = YYYY-MM-DD, end = YYYY-MM-DD }
uuids = []
```
