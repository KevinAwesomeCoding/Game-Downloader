# Game Installer for Mac — local demo

This is the native macOS interface preview. Its dark library grid follows the
Windows installer's card layout and accent colors, with catalog artwork,
descriptions, version and size, player counts, availability/patch labels, and
Mac compatibility badges. It also has Library, Downloads, Settings, search,
install-folder selection, and Highball readiness views.

The install and Highball actions now have a working local backend. The Windows
app, catalog, and Windows release workflow remain separate and unchanged.

## What works

- **Persistent serial install queue:** Download & install adds a job to Downloads.
  Progress, pause, cancel, retry, and app-restart recovery preserve completed
  archives and partial files. Interrupted jobs reopen paused rather than silently
  starting downloads. Install-folder and bottle choices are saved.
- **HTTPS and public Google Drive downloads:** streaming transfers, cookie-based
  confirmation forms, extensionless URLs, multipart source normalization,
  HTTP Range/If-Range with validators, length checks, and SHA-256 receipts. Changed
  content or ignored ranges start a separate transfer, preserving the old partial.
  Unidentified partials cannot safely resume and are likewise preserved separately.
  HTML, sign-in, quota, and unsupported responses produce an explicit error.
- **Staged extraction and repairs:** ZIP, including traditional password-protected
  ZIP using the catalog's existing password. RAR/7z uses optional local `unar` and
  `lsar`. Paths, links, case/Unicode collisions and each extracted file's size are
  checked against the archive listing. Every catalog-listed game archive is
  extracted; separate archive parts are merged only when their paths do not collide.
  Repairs overlay only the newly staged game; no existing installed game is modified.
  Publishing uses macOS exclusive rename and refuses even an empty existing destination.
- **Separate Highball registration and launch:** helper identity/readiness is
  checked before every action. New pins use the game's title and the catalog thumbnail
  is added through Highball's per-item cover store when available. Existing receipt-owned pins keep their names
  on retry. Conflicting/unowned names stop with an error. Retrying an
  owned, partially configured pin is supported. Highball's GUI must be closed
  during registration to avoid stale settings writes; Windows Steam may remain up.
  Pin operations use the supported CLI; artwork uses Highball's cover-file layout.
- **Two recorded recipes:** MECCHA CHAMELEON and Cut That Wire, from the local
  compatibility notebook. The exact main/repair archive hashes, tested engine,
  renderer availability and installed prerequisites must match before applying
  their saved executable/arguments/environment/renderer. The saved settings are
  read back before registration is marked complete. These are local observations,
  not universal fixes or proof that the new external install path works in-game.
- **Other games:** Choose .exe & add selects an executable within the installed
  folder and creates a pin using bottle defaults, without inferred DLL fixes.
  A prior recorded-settings pin is not silently converted to default settings.
  Registration errors keep installed files, cache, and receipts for retry.
- **Get Spacewar and catalog Steam entries:** after an explicit confirmation,
  hand `steam://install/<id>` to the Windows `Steam` pin in the selected Highball
  bottle with `highball run <bottle> Steam -- <uri>`. macOS Steam is never opened.
  Windows Steam must be installed/signed in; it owns the resulting installation.
  The app reports a handoff, not a completed Steam installation.

Launch requests detach with their output redirected to files in `MacPreview/LaunchLogs`,
so a running Steam client does not hold the installer busy until it exits. An
immediate nonzero exit is reported; later startup failures require inspecting the
log/window. Open log is shown after dispatch. Sending a request does not verify
startup, gameplay, or any multiplayer behavior.

Installation defaults to `~/Games/Game Installer/<Game Name>`. Queue snapshots,
per-job downloads, checksums, and install/registration receipts live under
`~/Library/Application Support/Game-Downloader/MacPreview/`. Download files opens
that job's cache; Show in Finder opens the installed directory. Downloads offers
an explicit Clean download files action after installation and Highball registration.
It verifies the installed receipt and each archive hash before removing only those
archive copies and their transfer metadata. Partial, failed, paused, and unregistered
jobs remain resumable; installed files and receipts are retained.
Remove from list hides a completed/stopped queue entry without removing its
installed files, archives or receipt. Existing install folders are still protected.

