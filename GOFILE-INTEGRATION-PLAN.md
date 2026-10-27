# GoFile installer integration

Investigation: October 27, 2026. Production installer integration is not implemented yet.

## Verified approach

Use a short browser session to open the ordinary GoFile share page. Read the
single file's download information from the response the page already requests,
then pass its HTTPS `/download/web/` URL, scoped session cookies, user agent,
filename, content ID, and expected size to the installer's existing HTTP transfer.
The browser can close before the archive transfer starts.

The browser is needed only for resolving the share and obtaining its session.
The archive should stream through the existing installer downloader so its
progress, cancellation, cache, and resume behavior stay connected to the UI.

No stable browser-free anonymous resolver has been established. GoFile's
documented content-listing API and direct-link creation are Premium features.
Use the normal public page rather than reproducing its internal website tokens.

## Live evidence

Environment: native macOS arm64, Python 3.13.15, Playwright 1.64.1.

- Earlier full downloads succeeded for an 8.5 MiB archive and a roughly 1.1 GiB
  archive using the standalone browser downloader.
- The public page response for `https://gofile.io/d/AGYixA` contained one accessible
  file, with a size of 8,917,438 bytes and its ordinary `/download/web/` URL.
- After closing the browser, a Python standard-library HTTP client with the
  captured session received HTTP 206 and `Content-Range: bytes 0-65535/8917438`.
- A second request with Range and the server's Last-Modified value as If-Range
  received HTTP 206 and `Content-Range: bytes 65536-131071/8917438`.
- The same URL without the session returned HTTP 200 with HTML, rather than
  archive bytes. A bare resolved URL is insufficient for this tested flow.
- The server supplied Last-Modified but no ETag in the sampled response.

The sample requests read small byte ranges and discarded them. They did not save,
extract, or execute another archive. Windows execution and packaged-app execution
have not been verified.

Install the prototype dependencies and browser in an activated virtual
environment. These dependencies are separate from the production installer:

```sh
python -m pip install -r tools/requirements-gofile.txt
python -m playwright install chromium
```

`tools/gofile_probe.py` reproduces the browser-to-HTTP handoff and range checks:

```sh
venv/bin/python tools/gofile_probe.py https://gofile.io/d/AGYixA
```

It observes the page's normal response, closes the browser, then reads at most
two 64 KiB ranges through Python's standard-library HTTP client. It prints no
session credentials and saves no archive.

## Integration design

1. Add one shared resolver implementation and a small helper process interface.
   Return the original source URL, file ID, filename, size, ordinary download URL,
   scoped cookies, and request context. Only invoke it for recognized HTTPS
   GoFile share links. Verify exactly one accessible file, including the reported
   total child count; do not silently choose the first item of a folder.
2. Resolve with up to three bounded attempts and short backoff for transient
   failures. Report an installer status such as “Preparing GoFile download…”.
   A user cancellation must close the helper and its browser promptly.
3. In `main.py`, route GoFile sources through the resolver before the existing
   streaming transfer. Put the cookies in its `requests.Session` with their
   original domain/path scope, and carry the browser user agent and referrer.
4. In `macos/backend/downloads.py`, resolve before opening the archive response.
   Add the cookies to its existing CookieJar and keep its existing transfer,
   progress callbacks, SHA-256 receipts, and cancellation checkpoints.
5. Keep the original share URL as the persistent cache source. Re-resolve after
   app restart or a rejected/expired session. Do not store cookies in receipts,
   manifests, logs, or source control. Confirm the file ID and size still match
   before resuming and validate the returned byte range and validators before
   appending to a partial. Do not treat HTTP 416 alone as proof of completion.
6. Preserve each part's primary/backup association. Each listed GoFile share must
   contain one file; the existing catalog can still list multiple shares for a
   multipart archive. Exhaust resolution attempts before moving to the matching
   backup. Keep separate partials for different sources.
7. Check the response for HTML and its expected byte length before promoting a
   partial archive. Continue through the current archive identification and
   extraction flow only after transfer completion.

The existing `zipUrl`, `fixZipUrl`, `zipBackupUrl`, and `fixBackupUrl` catalog
fields already carry the required links; this does not need a catalog schema
change. FileKeeper support is a separate provider task.

## Distribution choice

Bundle a matching, pinned Playwright runtime and Chromium headless shell with an
OS-specific resolver helper. This lets the Mac backend keep using its existing
Python without requiring users to install Playwright into that Python. The same
helper contract can be used by the Windows app. A standalone packaged helper
also prevents a browser launch failure from blocking ordinary HTTPS sources.

Playwright documents standalone PyInstaller builds, bundled browsers, and a
headless-shell-only install. The helper adds substantial package size; measure
the resulting releases rather than guessing a size.

- Windows x64: include the helper in both release layouts. The single-file
  executable will need its embedded helper/browser resources extracted at run
  time, so measure startup and extraction behavior before releasing it.
- macOS arm64: build a native helper, include it with the app's backend resources,
  and include its executable/framework signing in the Mac packaging process.
  The current Mac app already targets Apple silicon and macOS 14 or later.
- Update `.github/workflows/build.yml` to build the helper on each OS and trigger
  when shared resolver sources change. Installing the Python package alone does
  not install or bundle its browser.
- Current Playwright documentation lists Windows 11+ and macOS 14+. Do not claim
  Windows 10, Intel Mac, or the Windows app running inside Wine/Highball is
  supported by this change without separate evidence.

## Completion checks for implementation

- One accessible file resolves; zero/multiple files, private/password/error pages,
  and unsupported URLs produce clear failures.
- The HTTP transfer still works after helper exit, reports byte progress, and
  detects a session that returned HTML instead of a file.
- Cancellation during resolution and transfer leaves no active helper/browser.
- Resume works after pause and app restart with a fresh session. Changed files,
  ignored ranges, incorrect ranges, and truncated bodies cannot corrupt a partial.
- Primary/backup fallback and existing Google Drive/direct HTTPS sources work.
- Native Windows and native Mac packaged builds work without a development venv
  or an independently installed browser. Larger downloads retain progress and
  cancellation behavior.

## Cleanup state

`downloads/` is ignored by Git. The two earlier test archives remain local.
Ignoring them excludes them from Git exports but does not reclaim disk space or
exclude them from a manually created ZIP of the whole project folder. Release
packaging should include only build outputs and explicitly selected resources.

## References

- [GoFile API](https://gofile.io/api)
- [Playwright standalone packaging](https://playwright.dev/python/docs/library#pyinstaller)
- [Playwright browser management and headless shell](https://playwright.dev/python/docs/browsers)
- [Playwright system requirements](https://playwright.dev/python/docs/intro#system-requirements)
