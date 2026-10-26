#!/bin/bash
set -euo pipefail

# Local developer build only. This does not produce a distributable,
# Developer ID signed/notarized helper.
readonly VERSION="0.10.22"
readonly COMMIT="daa2095b9fd4abad0b9db7c57cacbd4d49a9e06e"
readonly REPOSITORY="https://github.com/gauthierpiarrette/highball.git"
readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly APP="/Applications/Highball.app"
readonly SUPPORT="$HOME/Library/Application Support/Game-Downloader/HighballHelper"
readonly DESTINATION="$SUPPORT/$VERSION"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This local helper installer supports macOS only." >&2
  exit 2
fi
if [[ ! -d "$APP" ]]; then
  echo "Highball.app was not found at $APP." >&2
  exit 2
fi
if [[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist")" != "$VERSION" ]]; then
  echo "Installed Highball version does not match pinned helper $VERSION." >&2
  exit 2
fi

temporary="$(mktemp -d "${TMPDIR:-/tmp}/game-downloader-highball.XXXXXX")"
stage=""
cleanup() {
  python3 - "$temporary" "$stage" "$SUPPORT" <<'PY'
from pathlib import Path
import shutil
import sys

tmp = Path(sys.argv[1]).resolve()
support = Path(sys.argv[3]).resolve()
for raw in sys.argv[1:3]:
    if not raw:
        continue
    path = Path(raw).resolve()
    if path == tmp or (path.parent == support and path.name.startswith(".staging-")):
        if path.exists():
            shutil.rmtree(path)
PY
}
trap cleanup EXIT

git clone --quiet --no-checkout "$REPOSITORY" "$temporary/source"
git -C "$temporary/source" checkout --quiet "$COMMIT"
actual_commit="$(git -C "$temporary/source" rev-parse HEAD)"
if [[ "$actual_commit" != "$COMMIT" ]]; then
  echo "Source commit mismatch: expected $COMMIT, got $actual_commit." >&2
  exit 2
fi

swift build --package-path "$temporary/source" -c release --product highball
binary="$temporary/source/.build/release/highball"
if [[ ! -x "$binary" ]]; then
  echo "Swift build did not produce an executable CLI." >&2
  exit 2
fi

mkdir -p "$SUPPORT"
stage="$(mktemp -d "$SUPPORT/.staging-$VERSION.XXXXXX")"
install -m 755 "$binary" "$stage/highball"
codesign --force --sign - "$stage/highball"
codesign --verify --strict "$stage/highball"

python3 - "$stage/highball" "$stage/build-manifest.json" "$COMMIT" "$VERSION" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

binary = Path(sys.argv[1])
metadata = {
    "schema_version": 1,
    "highball_version": sys.argv[4],
    "source_commit": sys.argv[3],
    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    "build": "local SwiftPM release build",
    "signature": "ad-hoc; no Developer ID",
}
Path(sys.argv[2]).write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
PY

# Verify the staged binary and manifest before replacing an existing install.
python3 -B "$ROOT/spikes/highball/probe.py" \
  --app "$APP" --helper "$stage/highball" --manifest "$stage/build-manifest.json" >/dev/null

backup=""
if [[ -e "$DESTINATION" ]]; then
  backup="$SUPPORT/.previous-$VERSION-$(date -u +%Y%m%dT%H%M%SZ)"
  mv "$DESTINATION" "$backup"
fi
if ! mv "$stage" "$DESTINATION"; then
  if [[ -n "$backup" ]]; then mv "$backup" "$DESTINATION"; fi
  echo "Could not install helper into $DESTINATION." >&2
  exit 1
fi
stage=""
printf 'Installed Highball CLI %s at %s\n' "$VERSION" "$DESTINATION/highball"
if [[ -n "$backup" ]]; then
  printf 'Previous helper preserved at %s\n' "$backup"
fi
