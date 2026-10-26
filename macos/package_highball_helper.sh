#!/bin/bash
set -euo pipefail

readonly VERSION="0.10.22"
readonly COMMIT="daa2095b9fd4abad0b9db7c57cacbd4d49a9e06e"
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

git clone --quiet --filter=blob:none --no-checkout "$REPOSITORY" "$TEMPORARY/source"
git -C "$TEMPORARY/source" checkout --quiet "$COMMIT"
actual_commit="$(git -C "$TEMPORARY/source" rev-parse HEAD)"
if [[ "$actual_commit" != "$COMMIT" ]]; then
  echo "Highball source commit mismatch: expected $COMMIT, got $actual_commit." >&2
  exit 2
fi

swift build --package-path "$TEMPORARY/source" -c release --product highball
binary="$TEMPORARY/source/.build/release/highball"
if [[ ! -x "$binary" ]]; then
  echo "Swift build did not produce the Highball CLI." >&2
  exit 2
fi

mkdir -p "$DESTINATION"
install -m 755 "$binary" "$DESTINATION/highball"
install -m 644 "$TEMPORARY/source/LICENSE" "$DESTINATION/LICENSE.txt"
git -C "$TEMPORARY/source" archive --format=tar.gz --prefix="Highball-$VERSION/" "$COMMIT" > "$DESTINATION/Highball-$VERSION-source.tar.gz"
cat > "$DESTINATION/SOURCE-README.txt" <<EOF
Highball CLI helper, version $VERSION

Unmodified upstream source commit:
$COMMIT

Repository: $REPOSITORY
Source archive: Highball-$VERSION-source.tar.gz
License: GPL-3.0; see LICENSE.txt

The executable is built from the source archive by this repository's
macos/package_highball_helper.sh and ad-hoc signed when the Mac app is built.
EOF

printf 'Packaged Highball CLI %s from %s for the Mac app resources.\n' "$VERSION" "$COMMIT"
