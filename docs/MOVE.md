# Move To The Owner's Account

Brings Loupe up in the second macOS account on this Mac, where her library lives.
Loupe refuses to open a Photos library in any account other than the one named in
`LOUPE_OWNER_MACOS_USER`.

## 1. Sign In And Sync

1. Log into her macOS account.
2. She signs into her Apple ID in System Settings and types the password and 2FA
   code herself. Nothing about her Apple ID goes in the repo, `.env`, or a log.
3. Open Photos. Settings > iCloud: iCloud Photos on, Optimize Mac Storage on.
4. Wait until the bottom of the Library view no longer shows syncing progress.

## 2. Permissions

System Settings > Privacy & Security > Full Disk Access: turn on the terminal app
you'll use. Quit and reopen it.

## 3. Install Tools

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

Open a new terminal so both are on `PATH`.

## 4. Clone And Install

```bash
mkdir -p ~/Documents/GitHub && cd ~/Documents/GitHub
```

```bash
git clone https://github.com/wpf002/loupe.git && cd loupe
```

```bash
uv sync
```

## 5. Configure

```bash
cp .env.example .env
```

```bash
whoami
```

Set `LOUPE_OWNER_MACOS_USER` in `.env` to the `whoami` output. Leave
`LOUPE_PHOTOS_LIBRARY` blank unless her library is not the system library.

Do not copy `config/pillars.local.toml` from the development account. It holds
stand-in pillars.

## 6. Doctor

```bash
uv run loupe doctor
```

Every row must say OK. Each failure prints its fix.

## 7. Fill Config

Copy the answers from `docs/INTAKE.md` into `config/pillars.toml`, then rerun
`uv run loupe doctor` to validate it.

## 8. Run Order

```bash
uv run loupe scan --limit 5000
```

Check the totals and album counts. Then the full run:

```bash
uv run loupe scan
```

Later milestones, once built:

```bash
uv run loupe embed
uv run loupe train
uv run loupe classify
uv run loupe review
uv run loupe write-albums
uv run loupe write-albums --apply
```

`write-albums` without `--apply` is a dry run. Only run `--apply` after she has
looked at the review pages.
