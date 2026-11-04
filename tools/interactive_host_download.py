#!/usr/bin/env python3
"""Open a game or host page for the user to start a download in a visible browser."""
import argparse
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_resolver_client import provider_for
from gofile_download import unique_path


def supported_page(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in (None, 443)
                and (parsed.hostname in ("steamrip.com", "www.steamrip.com")
                     or provider_for(url) in ("bzzhr", "fileditch")))
    except ValueError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="SteamRIP game page or BZZHR/FileDitch host page")
    parser.add_argument("--output", type=Path, default=Path("downloads"))
    parser.add_argument("--timeout", type=int, default=300, help="Seconds to wait for a download")
    args = parser.parse_args()
    if not supported_page(args.url):
        parser.error("Use an HTTPS SteamRIP, BZZHR, or FileDitch page.")
    if args.timeout <= 0:
        parser.error("Timeout must be positive.")
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(accept_downloads=True)
        downloads = []
        context.on("page", lambda page: page.on("download", lambda download: downloads.append(download)))
        page = context.new_page()
        try:
            try:
                page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:
                if "download" not in str(exc).lower() and "aborted" not in str(exc).lower():
                    raise
            print("In the browser, open the desired host link, complete any check, and start the download. Close all browser windows to cancel.", flush=True)
            deadline = time.monotonic() + args.timeout
            while not downloads and time.monotonic() < deadline:
                if not context.pages:
                    raise RuntimeError("Browser window closed before a download started.")
                context.pages[0].wait_for_timeout(250)
            if not downloads:
                raise TimeoutError("No download started before the timeout.")
            download = downloads[0]
            target = unique_path(args.output, download.suggested_filename)
            print(f"Saving {target.name}; keep the browser open until it finishes.", flush=True)
            download.save_as(str(target))
            print(f"Saved: {target}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
