# macOS Game Installer + Highball handoff

## Goal

Build a macOS version of Game-Downloader that makes installing a catalog game and opening it in Highball straightforward. The local SwiftUI demo now connects catalog browsing, a persistent install queue, staged extraction/repair, and explicit Highball registration/launch actions. Real-game end-to-end and interactive UI verification remain pending.

## Current project

- The repository is a Python/PyQt6 Windows desktop installer. It reads `games.json`, downloads ZIP/RAR archives, extracts them with progress and cancellation, and can merge an optional repair archive.
- The current local catalog has 44 entries and includes a `macCompatibility` field. `macos/GameDownloaderMac` contains a native SwiftUI demo backed by Mac-specific Python modules in `macos/backend`; it is not consumer-distribution-ready.
- The user moved this repository to `/Users/kevinanil/Game-Downloader` and created a local Codex project named `Downloader` for it.

## Desired Mac experience

1. Detect whether Highball is installed and whether a suitable bottle/engine is ready. If setup is missing, guide the user through Highball's official install and first-run steps.
2. Let the user choose a game and a visible install folder, preferably under `~/Games/Game Installer/<Game Name>`.
3. Download with resumable partial files, verify hashes when the catalog provides them, extract, and apply that game's explicitly configured repair files or setup steps.
4. Apply a saved, tested game recipe: executable path, renderer, launch arguments, DLL overrides/environment, and any required dependencies or patch actions.
5. Add or update the game in Highball and launch it there. Prefer Highball's supported CLI over editing its private bottle files directly.
6. Save useful logs and report what was verified. Preserve partial downloads for retry.

Highball's source repository has a CLI target, supports pinning executables outside a bottle, and supports per-program launch arguments, environment variables, and renderer settings. However, the official installed `Highball.app` packaging script builds and bundles the GUI target, not the standalone CLI. The Mac installer cannot assume that a user's installed Highball includes a usable `highball` command. Treat a matching, distributable helper or an upstream-supported integration API as a prerequisite to one-click pin configuration and launch; prototype and validate this path before promising that flow. Highball is GPL-3.0; its compatibility database is CC0. If the Mac app later embeds or modifies Highball code, review and follow the GPL terms.

### Verified Highball helper spike (2026-10-25)

- Built the unchanged Highball 0.10.22 CLI from reviewed commit `daa2095b9fd4abad0b9db7c57cacbd4d49a9e06e` and installed a local arm64, ad-hoc-signed helper at `~/Library/Application Support/Game-Downloader/HighballHelper/0.10.22/`.
- `spikes/highball/install_helper.sh` reproduces that local build, checks the installed app version and source commit, signs ad hoc, validates the helper interface and hash, and preserves the prior helper on replacement.
- Read-only live readiness returned ready: engine `x64-sikarugir10.0_6-r28`, Wine 10.0, and the `Games` bottle using D3DMetal; no damaged bottles were reported.
- A disposable-home smoke test successfully created a temporary bottle, pinned its built-in `cmd.exe`, set `/c exit 0`, and ran it. The temporary bottle was stopped and removed; the user's `Games` bottle was not used or changed.
- All 28 spike tests pass. No Developer ID signing identities are configured on this Mac. The local helper is for development only and is not suitable for consumer distribution; signing/notarization and corresponding GPL source materials remain required for shipping.

### Local SwiftUI preview

- `macos/build_preview.sh` creates an ad-hoc-signed preview at `~/Applications/Game Installer Preview.app` and preserves the previous build; it does not open the app.
- The preview preserves the dark library/card design and adds meaningful Downloads and Settings pages, saved folder/bottle choices, and a serial queue with pause/cancel/retry and restart recovery. HTTPS/Google Drive downloads preserve partials and validate ranges; extensionless URLs and multipart RAR source forms are supported.
- ZIP extraction/repair works through the Python standard library. RAR/7z uses the already installed local unar/lsar tools; missing tools and unsupported methods are reported explicitly. All merges happen in new staging folders; exclusive directory publication refuses existing targets. Completed archives and receipts remain available for retry.
- Installation, Highball registration, and launch are separate user actions. Pins use a separate Game Installer namespace; conflicts and unowned pins are refused. The GUI must be closed before registration. The checked helper is used without editing private bottle JSON; failed registration keeps installed files.
- The Mac installer now includes four build-matched records: MECCHA CHAMELEON, Cut That Wire, Buckshot Roulette, and Party Animals. MECCHA and Cut That Wire apply their recorded pin settings after archive hashes, engine, renderer, and prerequisites match. Buckshot keeps Highball's renderer/arguments at defaults and reminds the user to open Steam in the bottle first. Party Animals checks that `UnityPlayer.dll` and `PartyAnimals_Data` exist after its exact multipart archive is extracted; a missing required file prevents publishing that incomplete install and preserves downloads. Other games still require manual executable selection and use bottle defaults. Prediction badges do not establish a tested recipe.
- Get Spacewar and Steam catalog entries explicitly hand installation to the Windows Steam pin inside the selected bottle. No macOS Steam routing is used; the user must complete/sign in to Windows Steam. Repair-only patch application to existing Steam installs is still pending.
- The app uses local Python 3 for readiness and installation. Bundle a runtime and signed extraction/helper tools before distribution. No Developer ID/notarization claim is made.
- Launch requests use the helper with detached file logging; a long-running Windows Steam client does not hold the UI busy. The app reports a request, not verified game startup, and offers its launch log.
- Verification: 34 new backend tests (mock HTTP/Highball, real tiny ZIP/7z fixtures, and a real cached-fixture worker process), three compiled Swift model-check groups covering the 44-entry catalog, the 28 existing helper tests, and a successful release build/signature check. No catalog game was downloaded/launched and the live Games bottle was not changed. Native UI automation failed before returning app state repeatedly, including after resetting its runtime; headless rendering was unsuitable for native scroll views, so interactive UI verification remains outstanding. See `macos/README.md` for commands and limitations.

