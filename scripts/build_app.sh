#!/bin/bash
# Builds build/Loupe.app for this Mac: compiles the Swift launcher, writes
# launch.json pointing at this checkout's .venv, ad-hoc signs the bundle.
# A Developer ID build with embedded Python is needed to give it to other Macs.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
python="$root/.venv/bin/python"
app="$root/build/Loupe.app"

server_url="${LOUPE_SERVER_URL:-}"
if [[ -z "$server_url" && -f "$root/.env" ]]; then
  server_url="$(grep -E '^LOUPE_SERVER_URL=' "$root/.env" | tail -1 | cut -d= -f2- || true)"
fi
if [[ -z "$server_url" ]]; then
  echo "Set LOUPE_SERVER_URL in .env first." >&2
  exit 1
fi
if [[ ! -x "$python" ]]; then
  echo "Run 'uv sync' first." >&2
  exit 1
fi
if ! "$python" -c "import anthropic" 2>/dev/null; then
  echo "Run 'uv sync --extra secondpass' first. The app needs it for Claude calls." >&2
  exit 1
fi

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$root/macapp/Info.plist" "$app/Contents/Info.plist"
swiftc -O -o "$app/Contents/MacOS/Loupe" "$root/macapp/Loupe.swift"
"$python" - "$app/Contents/Resources/launch.json" "$python" "$root" "$server_url" <<'PY'
import json, sys
target, python, project, server_url = sys.argv[1:]
with open(target, "w") as handle:
    json.dump({"python": python, "project": project, "serverURL": server_url}, handle)
PY
codesign --force --sign - "$app"
echo "Built $app"
