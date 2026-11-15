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
if [[ ! -x "$RESOURCES/HighballHelper/highball" ]] ||
   ! cmp -s "$ROOT/macos/highball-steam.patch" "$RESOURCES/HighballHelper/highball-steam.patch"; then
  bash "$ROOT/macos/package_highball_helper.sh"
fi
cp "$ROOT/games.json" "$RESOURCES/games.json"
cp "$ROOT/spikes/highball/readiness.py" "$RESOURCES/readiness.py"
cp "$ROOT/spikes/highball/probe.py" "$RESOURCES/probe.py"
mkdir -p "$RESOURCES/Backend"
cp "$ROOT/macos/backend/"*.py "$RESOURCES/Backend/"
cp "$ROOT/download_support.py" "$ROOT/host_resolver_client.py" "$RESOURCES/Backend/"
python3 "$ROOT/tools/build_host_helper.py"
mkdir -p "$RESOURCES/Backend/HostResolver"
cp -R "$ROOT/build/host-resolver/HostResolver/." "$RESOURCES/Backend/HostResolver/"
cp "$ROOT/macos/backend/recipes.json" "$RESOURCES/Backend/recipes.json"
mkdir -p "$RESOURCES/Backend/HighballRecipes"
cp "$ROOT/macos/backend/HighballRecipes/"*.json "$RESOURCES/Backend/HighballRecipes/"

# The Needle save fix is build-specific. Keep its binary payload out of the
# source tree and stage it from the user's retained diagnostic backup only
# for this local app build. The recipe's hashes are the authority.
python3 - "$ROOT" "$RESOURCES/Backend" <<'PY'
import hashlib
import json
import shutil
import sys
from pathlib import Path

root, backend = map(Path, sys.argv[1:])
catalog = json.loads((backend / "recipes.json").read_text(encoding="utf-8"))
delta = catalog.get("deltas", {}).get("needle-in-a-haystack", {})
specs = list(delta.get("setupFiles", [])) + list(delta.get("managedPatches", []))
if not specs:
    raise SystemExit("Needle recipe has no local patch payload declarations.")
source_dir = Path.home() / "Library/Application Support/Game-Downloader/Diagnostics/Needle-20261026-194923"
source_by_name = {
    "NeedleHighballIO.dll": "NeedleHighballIO.dll",
    "Assembly-CSharp.fixed.dll": "Assembly-CSharp.fixed.dll",
}
for spec in specs:
    asset = Path(spec["assetPath"])
    filename = source_by_name.get(asset.name)
    if not filename or asset.parts[:2] != ("Patches", "needle"):
        raise SystemExit(f"Unexpected Needle patch asset path: {asset}")
    source = source_dir / filename
    if not source.is_file():
        raise SystemExit(f"Required local Needle patch asset is missing: {source}")
    expected = spec["sha256"] if "sha256" in spec else spec["patchedSha256"]
    actual = hashlib.sha256(source.read_bytes()).hexdigest()
    if actual != expected.lower():
        raise SystemExit(f"Needle patch asset hash mismatch for {source.name}: {actual}")
    target = backend / asset
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    print(f"Staged verified Needle patch asset: {asset} ({actual})")
PY

# Build the free, official RARLAB UnRAR extractor. Unlike unar, it follows
# encrypted RAR5 volume sets as a single archive when started from volume 1.
readonly UNRAR_VERSION="7.3.1"
readonly UNRAR_SOURCE_SHA256="634900842a3737d9cc15bbcc71d4c74cc713437e0bca296a573424fe5f2660ab"
unrar_build="$(mktemp -d "${TMPDIR:-/tmp}/game-installer-unrar.XXXXXX")"
curl -fL --retry 2 -o "$unrar_build/unrar-source.tar.gz" \
  "https://www.rarlab.com/rar/unrarsrc-${UNRAR_VERSION}.tar.gz"
printf '%s  %s\n' "$UNRAR_SOURCE_SHA256" "$unrar_build/unrar-source.tar.gz" | shasum -a 256 -c -
tar -xzf "$unrar_build/unrar-source.tar.gz" -C "$unrar_build"
make -C "$unrar_build/unrar" -j"$(sysctl -n hw.ncpu)"
mkdir -p "$RESOURCES/Backend/Tools"
install -m 755 "$unrar_build/unrar/unrar" "$RESOURCES/Backend/Tools/unrar"
install -m 644 "$unrar_build/unrar/license.txt" "$RESOURCES/Backend/Tools/unrar-license.txt"
install -m 644 "$unrar_build/unrar/acknow.txt" "$RESOURCES/Backend/Tools/unrar-acknowledgements.txt"
rm -rf "$unrar_build"