## Recipe and compatibility direction

Keep recipes as data separate from installer UI, either in a dedicated catalog field or a versioned recipe file. A recipe should include only repeatable, verified actions and should record which Highball/engine, macOS version, and chip were tested. Mark results separately for boot, main menu, gameplay, lobby hosting, invites, and joining. A successful launch does not prove multiplayer invites work.

### Existing Highball settings to use as recipe seeds

At the time of handoff, the `Games` bottle uses D3DMetal. Its saved Highball pins contain these per-game settings. They are useful starting data, but the app must not treat a saved setting by itself as proof that the setting caused a fix. Check each game's compatibility note before promoting it to an automatic recipe.

| Game | Saved launch arguments | Saved `WINEDLLOVERRIDES` |
|---|---|---|
| MECCHA CHAMELEON | `-windowed -ResX=1280 -ResY=720` | `winmm=n,b` |
| Fears to Fathom – Scratch Creek | `-screen-fullscreen 0 -screen-width 1280 -screen-height 720` | `winmm=n,b` |
| Buckshot Roulette | none | `OnlineFix64=n,b;SteamOverlay64=n,b;steam_api64=n,b` |
| Cut That Wire | `-screen-fullscreen 0 -screen-width 1280 -screen-height 720` | `winmm=n,b;OnlineFix64=n,b;SteamOverlay64=n,b;steam_api64=n,b` |
| Knights End | `-windowed -ResX=1280 -ResY=720` | `winmm=n,b;OnlineFix64=n,b;steam_api64=n,b;EOSSDK-Win64-Shipping=n,b` |
| Forest Escape: Last Train | none | `winmm=n,b;OnlineFix64=n,b;steam_api64=n,b;EOSSDK-Win64-Shipping=n,b` |
| MIMESIS | none | `winmm=n,b;OnlineFix64=n,b;SteamOverlay64=n,b;steam_api64=n,b` |

Detailed test notes live on this Mac at `/Users/kevinanil/Documents/Game-Downloader Mac compatibility/`. Read the relevant notes when preparing recipe data; they record executable paths, archive/repair details, exact observations, and which multiplayer capabilities were actually tested. The user wants those established recipes to become app data so consumers do not have to repeat the troubleshooting manually.

Past tests show why this matters: Fears to Fathom – Scratch Creek took troubleshooting before it reached a playable main menu; Buckshot Roulette reached a lobby and lobby-ID joining seemed to work, while the Steam invite button did nothing; The Backrooms: Rescue Expedition reached gameplay but showed an empty friend list and had a render-thread timeout on one relaunch before working after another relaunch. These should be treated as diagnostic history, not universal fixes. Preserve notes even if a game is uninstalled.

For an unfamiliar failure, collect the game's exact error, Highball launch log, renderer/engine, detected proxy DLLs, and the action that changed the result. Turn a confirmed fix into a recipe and verify it on a fresh or repeat launch before marking it automatic.

## Next milestone: manual demo verification and disposable-bottle game test

Reopen the rebuilt demo and verify navigation, install controls, queue persistence/progress and error presentation interactively. Then, with authorization for an actual game download/launch, verify one exact recorded build in a new disposable Highball bottle, including the visible external install location. Keep the existing Games bottle and Windows path intact. Repair-only Steam installs, consumer runtimes/signing/licensing and clean-Mac validation remain later work. Do not claim one-click game compatibility until the download, recipe, pin and launch flow is verified end to end; check gameplay, hosting, invites and joining separately.

## Current references

- Highball repository and CLI overview: https://github.com/gauthierpiarrette/highball
- Highball support for pinning preinstalled games: https://github.com/gauthierpiarrette/highball/releases/tag/v0.7.6
- Highball per-program settings: https://github.com/gauthierpiarrette/highball/releases/tag/v0.7.4
- Game-Downloader repository: https://github.com/KevinAwesomeCoding/Game-Downloader
