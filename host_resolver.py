"""Private JSON-lines browser helper. Credentials go only through its parent pipe."""
import asyncio
import json
import os
import re
import sys
import threading
from urllib.parse import urlsplit

from host_resolver_client import provider_for, validate_result

if getattr(sys, "frozen", False):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"


class ResolutionError(Exception):
    pass


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)


def single_file(data):
    if data.get("type") == "file":
        item = data
    else:
        children = data.get("children", {})
        children = list(children.values()) if isinstance(children, dict) else children
        if (not isinstance(children, list) or len(children) != 1
                or data.get("childrenCount", len(children)) != 1):
            raise ResolutionError("The GoFile share must contain exactly one file.")
        item = children[0]
    if item.get("type") != "file" or not item.get("canAccess"):
        raise ResolutionError("The GoFile file is unavailable or requires access.")
    return item


async def gofile(context, source):
    parsed = urlsplit(source)
    match = re.fullmatch(r"/d/([A-Za-z0-9]+)/?", parsed.path)
    if not match:
        return await download_page(context, source, "gofile")
    code = match[1]
    page = await context.new_page()
    ready = asyncio.Event()
    payloads = []

    async def observed(response):
        parts = urlsplit(response.url)
        if parts.hostname == "api.gofile.io" and parts.path.rstrip("/") == f"/contents/{code}":
            try:
                payloads.append(await response.json())
                ready.set()
            except Exception:
                pass

    page.on("response", observed)
    await page.goto(source, wait_until="domcontentloaded", timeout=40_000)
    await asyncio.wait_for(ready.wait(), timeout=20)
    payload = payloads[-1]
    if payload.get("status") != "ok":
        raise ResolutionError("GoFile could not open this public file. Check access or retry later.")
    item = single_file(payload["data"])
    url = item["link"]
    return {"url": url, "name": item["name"], "content_id": item["id"],
            "size": int(item["size"]), "cookies": await context.cookies([url]),
            "user_agent": await page.evaluate("navigator.userAgent")}


async def download_page(context, source, provider):
    page = await context.new_page()
    arrived = asyncio.Event()
    downloads = []
    page.on("download", lambda download: (downloads.append(download), arrived.set()))
    try:
        response = await page.goto(source, wait_until="domcontentloaded", timeout=40_000)
        if response and response.status in (404, 410):
            raise ResolutionError("The file host reports that this file is unavailable.")
    except ResolutionError:
        raise
    except Exception:
        if not downloads:
            raise
    if not downloads:
        # Let the site's ordinary countdown enable its own Download control.
        selectors = [
            page.get_by_role("button", name=re.compile(r"^\s*(?:free download|download(?: file| now)?|скачать)\s*$", re.I)),
            page.get_by_role("link", name=re.compile(r"^\s*(?:free download|download(?: file| now)?|скачать)\s*$", re.I)),
            page.locator('input[type="submit"][value*="Download" i]'),
        ]
        deadline = asyncio.get_running_loop().time() + 45
        while not downloads and asyncio.get_running_loop().time() < deadline:
            clicked = False
            for locator in selectors:
                for index in range(await locator.count()):
                    control = locator.nth(index)
                    if await control.is_visible() and await control.is_enabled():
                        await control.click(timeout=5000)
                        clicked = True
                        break
                if clicked:
                    break
            await asyncio.sleep(1)
        if not downloads:
            raise TimeoutError("The file host did not start a public download.")
    download = downloads[0]
    # A download event fires when headers arrive. Cancel its browser transfer;
    # the installer will stream through its own resumable HTTP client.
    await download.cancel()
    code = urlsplit(source).path.strip("/").split("/")[0] if provider == "filekeeper" else source
    return {"url": download.url, "name": download.suggested_filename,
            "content_id": code or download.url, "size": None,
            "cookies": await context.cookies([download.url]),
            "user_agent": await page.evaluate("navigator.userAgent")}


async def resolve(source):
    from playwright.async_api import async_playwright
    provider = provider_for(source)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            last_error = None
            for attempt in range(1, 4):
                emit("status", message=f"Preparing {'GoFile' if provider == 'gofile' else 'FileKeeper'} download ({attempt}/3)…")
                context = await browser.new_context(accept_downloads=True)
                try:
                    result = await (gofile(context, source) if provider == "gofile"
                                    else download_page(context, source, provider))
                    return validate_result(result, source)
                except ResolutionError:
                    raise
                except Exception as exc:
                    last_error = exc
                finally:
                    await context.close()
                if attempt < 3:
                    await asyncio.sleep(attempt)
            raise ResolutionError("Could not prepare this file after three attempts. Check the link or retry later.") from last_error
        finally:
            await browser.close()


async def run(source):
    loop = asyncio.get_running_loop()
    work = asyncio.create_task(resolve(source))

    def watch_parent():
        sys.stdin.readline()
        if not loop.is_closed():
            loop.call_soon_threadsafe(work.cancel)

    threading.Thread(target=watch_parent, daemon=True).start()
    try:
        result = await work
        emit("resolved", file=result)
    except asyncio.CancelledError:
        pass
    except ResolutionError as exc:
        emit("failed", message=str(exc))
    except Exception:
        emit("failed", message="The download browser could not start. Install the browser for development or reinstall the packaged app.")


async def self_check():
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content("<title>Download browser ready</title>")
            if await page.title() != "Download browser ready":
                raise RuntimeError("Browser check failed")
        finally:
            await browser.close()
    emit("ready")


def main():
    if sys.argv[1:] == ["--self-check"]:
        asyncio.run(self_check())
        return 0
    try:
        source = json.loads(sys.stdin.readline())["url"]
        parts = urlsplit(source)
        if (not provider_for(source) or parts.scheme != "https" or parts.username
                or parts.password or parts.port not in {None, 443}):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        emit("failed", message="Use a public HTTPS GoFile or FileKeeper link.")
        return 2
    asyncio.run(run(source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
