#!/bin/bash
set -euo pipefail

readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly PROJECT="$ROOT/macos/GameDownloaderMac"
readonly RESOURCES="$PROJECT/Sources/GameDownloaderMac/Resources"
readonly PRODUCT="GameDownloaderMac"
readonly APP_DISPLAY_NAME="${APP_DISPLAY_NAME:-Game Installer Preview}"
readonly APP_BUNDLE_IDENTIFIER="${APP_BUNDLE_IDENTIFIER:-com.kevinawesomecoding.GameInstallerPreview}"
readonly DESTINATION="${APP_DESTINATION:-$HOME/Applications/$APP_DISPLAY_NAME.app}"

mkdir -p "$RESOURCES"
cp "$ROOT/games.json" "$RESOURCES/games.json"
cp "$ROOT/spikes/highball/readiness.py" "$RESOURCES/readiness.py"
cp "$ROOT/spikes/highball/probe.py" "$RESOURCES/probe.py"
mkdir -p "$RESOURCES/Backend"
cp "$ROOT/macos/backend/"*.py "$RESOURCES/Backend/"
cp "$ROOT/macos/backend/recipes.json" "$RESOURCES/Backend/recipes.json"

swift build --package-path "$PROJECT" -c release --product "$PRODUCT"

binary="$PROJECT/.build/release/$PRODUCT"
resource_bundle="$PROJECT/.build/release/${PRODUCT}_${PRODUCT}.bundle"
if [[ ! -x "$binary" || ! -d "$resource_bundle" ]]; then
  echo "The Swift build did not produce the app executable and resource bundle." >&2
  exit 2
fi

mkdir -p "$HOME/Applications"
stage="$(mktemp -d "$HOME/Applications/.game-installer-preview.XXXXXX")"
cleanup() {
python3 - "$stage" <<'PY'
from pathlib import Path
import shutil, sys
p=Path(sys.argv[1]).resolve()
apps=(Path.home()/"Applications").resolve()
if p.parent==apps and p.name.startswith(".game-installer-preview.") and p.exists():
    shutil.rmtree(p)
PY
}
trap cleanup EXIT
mkdir -p "$stage/Contents/MacOS" "$stage/Contents/Resources"
install -m 755 "$binary" "$stage/Contents/MacOS/$PRODUCT"
ditto "$resource_bundle" "$stage/Contents/Resources/${PRODUCT}_${PRODUCT}.bundle"

python3 - "$stage/Contents/Info.plist" <<'PY'
import plistlib,sys
import os
info={
    "CFBundleName":os.environ.get("APP_DISPLAY_NAME", "Game Installer Preview"),
    "CFBundleDisplayName":os.environ.get("APP_DISPLAY_NAME", "Game Installer Preview"),
    "CFBundleIdentifier":os.environ.get("APP_BUNDLE_IDENTIFIER", "com.kevinawesomecoding.GameInstallerPreview"),
    "CFBundleExecutable":"GameDownloaderMac",
    "CFBundlePackageType":"APPL",
    "CFBundleShortVersionString":"0.1.0",
    "CFBundleVersion":"1",
    "LSMinimumSystemVersion":"14.0",
    "NSHighResolutionCapable":True,
}
with open(sys.argv[1],"wb") as f: plistlib.dump(info,f)
PY

codesign --force --deep --sign - "$stage"
codesign --verify --deep --strict "$stage"

backup=""
if [[ -e "$DESTINATION" ]]; then
  backup="$HOME/Applications/.previous-game-installer-preview-$(date -u +%Y%m%dT%H%M%SZ).app"
  mv "$DESTINATION" "$backup"
  printf 'Previous preview preserved at %s\n' "$backup"
fi
if ! mv "$stage" "$DESTINATION"; then
  if [[ -n "$backup" ]]; then mv "$backup" "$DESTINATION"; fi
  echo "Could not install preview into $DESTINATION." >&2
  exit 1
fi
stage=""
printf 'Built Mac app at %s\n' "$DESTINATION"
