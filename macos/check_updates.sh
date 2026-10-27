#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/game-installer-update-check.XXXXXX")"
trap 'rm -rf "$TEMP_DIR"' EXIT
swiftc "$ROOT/macos/GameDownloaderMac/Sources/GameDownloaderMac/Updater.swift" \
  "$ROOT/macos/tests/UpdateModelChecks.swift" -o "$TEMP_DIR/update-model-checks"
"$TEMP_DIR/update-model-checks"
