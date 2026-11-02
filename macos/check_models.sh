#!/bin/bash
set -euo pipefail
readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly SOURCE="$ROOT/macos/GameDownloaderMac/Sources/GameDownloaderMac"
temporary="$(mktemp -d -t game-installer-model-checks)"
trap 'rm -rf "$temporary"' EXIT
swiftc -parse-as-library "$SOURCE/AppResources.swift" "$SOURCE/Game.swift" "$SOURCE/QueueModels.swift" \
  "$ROOT/macos/tests/ModelChecks.swift" -o "$temporary/model-checks"
"$temporary/model-checks" "$ROOT/games.json"
