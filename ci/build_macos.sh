#!/bin/bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/Library/Frameworks/Python.framework/Versions/3.13/bin:$PATH"
export HIGHBALL_SOURCE_DIR="$HOME/Game-Downloader/Highball"
python3 -m venv "$CI_PROJECT_DIR/.ci-macos-venv"
source "$CI_PROJECT_DIR/.ci-macos-venv/bin/activate"
python3 -m pip install 'requests>=2.28'
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s macos/backend/tests -v
bash macos/package_highball_helper.sh
export HIGHBALL_STEAM_TEST_CLI="$CI_PROJECT_DIR/macos/GameDownloaderMac/Sources/GameDownloaderMac/Resources/HighballHelper/highball"
python3 -B -m unittest discover -s macos/backend/tests -p 'test_steam_integration.py' -v
python3 -B -m unittest discover -s spikes/highball -p 'test_*.py' -v
export APP_DISPLAY_NAME='Game Installer'
export APP_BUNDLE_IDENTIFIER='com.kevinawesomecoding.GameInstaller'
export RELEASE_VERSION="$RELEASE_TAG"
export APP_DESTINATION="$CI_PROJECT_DIR/dist/Game Installer.app"
mkdir -p dist
bash macos/build_preview.sh
ditto -c -k --sequesterRsrc --keepParent "$APP_DESTINATION" "$CI_PROJECT_DIR/dist/GameInstaller-macOS-arm64.zip"
bash macos/check_updates.sh
