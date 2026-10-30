#!/usr/bin/env python3
"""
Check game archive links by receiving a small sample of file data.

Download URLs are resolved using the installer's normal host handling and up
to 256 KiB is read then discarded; no archive is saved. GoFile and FileKeeper
links may start the project's browser helper. Source and thumbnail URLs are
checked for HTTP liveness only.

Usage:
    python tools/check_catalog_links.py
    python tools/check_catalog_links.py --games path/to/games.json
    python tools/check_catalog_links.py --verbose
    python tools/check_catalog_links.py --download-only
    python tools/check_catalog_links.py --timeout 15 --workers 8

Exit codes:
    0  - no download link failed
    1  - at least one download link failed or did not return file data
"""

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    sys.exit("requests is not installed. Run: pip install requests")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from download_support import Downloader
except ImportError as exc:
    sys.exit(f"Could not load the project's download checker: {exc}")

# --------------------------------------------------------------------------
# Colour helpers
# --------------------------------------------------------------------------

_USE_COLOUR = sys.stdout.isatty()


def _c(text, code):
    if not _USE_COLOUR:
        return text
    return f"\033[{code}m{text}\033[0m"


green  = lambda t: _c(t, "32")
red    = lambda t: _c(t, "31")
yellow = lambda t: _c(t, "33")
dim    = lambda t: _c(t, "2")
bold   = lambda t: _c(t, "1")


# --------------------------------------------------------------------------
# URL field definitions
# --------------------------------------------------------------------------

# Hard-failure fields (dead link = FAIL exit code)
DOWNLOAD_FIELDS = ("zipUrl", "zipBackupUrl", "fixZipUrl", "fixBackupUrl")

# Soft-failure fields (dead link = WARN only)
INFO_FIELDS = ("source", "thumbnail")

ALL_FIELDS = DOWNLOAD_FIELDS + INFO_FIELDS

# --------------------------------------------------------------------------
# Result dataclass
# --------------------------------------------------------------------------

@dataclass
class LinkResult:
    game_id:     str
    game_name:   str
    field:       str
    url:         str
    status_code: Optional[int] = None
    ok:          bool  = False
    skip:        bool  = False   # unsupported protocol
    warning:     bool  = False   # info field (soft failure)
    note:        str   = ""
    elapsed_ms:  float = 0.0


# --------------------------------------------------------------------------
# HTTP session
# --------------------------------------------------------------------------

def _make_session(timeout: int) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://",  adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
    })
    session._timeout = timeout
    return session


# --------------------------------------------------------------------------
# Core check
# --------------------------------------------------------------------------

def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def check_url(session: requests.Session, result: LinkResult) -> LinkResult:
    url     = result.url
    timeout = session._timeout

    scheme = urlparse(url).scheme.lower()
    if scheme not in {"http", "https"}:
        result.skip = True
        result.note = f"{scheme or 'unknown'} URL is not an HTTP download link"
        return result

    t0 = time.monotonic()
    try:
        if result.field.split("[")[0] in DOWNLOAD_FIELDS:
            # This resolves GoFile/FileKeeper through the same browser helper
            # the installer uses, follows redirects, and reads only a small
            # byte sample. No archive is saved to disk.
            downloader = Downloader()
            speed = downloader.measure_speed(url, timeout=timeout)
            probe = downloader.last_probe
            result.status_code = probe["status_code"]
            result.ok = probe["bytes_received"] > 0
            result.note = (
                f"download data received ({probe['bytes_received']:,} bytes; "
                f"{speed / 1_000_000:.2f} MB/s)"
            )
            result.elapsed_ms = (time.monotonic() - t0) * 1000
            return result

        # Informational source/thumbnail links are checked for liveness only.
        resp = session.head(url, timeout=timeout, allow_redirects=True)
        elapsed = (time.monotonic() - t0) * 1000

        # Some servers reject HEAD; fall back to a 1-byte range GET.
        if resp.status_code in (405, 403, 501):
            resp.close()
            resp = session.get(
                url, timeout=timeout, allow_redirects=True,
                headers={"Range": "bytes=0-0"},
                stream=True,
            )
            resp.close()
            elapsed = (time.monotonic() - t0) * 1000

        result.status_code = resp.status_code
        result.elapsed_ms  = elapsed
        result.ok = resp.status_code in (200, 206)
        if not result.ok:
            result.note = f"HTTP {resp.status_code}"
        resp.close()

    except requests.exceptions.SSLError as exc:
        result.note = f"SSL error: {exc}"
    except requests.exceptions.ConnectionError as exc:
        result.note = f"Connection error: {exc}"
    except requests.exceptions.Timeout:
        result.note = f"Timed out after {timeout}s"
    except Exception as exc:
        probe = getattr(locals().get("downloader"), "last_probe", {})
        result.status_code = probe.get("status_code", getattr(exc, "code", None))
        result.note = f"{type(exc).__name__}: {exc}"

    if result.status_code is None:
        result.elapsed_ms = (time.monotonic() - t0) * 1000

    return result


# --------------------------------------------------------------------------
# Report helpers
# --------------------------------------------------------------------------

def _pad(s: str, width: int) -> str:
    if len(s) > width:
        return s[: width - 1] + ">"
    return s.ljust(width)


