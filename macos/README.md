# Game Installer for Mac — local demo

This is the native macOS interface preview. Its dark library grid follows the
Windows installer's card layout and accent colors, with catalog artwork,
descriptions, version and size, player counts, availability/patch labels, and
Mac compatibility badges. It also has Library, Downloads, Settings, search,
install-folder selection, and Highball readiness views.

The install and Highball actions now have a working local backend. The Windows
app, catalog, and Windows release workflow remain separate and unchanged.

## First game on a new Mac

On first launch, Game Installer asks you to choose its Games folder before it
shows the library. macOS grants access through that folder picker, and the app
checks that it can create and remove a temporary file there. This is the only
extra file permission the flow needs. It does not ask for Full Disk Access,
Accessibility, or Automation access. Changing the folder later is available in
Settings.

Game Installer keeps each game in the install folder you choose. Highball gets
a game entry that points to that folder; the game files are not copied into the
bottle. For a recorded game recipe, the installer can prepare that bottle's
required Windows components before the download, then add the verified game to
Highball automatically with its title, catalog cover, executable, and saved
launch settings. It does not launch the game automatically or sign you into
Steam.

1. Install Highball from its [official install guide](https://gethighball.com/docs/install/),
   move it to Applications, open it, and press **Get started**. Leave Highball
   open until it finishes downloading its engine and creating the first Windows
   environment. Highball currently requires an Apple silicon Mac (M1 or newer)
   and macOS 14 or newer.
2. In Game Installer → **Settings** → **Highball readiness**, select a ready
   bottle under **Game launch bottle**. The first recorded game that needs
   Windows Steam and Visual C++ runtimes will offer **Set Up & Download**.
   Accepting installs only those missing components into that bottle. This
   briefly closes and reopens Highball so its settings do not overwrite the
   installer changes. Steam sign-in remains yours and can wait until you play.
   Those components are shared by all games in that bottle.
3. Choose a recorded game and press **Download & add to Highball**. After the
   archive build is verified and extracted, Game Installer adds the game pin
   and cover automatically. If the build does not match the saved recipe, it
   keeps the installed files and offers manual executable selection instead.
4. Use the game's **Play** action when you are ready. For a **Steam first**
   recipe, the first Play action opens Windows Steam in the selected bottle;
   sign in there, then choose Play again to start the game.

If Settings reports no Highball environment, finish Highball's first-run setup
and press **Check again**. Choosing **Download Only** skips prerequisite setup
and automatic registration for that job; the existing **Add with recorded
recipe** button remains available afterward. The installer does not create
fake Steam installation records.

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
- **Primary and backup catalog links:** `zipBackupUrl` and `fixBackupUrl` are
  tried only after the corresponding primary link fails. Multipart backup lists
  must match primary part-for-part and use the same order; each host keeps a
  separate partial download.
- **GoFile and FileKeeper:** the bundled browser helper prepares a public file's
  download session, then closes while the shared Python downloader streams the
  archive with progress, cancellation, and verified resume. GoFile shares must
  contain exactly one accessible file. The helper and its Chromium headless
  browser are included in Mac releases; installing Playwright in system Python
  is not required. Build details are in `GOFILE-INTEGRATION-PLAN.md` at the repo root.
- **Staged extraction and repairs:** ZIP, including traditional password-protected
  ZIP using the catalog's existing password. The app bundles universal arm64/x86_64
  7-Zip and UnRAR helpers, so RAR/7z extraction does not require Homebrew or local
  tools. Paths, links, case/Unicode collisions and each extracted file's size are
  checked against the archive listing. Every catalog-listed game archive is
  extracted; separate archive parts are merged only when their paths do not collide.
  Repairs overlay only the newly staged game; no existing installed game is modified.
  Publishing uses macOS exclusive rename and refuses even an empty existing destination.
- **Highball registration and launch:** helper identity/readiness is
  checked before every action. New pins use the game's title and the catalog thumbnail
  is added through Highball's per-item cover store when available. Existing receipt-owned pins keep their names
  on retry. Conflicting/unowned names stop with an error. Retrying an
  owned, partially configured pin is supported. Highball's GUI must be closed
  during registration to avoid stale settings writes; Windows Steam may remain up.
  Pin operations use the supported CLI; artwork uses Highball's cover-file layout.
  If a recorded renderer is unavailable, registration falls back to the selected
  bottle's renderer only when Highball reports it available, and reports the change.
- **Per-bottle prerequisites and automatic add:** recorded games declare only
  their needed Windows Steam and Visual C++ prerequisites. Before downloading,
  the app offers to install missing components in the selected Highball bottle;
  completed prerequisites are kept if a later step fails. After a matching
  archive is installed, the app automatically registers the external game path,
  title, cover and saved settings. This does not copy game files into the bottle
  or start the game. Component recipes affect every game using that bottle.
- **Recorded recipes:** MECCHA CHAMELEON, Cut That Wire, Buckshot Roulette,
  Party Animals, and the exact-build Needle In A Haystack storage fix. Exact main/repair archive hashes and the tested Highball
  engine must match before a recipe can be applied. Recipes may leave renderer,
  arguments, or environment at the bottle defaults when no override was verified.
  Party Animals also checks for `UnityPlayer.dll` and `PartyAnimals_Data`; if either
  is missing, the install completes but its recorded Highball recipe is unavailable.
  On the first Play click, recipes that need Steam open the Steam pin; sign in, then
  click Play again to launch the game. For verified builds, detected DLL names
  that collide with Wine builtins receive per-game native-first overrides. Steam
  install records are never fabricated. These local
  observations do not prove every gameplay or multiplayer feature works.
- **Other games:** after installation, the installer picks a likely executable
  (exact title match first, then common shipping names or one unambiguous .exe)
  and creates a pin using bottle defaults plus any detected Wine DLL-loading
  overrides. If detection is ambiguous, Choose .exe & add remains available;
  Change .exe can correct a registered pin. No unverified renderer or per-game
  fixes are inferred.
  A prior recorded-settings pin is not silently converted to default settings.
  Registration errors keep installed files, cache, and receipts for retry.
- **Launch Steam:** asks Highball to resolve Windows Steam for the selected bottle,
  then calls `highball steam launch <bottle> --uri steam://open/main`. The resolved
  executable is shown in the action message. Highball owns discovery and the
  persistent per-bottle override; Game Installer has no separate Steam-path setting.
  To correct the path, use Highball > Settings > Environments > the environment's
  settings > Advanced > Steam executable. Missing or invalid paths stop before dispatch.
- **Get Spacewar and catalog Steam entries:** after confirmation, send
  `steam://install/<id>` through the same Highball operation. Windows Steam handles
  sign-in and downloads; the app reports a handoff, not a completed installation.
- **Play in Highball:** when the catalog app ID and exact installed folder match
  Windows Steam's own fully installed manifest, call `highball steam game` so Steam
  handles ownership and authentication via `-applaunch`. Local copies keep the
  ordinary pin flow and receive a clear explanation that opening Steam cannot grant
  them official Steam authentication. No install records or ownership are fabricated.

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

The build first refreshes the bundled Highball helper when its local Steam integration
patch changes, then copies the current catalog and readiness scripts into Swift package
resources, along with the Mac backend, compiles the SwiftUI app, and ad-hoc signs a local preview. It
preserves the previous preview in `~/Applications` when rebuilding. The live
readiness check currently calls `python3`; the packaged consumer app will need
a bundled runtime or a native Swift implementation. The preview is not signed
or notarized for distribution.

The demo requires local Python 3 and the previously installed Highball 0.10.22
development helper at
`~/Library/Application Support/Game-Downloader/HighballHelper/0.10.22/`. Its
`build-manifest.json` must match the app and helper bytes. The spike README
documents its build and GPL requirements. The helper is built from the reviewed
upstream commit plus `macos/highball-steam.patch`; its manifest requires
`steam_integration: steam-path-v1`. Existing unpatched helpers are rejected with
an update direction. The complete patched source and patch accompany the helper.
Rebuild Highball from the nested repository as well to expose its override picker;
Game Installer's helper setup does not replace Highball.app. The preview build compiles the pinned
RARLAB UnRAR source and bundles its license and acknowledgements; it uses UnRAR
for complete split RAR volume sets. It also downloads and checksum-verifies the
official universal 7-Zip release, bundles its license, and uses it for 7z and
other RAR formats. No Homebrew installation or local archive utilities are needed.
Settings reports whether the bundled RAR/7z tools are present.
The Highball helper remains an ad-hoc development build, not a
consumer-distribution-ready helper.

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
  its patch to a pre-existing Steam game is not implemented. Uninstall,
  overwrite-existing-folder, automated prerequisite installation,
  private Drive authentication or split-ZIP reconstruction flow.
- ZIP AES/unsupported compression methods stop clearly. Split RAR archives are
  extracted as a single volume set by UnRAR. The release build still needs
  Developer ID signing and notarization; the preview uses an ad-hoc signature.
- Scratch Creek's invite workaround now applies only to its exact recorded archive
  and assembly hashes. The installer preserves `Assembly-CSharp.original.dll`,
  verifies the patched output, and skips Highball setup if any guard fails.
  Its Unity runtime and game data files are required before the recipe can apply.
  Other historical settings are not guessed from detected DLLs or compatibility predictions.
- Needle In A Haystack's storage patch is limited to its recorded v1.2.7 archive
  and original assembly hashes. The local build stages the verified helper and
  patched assembly from `~/Library/Application Support/Game-Downloader/Diagnostics/Needle-20261026-194923/`;
  the original managed assembly is backed up beside the game before patching.
- Startup, menu, gameplay, hosting, invites and joining remain separate results.
  Archive hashes identify tested bytes, not publisher authenticity. Menu arrival,
  an install receipt or a successful CLI exit cannot establish multiplayer.
- Bundle the Python runtime, add Developer ID signing/notarization, GPL corresponding
  source/notices for the helper, and clean-Mac end-to-end tests
  before presenting this as a consumer-ready installer.

For the launch trace, changed files, tests, and verification limits, see
[Steam launch-path fix](STEAM-PATH-FIX.md). The cross-repository tests can use the
actual built helper without launching Wine:

```sh
HIGHBALL_STEAM_TEST_CLI="$PWD/Highball/.build/debug/highball" \
  python3 -B -m unittest discover -s macos/backend/tests -p 'test_*.py' -v
```
