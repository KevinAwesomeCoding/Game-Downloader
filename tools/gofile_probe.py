#!/usr/bin/env python3
"""Verify a GoFile/FileKeeper session and resume using two small byte ranges."""
from pathlib import Path
import argparse
import http.cookiejar
import re
import sys
from urllib.request import Request, build_opener, HTTPCookieProcessor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from download_support import HostedRedirect, add_session_cookies
from host_resolver_client import provider_for, resolve_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--helper", type=Path, help="Use this packaged HostResolver executable")
    args = parser.parse_args()
    try:
        item = resolve_source(args.url, status=lambda message: print(message, flush=True),
                              command=[str(args.helper.resolve())] if args.helper else None)
        print(f"Browser closed. File: {item['name']}", flush=True)
        jar = http.cookiejar.CookieJar()
        add_session_cookies(jar, item["cookies"])
        opener = build_opener(HostedRedirect(provider_for(args.url)), HTTPCookieProcessor(jar))
        total = item.get("size")
        modified = None
        for start in (0, 65536):
            if total is not None and start >= total:
                break
            end = min(start + 65536, total) - 1 if total else start + 65535
            headers = {"Range": f"bytes={start}-{end}", "User-Agent": item["user_agent"],
                       "Referer": args.url, "Accept-Encoding": "identity"}
            if modified:
                headers["If-Range"] = modified
            with opener.open(Request(item["url"], headers=headers), timeout=20) as response:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                if response.status != 206 or not match or int(match[1]) != start:
                    raise ValueError("The host did not provide the requested file range.")
                if total is not None and total != int(match[3]):
                    raise ValueError("The file size changed between requests.")
                total = int(match[3])
                expected = min(end, total - 1) - start + 1
                if int(match[2]) != start + expected - 1:
                    raise ValueError("The host returned an inconsistent file range.")
                chunk = response.read(expected + 1)
                if len(chunk) != expected or "text/html" in response.headers.get("Content-Type", "").lower():
                    raise ValueError("The host returned an incomplete range or web page.")
                modified = response.headers.get("Last-Modified")
                print(f"HTTP 206: bytes {start}-{start + expected - 1}/{total}", flush=True)
        print("Handoff and range checks passed. Sample bytes discarded; no archive saved.")
        return 0
    except Exception as exc:
        print(f"Probe failed: {type(exc).__name__}. No complete archive was saved.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