def print_report(results: list, verbose: bool = False) -> None:
    W_NAME  = 28
    W_FIELD = 14
    W_CODE  = 6
    W_NOTE  = 40

    print("")
    print(
        bold(_pad("Game",   W_NAME))  + "  " +
        bold(_pad("Field",  W_FIELD)) + "  " +
        bold(_pad("Code",   W_CODE))  + "  " +
        bold(_pad("Note",   W_NOTE))
    )
    print("-" * (W_NAME + W_FIELD + W_CODE + W_NOTE + 8))

    for r in results:
        if r.skip:
            badge    = yellow("SKIP")
            code_str = "--"
            note     = r.note
        elif r.ok:
            if not verbose:
                continue
            badge    = green("OK  ")
            code_str = str(r.status_code)
            note     = r.note
        elif r.warning:
            badge    = yellow("WARN")
            code_str = str(r.status_code) if r.status_code else "--"
            note     = r.note or ""
        else:
            badge    = red("FAIL")
            code_str = str(r.status_code) if r.status_code else "--"
            note     = r.note or ""

        print(
            f"{badge} "
            f"{_pad(r.game_name, W_NAME)}  "
            f"{_pad(r.field,     W_FIELD)}  "
            f"{_pad(code_str,    W_CODE)}  "
            f"{_pad(note,        W_NOTE)}"
        )
        if not r.ok and not r.skip:
            print(f"     {dim(r.url)}")


def print_summary(results: list) -> None:
    ok   = sum(1 for r in results if r.ok)
    skip = sum(1 for r in results if r.skip)
    warn = sum(1 for r in results if r.warning and not r.ok)
    fail = sum(1 for r in results if not r.ok and not r.skip and not r.warning)

    print("")
    print(bold("Summary"))
    print(f"  {green('OK')}:   {ok}")
    print(f"  {yellow('SKIP')}: {skip}  (non-HTTP links)")
    print(f"  {yellow('WARN')}: {warn}  (info fields - not counted as failures)")
    print(f"  {red('FAIL')}: {fail}")
    print(f"  Total checked: {ok + skip + warn + fail}")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> int:
    global _USE_COLOUR

    parser = argparse.ArgumentParser(
        description="Check catalog download links by receiving a small file sample."
    )
    parser.add_argument("--games",        default=None, help="Path to games.json")
    parser.add_argument("--timeout",      type=int, default=20,
                        help="Request timeout in seconds (default: 20)")
    parser.add_argument("--workers",      type=int, default=6,
                        help="Concurrent workers (default: 6)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Also show passing (OK) links")
    parser.add_argument("--no-colour",    action="store_true",
                        help="Disable ANSI colour output")
    parser.add_argument("--download-only", action="store_true",
                        help="Only check zipUrl/zipBackupUrl/fixZipUrl/fixBackupUrl")
    args = parser.parse_args()

    if args.no_colour:
        _USE_COLOUR = False

    # Resolve games.json path
    if args.games:
        games_path = args.games
    else:
        here      = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(here)  # tools/ -> repo root
        games_path = os.path.join(repo_root, "games.json")

    if not os.path.isfile(games_path):
        print(f"ERROR: games.json not found at {games_path}", file=sys.stderr)
        return 1

    with open(games_path, encoding="utf-8") as fh:
        games = json.load(fh)

    print(bold(f"Loaded {len(games)} game(s) from {games_path}"))

    fields_to_check = DOWNLOAD_FIELDS if args.download_only else ALL_FIELDS

    work: list[LinkResult] = []
    for game in games:
        gid   = game.get("id",   "?")
        gname = game.get("name", gid)
        for f in fields_to_check:
            raw = game.get(f) or ""
            entries = raw if isinstance(raw, list) else re.split(r"[,\n]", raw) if isinstance(raw, str) else []
            urls = [str(item.get("url", "")).strip() if isinstance(item, dict) else str(item).strip()
                    for item in entries]
            # Steam protocol commands (for example steam://install/12345) are
            # app actions, not HTTP archive links, so do not include them.
            urls = [url for url in urls if url and urlparse(url).scheme.lower() != "steam"]
            for index, url in enumerate(urls, start=1):
                field_label = f"{f}[{index}]" if len(urls) > 1 else f
                work.append(LinkResult(
                    game_id=gid, game_name=gname,
                    field=field_label, url=url,
                    warning=(f in INFO_FIELDS),
                ))

    print(
        f"Checking {len(work)} URL(s) with {args.workers} worker(s)"
        f" (timeout={args.timeout}s)..."
    )

    session = _make_session(args.timeout)
    results: list[LinkResult] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(check_url, session, item): item for item in work}
        done = 0
        for future in concurrent.futures.as_completed(futures):
            r = future.result()
            results.append(r)
            done += 1
            badge = (
                green("v") if r.ok            else
                yellow("~") if r.skip          else
                yellow("!") if r.warning       else
                red("x")
            )
            print(
                f"\r  [{done:>4}/{len(work)}]  {badge}  "
                f"{r.game_name[:24]:<24} / {r.field:<14}     ",
                end="", flush=True,
            )

    print()

    results.sort(key=lambda r: (
        r.game_name.lower(),
        ALL_FIELDS.index(r.field) if r.field in ALL_FIELDS else 99,
    ))

    print_report(results, verbose=args.verbose)
    print_summary(results)

    hard_failures = [
        r for r in results
        if not r.ok and not r.skip and not r.warning
    ]
    return 1 if hard_failures else 0


if __name__ == "__main__":
    sys.exit(main())
