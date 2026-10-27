#!/usr/bin/env python3
"""Download a single publicly shared GoFile item through its normal web flow.

This uses the GoFile page and its own Download control. It does not create or
manufacture Premium API direct links.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ATTEMPTS = 3
DOWNLOAD_TIMEOUT_MS = 90_000


def validate_link(link: str) -> str:
    parsed = urlparse(link.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (
        host == "gofile.io" or host.endswith(".gofile.io")
    ):
        raise ValueError("Enter a GoFile URL (gofile.io or a *.gofile.io host).")
    return link.strip()


def safe_filename(name: str) -> str:
    name = Path(name).name.replace("\x00", "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name or "gofile-download"


def unique_path(folder: Path, filename: str) -> Path:
    candidate = folder / safe_filename(filename)
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    number = 2
    while True:
        alternative = folder / f"{stem} ({number}){suffix}"
        if not alternative.exists():
            return alternative
        number += 1


def download_from_page(page, link: str, folder: Path) -> Path:
    # Some GoFile links are download endpoints and begin downloading on page
    # navigation; others show a page with a Download button.
    try:
        with page.expect_download(timeout=12_000) as pending:
            try:
                page.goto(link, wait_until="domcontentloaded", timeout=45_000)
            except Exception as exc:
                # Chromium reports navigation cancellation when a download
                # starts. The expect_download event below is authoritative.
                if "download" not in str(exc).lower() and "aborted" not in str(exc).lower():
                    raise
        return save_download(pending.value, folder)
    except PlaywrightTimeoutError:
        pass

    # Find the normal page's Download action. If a site changes its wording,
    # fail clearly instead of guessing at private endpoints.
    candidates = [
        page.get_by_role("button", name=re.compile(r"download", re.I)),
        page.get_by_role("link", name=re.compile(r"download", re.I)),
    ]
    for locator in candidates:
        try:
            if locator.count() and locator.first.is_visible():
                with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as pending:
                    locator.first.click(timeout=10_000)
                return save_download(pending.value, folder)
        except PlaywrightTimeoutError:
            raise RuntimeError("Clicked Download, but GoFile did not start a file download.")

    raise RuntimeError("No Download control appeared on the GoFile page.")


def save_download(download, folder: Path) -> Path:
    target = unique_path(folder, download.suggested_filename)
    print(f"Receiving: {target.name}", flush=True)
    download.save_as(str(target))
    return target


def main() -> int:
    try:
        link = validate_link(input("GoFile file link: "))
    except (EOFError, ValueError) as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2

    folder_text = input("Save folder [./downloads]: ").strip()
    folder = Path(folder_text or "downloads").expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)

    last_error: Exception | None = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for attempt in range(1, ATTEMPTS + 1):
                print(f"Attempt {attempt}/{ATTEMPTS}: opening GoFile…", flush=True)
                context = browser.new_context(accept_downloads=True)
                page = context.new_page()
                try:
                    path = download_from_page(page, link, folder)
                    print(f"Download complete: {path}")
                    return 0
                except Exception as exc:
                    last_error = exc
                    print(f"Attempt {attempt} failed: {exc}", file=sys.stderr, flush=True)
                finally:
                    context.close()
        finally:
            browser.close()

    print(f"Could not download after {ATTEMPTS} attempts: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
