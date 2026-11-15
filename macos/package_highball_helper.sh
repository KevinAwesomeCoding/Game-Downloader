#!/bin/bash
set -euo pipefail

readonly VERSION="0.10.26"
readonly COMMIT="dbc36ebc67f75944fce0dd7994b060dcd68a67d1"
readonly REPOSITORY="https://github.com/gauthierpiarrette/highball.git"
readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly RESOURCES="$ROOT/macos/GameDownloaderMac/Sources/GameDownloaderMac/Resources"
readonly DESTINATION="$RESOURCES/HighballHelper"
readonly TEMPORARY_ROOT="${TMPDIR:-/tmp}"
readonly TEMPORARY="$(mktemp -d "$TEMPORARY_ROOT/game-downloader-helper-package.XXXXXX")"

cleanup() {
  python3 - "$TEMPORARY" "$TEMPORARY_ROOT" <<'PY'
from pathlib import Path
import shutil, sys
path = Path(sys.argv[1]).resolve()
temporary_root = Path(sys.argv[2]).resolve()
if path.parent == temporary_root and path.name.startswith("game-downloader-helper-package."):
    shutil.rmtree(path)
PY
}
trap cleanup EXIT

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "The Highball helper must be built on macOS." >&2
  exit 2
fi

source_repository="$REPOSITORY"
local_source="${HIGHBALL_SOURCE_DIR:-$ROOT/Highball}"
if git -C "$local_source" cat-file -e "$COMMIT^{commit}" 2>/dev/null; then
  source_repository="$local_source"
fi
git clone --quiet --no-checkout "$source_repository" "$TEMPORARY/source"
git -C "$TEMPORARY/source" checkout --quiet "$COMMIT"
actual_commit="$(git -C "$TEMPORARY/source" rev-parse HEAD)"
if [[ "$actual_commit" != "$COMMIT" ]]; then
  echo "Highball source commit mismatch: expected $COMMIT, got $actual_commit." >&2
  exit 2
fi

git -C "$TEMPORARY/source" apply "$ROOT/macos/highball-steam-0.10.26.patch"

swift build --package-path "$TEMPORARY/source" -c release --product highball
binary="$TEMPORARY/source/.build/release/highball"
if [[ ! -x "$binary" ]]; then
  echo "Swift build did not produce the Highball CLI." >&2
  exit 2
fi

mkdir -p "$DESTINATION"
install -m 755 "$binary" "$DESTINATION/highball"
install -m 644 "$TEMPORARY/source/LICENSE" "$DESTINATION/LICENSE.txt"
mkdir -p "$TEMPORARY/archive/Highball-$VERSION"
git -C "$TEMPORARY/source" archive "$COMMIT" | tar -xf - -C "$TEMPORARY/archive/Highball-$VERSION"
git -C "$TEMPORARY/archive/Highball-$VERSION" apply "$ROOT/macos/highball-steam-0.10.26.patch"
tar -czf "$DESTINATION/Highball-$VERSION-source.tar.gz" -C "$TEMPORARY/archive" "Highball-$VERSION"
install -m 644 "$ROOT/macos/highball-steam-0.10.26.patch" "$DESTINATION/highball-steam-0.10.26.patch"
cat > "$DESTINATION/SOURCE-README.txt" <<EOF
Highball CLI helper, version $VERSION

Upstream base source commit:
$COMMIT

Local integration patch: highball-steam-0.10.26.patch (Steam path resolution and CLI operation)
Repository: $REPOSITORY
Source archive: Highball-$VERSION-source.tar.gz
License: GPL-3.0; see LICENSE.txt

The executable is built from the source archive by this repository's
macos/package_highball_helper.sh and ad-hoc signed when the Mac app is built.
EOF

python3 - "$DESTINATION" "$COMMIT" "$VERSION" <<'PY'
from pathlib import Path
import hashlib
import json
import sys

directory = Path(sys.argv[1])
metadata = {
    "schema_version": 1,
    "highball_version": sys.argv[3],
    "source_commit": sys.argv[2],
    "steam_integration": "steam-path-v1",
    "source_patch_sha256": hashlib.sha256((directory / "highball-steam-0.10.26.patch").read_bytes()).hexdigest(),
    "binary_sha256": hashlib.sha256((directory / "highball").read_bytes()).hexdigest(),
    "build": "SwiftPM release build from pinned upstream source plus highball-steam-0.10.26.patch",
    "signature": "SwiftPM ad-hoc; preview build re-signs the bundled binary",
}
(directory / "build-manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
PY

printf 'Packaged Highball CLI %s from %s for the Mac app resources.\n' "$VERSION" "$COMMIT"