# Bundle the official universal 7-Zip console tool for RAR and 7z archives.
# The upstream macOS archive contains one arm64+x86_64 universal executable.
readonly SEVENZIP_VERSION="26.05"
readonly SEVENZIP_SHA256="12ef6830317743cfd3f0ab1724bde43fcc270d8b6d8564f8c12c2a2b7f993570"
sevenzip_build="$(mktemp -d "${TMPDIR:-/tmp}/game-installer-7zip.XXXXXX")"
curl -fL --retry 2 -o "$sevenzip_build/7zip-mac.tar.xz" \
  "https://downloads.sourceforge.net/project/sevenzip/7-Zip/${SEVENZIP_VERSION}/7z${SEVENZIP_VERSION//./}-mac.tar.xz"
printf '%s  %s\n' "$SEVENZIP_SHA256" "$sevenzip_build/7zip-mac.tar.xz" | shasum -a 256 -c -
tar -xJf "$sevenzip_build/7zip-mac.tar.xz" -C "$sevenzip_build" 7zz License.txt
lipo "$sevenzip_build/7zz" -verify_arch arm64 x86_64
install -m 755 "$sevenzip_build/7zz" "$RESOURCES/Backend/Tools/7zz"
install -m 644 "$sevenzip_build/License.txt" "$RESOURCES/Backend/Tools/7zip-license.txt"
rm -rf "$sevenzip_build"

swift build --package-path "$PROJECT" -c release --product "$PRODUCT"

binary="$PROJECT/.build/release/$PRODUCT"
resource_bundle="$PROJECT/.build/release/${PRODUCT}_${PRODUCT}.bundle"
if [[ ! -x "$binary" || ! -d "$resource_bundle" ]]; then
  echo "The Swift build did not produce the app executable and resource bundle." >&2
  exit 2
fi

helper_resource="$resource_bundle/Resources/HighballHelper"
if [[ -d "$helper_resource" ]]; then
  helper_binary="$helper_resource/highball"
  if [[ ! -x "$helper_binary" ]]; then
    echo "The bundled Highball helper is not executable." >&2
    exit 2
  fi
  "$helper_binary" steam launch --help >/dev/null
  "$helper_binary" steam game --help >/dev/null
  codesign --force --sign - "$helper_binary"
  codesign --verify --strict "$helper_binary"
  python3 - "$helper_binary" "$helper_resource/build-manifest.json" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

binary = Path(sys.argv[1])
manifest = {
    "schema_version": 1,
    "highball_version": "0.10.22",
    "source_commit": "daa2095b9fd4abad0b9db7c57cacbd4d49a9e06e",
    "steam_integration": "steam-path-v1",
    "source_patch_sha256": hashlib.sha256((binary.parent / "highball-steam.patch").read_bytes()).hexdigest(),
    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    "build": "SwiftPM release build from pinned upstream source plus highball-steam.patch",
    "signature": "ad-hoc; no Developer ID",
}
Path(sys.argv[2]).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
PY
fi

unrar_resource="$resource_bundle/Resources/Backend/Tools/unrar"
if [[ ! -x "$unrar_resource" ]]; then
  echo "The bundled UnRAR extraction helper is missing." >&2
  exit 2
fi
codesign --force --sign - "$unrar_resource"
codesign --verify --strict "$unrar_resource"
sevenzip_resource="$resource_bundle/Resources/Backend/Tools/7zz"
if [[ ! -x "$sevenzip_resource" ]]; then
  echo "The bundled 7-Zip extraction helper is missing." >&2
  exit 2
fi
codesign --force --sign - "$sevenzip_resource"
codesign --verify --strict "$sevenzip_resource"

# Sign nested helper binaries before the outer app; Chromium's headless shell
# and the frozen Python helper each include their own native libraries.
python3 - "$resource_bundle/Resources/Backend/HostResolver" <<'PY'
from pathlib import Path
import subprocess, sys
root = Path(sys.argv[1])
for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
    if path.is_file() and not path.is_symlink():
        with path.open("rb") as stream:
            magic = stream.read(4)
        if magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
            subprocess.run(["codesign", "--force", "--sign", "-", str(path)], check=True)
PY

mkdir -p "$HOME/Applications" "$(dirname "$DESTINATION")"
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
cp "$ROOT/assets/icons/game-installer.icns" "$stage/Contents/Resources/GameInstaller.icns"
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
    "CFBundleIconFile":"GameInstaller.icns",
    "CFBundlePackageType":"APPL",
    "CFBundleShortVersionString":os.environ.get("RELEASE_VERSION", "0.1.0"),
    "CFBundleVersion":"1",
    "LSMinimumSystemVersion":"14.0",
    "NSHighResolutionCapable":True,
}
with open(sys.argv[1],"wb") as f: plistlib.dump(info,f)
PY

codesign --force --sign - "$stage"
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
