#!/usr/bin/env python3
"""Check a single-file GoFile browser-to-HTTP handoff without saving the file.

Usage: venv/bin/python tools/gofile_probe.py https://gofile.io/d/SHARE_CODE
Session credentials stay in memory and are excluded from the printed report.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import http.cookiejar
import re
import time
from urllib.parse import urlsplit
from urllib.request import (
    HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener,
)

from playwright.sync_api import sync_playwright


SAMPLE_BYTES = 64 * 1024


def share_url(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme != "https" or parts.hostname not in {"gofile.io", "www.gofile.io"}
            or parts.username or parts.password or parts.port not in {None, 443}
            or not re.fullmatch(r"/d/[A-Za-z0-9]+/?", parts.path)):
        raise ValueError("Supply an HTTPS GoFile share link: https://gofile.io/d/CODE")
    return value


def download_url(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme != "https" or not (parts.hostname or "").endswith(".gofile.io")
            or parts.username or parts.password or parts.port not in {None, 443}
            or not parts.path.startswith("/download/web/")):
        raise ValueError("GoFile returned an unexpected download address.")
    return value


@dataclass(repr=False)
class ResolvedFile:
    name: str
    content_id: str
    size: int
    source: str
    url: str = field(repr=False)
    cookies: list = field(repr=False)
    user_agent: str = field(repr=False)


def resolve(source: str) -> ResolvedFile:
    source = share_url(source)
    code = urlsplit(source).path.rstrip("/").rsplit("/", 1)[1]
    last_error = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for attempt in range(1, 4):
                print(f"Resolving link: attempt {attempt}/3", flush=True)
                context = browser.new_context()
                try:
                    page = context.new_page()
                    payloads = []

                    def observe(response):
                        parts = urlsplit(response.url)
                        if (parts.hostname == "api.gofile.io"
                                and parts.path.rstrip("/") == f"/contents/{code}"):
                            try:
                                payloads.append(response.json())
                            except Exception:
                                pass

                    page.on("response", observe)
                    page.goto(source, wait_until="domcontentloaded", timeout=45_000)
                    deadline = time.monotonic() + 30
                    while not payloads and time.monotonic() < deadline:
                        page.wait_for_timeout(100)
                    if not payloads or payloads[-1].get("status") != "ok":
                        raise ValueError("The normal GoFile page did not provide accessible file information.")
                    data = payloads[-1]["data"]
                    if data.get("type") == "file":
                        item = data
                    else:
                        children = data.get("children", {})
                        children = list(children.values()) if isinstance(children, dict) else children
                        if len(children) != 1 or data.get("childrenCount", len(children)) != 1:
                            raise ValueError("This probe requires a share containing exactly one file.")
                        item = children[0]
                    if item.get("type") != "file" or not item.get("canAccess"):
                        raise ValueError("The share does not contain one accessible file.")
                    url = download_url(item["link"])
                    return ResolvedFile(
                        name=item["name"], content_id=item["id"], size=int(item["size"]),
                        source=source, url=url, cookies=context.cookies([url]),
                        user_agent=page.evaluate("navigator.userAgent"),
                    )
                except Exception as exc:
                    last_error = exc
                    # Errors can include request headers; keep diagnostics generic.
                    print(f"Resolution attempt {attempt} did not finish.", flush=True)
                finally:
                    context.close()
                if attempt < 3:
                    time.sleep(attempt)
        finally:
            browser.close()
    raise RuntimeError("Could not resolve a single accessible file after three attempts.") from last_error


class GoFileRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        download_url(newurl)
        return super().redirect_request(request, response, code, message, headers, newurl)


def opener_for(item: ResolvedFile):
    jar = http.cookiejar.CookieJar()
    for value in item.cookies:
        domain = value["domain"]
        expires = value.get("expires", -1)
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=value["name"], value=value["value"], port=None,
            port_specified=False, domain=domain, domain_specified=True,
            domain_initial_dot=domain.startswith("."), path=value["path"],
            path_specified=True, secure=value["secure"],
            expires=int(expires) if expires > 0 else None, discard=expires <= 0,
            comment=None, comment_url=None, rest={}, rfc2109=False,
        ))
    return build_opener(GoFileRedirect(), HTTPCookieProcessor(jar))


def sample(opener, item: ResolvedFile, start: int, modified=None):
    end = min(start + SAMPLE_BYTES, item.size) - 1
    headers = {"User-Agent": item.user_agent, "Referer": item.source,
               "Accept-Encoding": "identity", "Range": f"bytes={start}-{end}"}
    if modified:
        headers["If-Range"] = modified
    with opener.open(Request(item.url, headers=headers), timeout=20) as response:
        content_range = response.headers.get("Content-Range", "")
        expected_range = f"bytes {start}-{end}/{item.size}"
        if response.status != 206 or content_range != expected_range:
            raise ValueError("The server did not return the requested archive range.")
        chunk = response.read(end - start + 2)
        if len(chunk) != end - start + 1:
            raise ValueError("The sampled response length did not match its range.")
        if start == 0:
            if ("text/html" in response.headers.get("Content-Type", "").lower()
                    or chunk.lstrip().lower().startswith((b"<!doctype html", b"<html"))):
                raise ValueError("GoFile returned a web page instead of file bytes.")
        print(f"HTTP {response.status}: {content_range} ({len(chunk):,} bytes received)")
        return response.headers.get("Last-Modified")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", type=share_url)
    args = parser.parse_args()
    try:
        item = resolve(args.url)
        print(f"Browser closed. File: {item.name} ({item.size:,} bytes)")
        opener = opener_for(item)
        modified = sample(opener, item, 0)
        if item.size > SAMPLE_BYTES:
            sample(opener, item, SAMPLE_BYTES, modified)
        print("Handoff succeeded. Sample bytes were discarded; no file was saved.")
        return 0
    except Exception:
        print("Probe failed. No complete file was downloaded or saved.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
