# Set Up On Her Mac

Loupe runs on the owner's own Mac, in the macOS account that is already signed
into her iCloud. Her library and every file Loupe writes stay on that Mac. Loupe
refuses to open a Photos library in any account other than the one named in
`LOUPE_OWNER_MACOS_USER`.

## 1. Check Her Photos

On her Mac, in her account:

1. System Settings > Apple Account > iCloud > Photos: Sync This Mac on.
2. Photos > Settings > iCloud: Optimize Mac Storage on.
3. Wait until the bottom of the Library view no longer shows syncing progress.

Nothing about her Apple ID goes in the repo, `.env`, or a log.

## 2. Access From The Developer's Mac

Both Macs on the same network. The developer works on her Mac over SSH, so every
command runs as her user and the owner-account check passes.

On her Mac:

1. System Settings > General > Sharing > Remote Login: on.
2. Click the info button next to Remote Login. Allow access for her user only.
   Turn on "Allow full disk access for remote users".
3. Note the local hostname shown at the bottom of the Sharing pane, for example
   `amandas-macbook-air.local`.
4. In Terminal, run `whoami` and note the result.

On the developer's Mac (skip `ssh-keygen` if `~/.ssh/id_ed25519.pub` exists):

```bash
ssh-keygen -t ed25519
```

```bash
ssh-copy-id <her-user>@<her-hostname>.local
```

She types her macOS password once. Then check:

```bash
ssh <her-user>@<her-hostname>.local whoami
```

To run Claude Code there, add an SSH connection to `<her-user>@<her-hostname>.local`
in the Claude desktop app and start sessions on it, or run
`ssh <her-user>@<her-hostname>.local` and then `claude`. Steps 3 to 8 below can all
run over this connection.

`write-albums --apply` controls the Photos app, which needs her logged-in
desktop session and the Automation prompt. Run that one command at her Mac.

Remote Login accepts connections from the network. When setup is done, turn it
off, or remove the developer's key from her `~/.ssh/authorized_keys`.

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

Every row must say OK. Each failure prints its fix. Over SSH, a Full Disk Access
failure means the Remote Login full disk access toggle in step 2 is off.

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

Then, in order:

```bash
uv run loupe embed
uv run loupe train
uv run loupe classify
uv run loupe review
uv run loupe write-albums
```

`write-albums` without `--apply` is a dry run. Only run `--apply` at her Mac
after she has looked at the review pages:

```bash
uv run loupe write-albums --apply
```

## 9. Brand And Content

```bash
cp config/brand.example.toml config/brand.toml
```

Fill in her colours, font file paths and three to five past captions, then:

```bash
uv run loupe brand
uv run loupe transcribe
uv run loupe app --open
```

The app runs on http://127.0.0.1:8765 on her Mac only.
