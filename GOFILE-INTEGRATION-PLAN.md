# Hosted download integration

Implemented October 27, 2026. GoFile and FileKeeper share links now use a browser
resolver followed by the shared resumable HTTP download engine in both installers.

## How it works

`host_resolver.py` opens the host's ordinary public page in an isolated browser.
GoFile's page supplies its single file's metadata and ordinary `/download/web/`
address. FileKeeper runs its own countdown and Free download control; the helper
captures the resulting download and immediately cancels the browser's transfer.
The installer then downloads through Python with the matching session cookies,
user agent, and referrer. The browser closes before the archive transfer starts.

`host_resolver_client.py` runs the helper separately, supplies progress messages,
and cancels it when the user stops the job. Link preparation has up to three
attempts. GoFile shares must contain exactly one accessible file. Temporary
credentials remain in memory and do not enter catalogs, receipts, or logs.

Only HTTPS host addresses and the observed FileKeeper delivery domain
`fetchio.cc` are accepted as resolved destinations. Cookies retain their domain
and path scope. An unrelated redirect is rejected.

`download_support.py` owns the shared transfer engine. The native Mac worker
imports it through `macos/backend/downloads.py`; the Windows InstallWorker routes
GoFile and FileKeeper sources through the same engine. Other Windows sources
retain their existing download handlers. Pixeldrain share-form URLs are normalized
to their ordinary file API address.

## Resume, progress, and fallback

- The installer reports link preparation, transferred bytes, and download progress.
  Windows also reports speed and estimated remaining time.
- Pause/cancel keeps partial archives. Resume prepares a fresh host session, checks
  the stable file identity, and uses HTTP Range/If-Range with server validators.
- Changed identities, ignored ranges, invalid ranges, truncated responses, and
  HTML pages cannot be promoted as completed archives. HTTP 416 alone is not
  proof of completion. A rejected/expired host session is refreshed once.
- Existing SHA-256 completion records verify cached files before reuse. Completed
  backups are verified and reused at the final path without another transfer.
- Each primary and backup has separate partial files. Multipart shares remain
  ordered by the existing catalog and archive extraction rules.

The proposed older-build mirrors were removed where matching archive builds had
not been established. Their discovered addresses remain in `online_fix_hosters.txt`
for reference. Both catalog copies are synchronized byte-for-byte. This change
preserves existing requirements and compatibility observations.

## Packaging

`tools/build_host_helper.py` creates an isolated build environment, installs pinned
Playwright 1.64.1, and freezes a native HostResolver executable. It copies the
matching Chromium headless shell after freezing so its native loader paths remain
intact, then runs a browser launch check. The helper is about 360 MiB unpacked on
the tested Mac; compressed release sizes differ.

Windows embeds the helper in both folder and single-file builds. CI launches each
packaged application with `--check-download-browser` to test that the embedded
helper and browser can start. The single-file build extracts its resources when
launched, so startup requires additional time and temporary disk space.

The Mac build copies shared sources and the helper into Backend resources, signs
its nested native binaries, and verifies the final app signature. Its host resolver
needs no Playwright installation in the user's Python. The existing Mac backend
and readiness checks still require Python 3.

The supported targets follow the current browser/runtime and project: native
Windows 11+ x64 and Apple silicon macOS 14+. Windows 10, Intel Mac, and running the
Windows browser helper inside Wine/Highball have not been established as supported.

The release workflow builds and checks both platforms. Branch workflow runs build
artifacts without publishing releases; publication is restricted to main.

## Verification

The automated checks cover session handoff, resume with a fresh session, changed
file identities, HTML rejection/session refresh, length mismatches, completed
backup reuse, helper cancellation, destination validation, and the actual Windows
worker's shared-transfer path. Existing Mac archive/install/Highball checks are
also run. Two stale Highball fixture expectations were updated to reflect the
existing prerequisite and detected-DLL behavior.

Live checks have retrieved archive ranges from both GoFile and FileKeeper after
closing the browser. The shared engine and packaged Mac backend are checked with
pause/resume and a small full download; those checks inspect bytes/checksums only.
The earlier full GoFile browser downloads also succeeded for roughly 8.5 MiB and
1.1 GiB archives. Per-platform CI smoke checks establish packaged browser startup;
they do not prove that every third-party URL will remain available.

The catalog audit checked all 32 new GoFile primary links and confirmed one
accessible file in each share. All 30 FileKeeper backup archives matched their
GoFile primary's decoded filename and exact byte size. The two Pixeldrain backup
volumes also passed range and size checks. One FileKeeper attempt failed
transiently and passed on retry. These are availability and metadata checks,
not a claim that separate hosts' entire archive bytes were hashed and compared.

Run local deterministic checks:

```sh
python -m unittest discover -s tests -v
python -m unittest discover -s macos/backend/tests -v
```

For source development and the standalone tools, activate the project virtual
environment and install the browser:

```sh
python -m pip install -r tools/requirements-gofile.txt
python -m playwright install chromium
python tools/gofile_probe.py https://gofile.io/d/SHARE_CODE
python tools/gofile_download.py
```

The probe reads small ranges and discards them. The standalone downloader saves
its file in the folder selected at its prompt. Packaged applications use their
existing job caches instead of the prototype's `downloads/` folder.

## Repository hygiene

`downloads/`, build outputs, and `.DS_Store` files are ignored. Existing test
archives remain local and are excluded from Git. Manually zipping an entire
project directory can still include ignored files; release packaging selects only
its build outputs and declared resources.

## References

- [GoFile API](https://gofile.io/api)
- [Playwright standalone packaging](https://playwright.dev/python/docs/library#pyinstaller)
- [Playwright browser management](https://playwright.dev/python/docs/browsers)
- [Playwright system requirements](https://playwright.dev/python/docs/intro#system-requirements)