## Build and open locally

From the repository root:

```sh
bash macos/build_preview.sh
open "$HOME/Applications/Game Installer Preview.app"
```

The build copies the current catalog and readiness scripts into Swift package
resources, along with the Mac backend, compiles the SwiftUI app, and ad-hoc signs a local preview. It
preserves the previous preview in `~/Applications` when rebuilding. The live
readiness check currently calls `python3`; the packaged consumer app will need
a bundled runtime or a native Swift implementation. The preview is not signed
or notarized for distribution.

The demo requires local Python 3 and the previously installed Highball 0.10.22
development helper at
`~/Library/Application Support/Game-Downloader/HighballHelper/0.10.22/`. Its
`build-manifest.json` must match the app and helper bytes. The spike README
documents its build and GPL requirements. For RAR/7z, `unar` and `lsar` must
already exist in `/opt/homebrew/bin` or `/usr/local/bin`; Settings reports them.
No tools or engines are downloaded automatically. The local helper is an ad-hoc
development build, not a consumer-distribution-ready helper.

## Verification

```sh
python3 -B -m unittest discover -s macos/backend/tests -v
bash macos/check_models.sh
python3 -B -m unittest discover -s spikes/highball -p 'test_*.py' -v
bash macos/build_preview.sh
```

- 34 backend tests cover streaming/resume and validator failures, changed/ignored
  ranges, 416 handling, Google Drive confirmation/quota/HTML, incomplete transfers,
  cache integrity, path/link/collision rejection, encrypted ZIP, repair placement,
  exclusive publication, cancellation, recipe gates, pin conflicts/retry, and
  Windows Steam routing. HTTP and Highball calls are mocked.
- Real local fixtures exercise native 7z extraction and the actual worker
  subprocess. The worker installs and repairs tiny cached test archives, then
  recovers the completed receipt on retry. No catalog archive is downloaded and
  the fixture executable is never run. Native RAR is covered with a mocked
  listing/extraction adapter; this flow has not extracted a real game RAR.
- Three compiled Swift check groups cover multipart catalog/job persistence,
  Steam metadata/worker events, and all 44 current catalog entries. They use
  `swiftc` because the available command-line toolchain lacks XCTest.
- The existing 28 helper tests still pass. The preview release build and strict
  ad-hoc signature verification pass. Hash comparisons confirmed `main.py`,
  `games.json`, the Windows workflow, the Highball GUI executable and existing
  `Games/bottle.json` were unchanged during development.
- Interactive inspection could not be completed: the native UI automation pipe
  closed before returning app state repeatedly, including after resetting the
  automation runtime. Headless SwiftUI rendering
  could not capture native scroll views/controls faithfully and is not treated
  as UI verification. Reopen the rebuilt app for the remaining manual check.

## Limits and next steps

- No catalog game, real Highball game launch, actual Spacewar download or live
  Games pin mutation was used for development tests. Verify visible navigation,
  install controls and progress on the actual app, then perform a separately
  authorized disposable-bottle game test before claiming end-to-end compatibility.
- The repair-only Steam catalog entry can hand off to Windows Steam, but applying
  its patch to a pre-existing Steam game is not implemented. There is no update,
  uninstall, overwrite-existing-folder, automated prerequisite installation,
  private Drive authentication or split-ZIP reconstruction flow.
- ZIP AES/unsupported compression methods stop clearly. RAR/7z requires local
  tools; shipping needs a reviewed, signed extraction runtime and license notices.
- Only the two recorded build-specific recipes are included. Scratch Creek's
  incomplete package/local assembly workaround is not automated. Other historical
  settings are not guessed from detected DLLs or compatibility predictions.
- Startup, menu, gameplay, hosting, invites and joining remain separate results.
  Archive hashes identify tested bytes, not publisher authenticity. Menu arrival,
  an install receipt or a successful CLI exit cannot establish multiplayer.
- Bundle the runtime, add Developer ID signing/notarization, GPL corresponding
  source/notices for the helper, version/update coordination and clean-Mac tests
  before presenting this as a consumer-ready installer.
