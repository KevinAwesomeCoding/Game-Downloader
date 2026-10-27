"""Compatibility imports for the shared cross-platform download engine."""
from pathlib import Path
import sys

# Source checkout: worker.py executes with macos/backend as sys.path[0].
# Release builds copy the shared modules into the Backend resource directory.
if not (Path(__file__).parent / "download_support.py").is_file():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from download_support import (  # noqa: F401
    Downloader, DownloadForm, HTTPSRedirect, Stopped, atomic_json, canonical_source,
    checkpoint, download_host_name, drive_confirmation, fetch_with_backup, https_url, parts, sha256,
)
