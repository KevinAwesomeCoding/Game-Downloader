# Steam launch-path fix

Implementation: 100%. Required focused verification: 100%. Live Steam sign-in and
gameplay were not exercised. No installed app/helper was replaced, and no real
bottle, Steam/game/save file, or pin was edited.

## Launch trace and cause

Game Installer's Launch Steam button calls `AppModel.requestSteam`, which starts
`WorkerSession` with the `steam` action, selected bottle, and `steam://open/main`.
The worker calls `Highball.steam_install`. Previously it listed pins and sent
`highball run <bottle> Steam`. Highball's CLI selected the pin, but a running
Steam client caused `forwardToRunningSteam` / `showRunningSteam` to discard the
pin's executable and use `Program Files (x86)/Steam/steam.exe`. That explains why
correcting a pin could leave the failure unchanged. GUI Steam launch and library
discovery contained additional copies of the same path assumption.

HighballKit now owns a single resolver: explicit per-bottle override, existing
valid Steam pins, then existing installations in both Program Files locations.
It keeps actual filename spelling and validates a readable Steam.exe with a PE
signature. Invalid explicit overrides fail instead of silently falling back.
Errors include the full attempted/resolved path and the Highball setting to fix.
Resolution and launch requests do not rewrite existing pins.

The Highball GUI stores the override through its environment settings and a
validated file picker: Settings > Environments > the environment's settings >
Advanced > Steam executable. In-bottle choices are relative to drive_c. Automatic
detection clears only the override. GUI launch, CLI pin launch, running-client
handoff, Steam shutdown, library discovery and verification use the resolver.

Game Installer now obtains a read-only JSON request with:

```sh
highball steam launch Games --uri steam://open/main --check
```

It shows the resolved executable, then dispatches the same supported operation
without `--check`. Highball resolves and validates again before launching.
Game Installer has no Steam-path preference. Install URIs use this same route.

## Play in Highball and authentication

`playOwnedGame` reaches the worker's `launch` action for registered games and
catalog entries with a Steam app ID. Before checking/creating a local game pin,
the worker asks Highball whether Steam records that app ID as fully installed at
the exact selected game folder, with a Windows Steam launcher. A matching game
uses `highball steam game <bottle> <appid> --install-path <folder>`, which launches
Steam with `-applaunch <appid>`. Steam performs its own ownership and sign-in
checks. Steam's manifest is installation evidence, not proof of ownership.

An unmatched local copy retains its existing pin flow and gets an explicit
explanation: opening Steam cannot grant it official Steam authentication. No
appmanifest, ownership, authentication token, or account state is fabricated or
patched. The first-click registration behavior remains available for local games.

## Helper build and deployment boundary

Game Installer's helper remains based on reviewed Highball 0.10.22 commit
`daa2095b9fd4abad0b9db7c57cacbd4d49a9e06e`, with the core resolver/CLI changes
carried in `macos/highball-steam.patch`. Packaging prefers the nested repository's
local Git objects, applies that patch to the pinned base in a temporary checkout,
and bundles the complete patched source, patch, license, and build identity.
The preview builder refreshes the helper when the patch changes. It does not
relabel an unpatched binary as supporting the new operation.

Readiness requires `steam_integration: steam-path-v1` and checks the new CLI help
interfaces as well as the existing binary hash/version checks. An old installed
helper is rejected with instructions to install the new bundled helper.

To use the GUI override, Highball.app must be rebuilt from the nested repository's
changes too. The Game Installer helper installer does not replace Highball.app.
An older Highball GUI does not know the new settings field and can drop it while
saving. The code and bundled helper were built here; installed apps and the
installed helper were deliberately left as found. The complete Game Installer
preview packaging/install script was syntax-checked, but was not run to replace
an installed app.

## Verification

- Game Installer backend: **51 tests passed**, including **5 cross-repository
  integration tests** run against the actual packaged release CLI. They use
  temporary metadata and capture dispatch; Wine is never started. Tests cover
  both Program Files locations, actual case, stale pins, persisted overrides,
  bottle isolation, invalid/missing paths, exact executable and
  `steam://open/main`, official game routing, and the worker's Play action before
  local pin registration. Missing-path failures dispatch nothing.
- Highball focused checks: **47 passed** (included in the broader run).
- Highball broader run: **581 passed, 16 skipped, 0 failures**. One additional,
  unrelated slow test was excluded:
  `RecipeEngineTests.testNoShippedEngineIsOfferedAnEarlierRevisionOfItsOwnLine`.
  The initial unrestricted run spent over two minutes repeatedly querying
  `sw_vers` in that existing engine-matrix test; it was stopped and the broader
  suite rerun with that one exclusion. The 16 skips are host/database-dependent
  existing tests. No claim is made that excluded/skipped checks passed.
- Helper provenance/readiness suite: **29 passed**, including rejection of the
  old helper build without the Steam integration.
- Swift builds: Highball CLI, Highball GUI, and Game Installer succeeded.
  The pinned 0.10.22 source plus integration patch also built successfully, and
  the release helper packaging script completed. The packaged helper passed the
  real read-only capability probe; its resolver source and patch match the
  nested repository implementation.
- Standalone Game Installer model checks passed for the 48-entry current catalog
  when compiled with `AppResources.swift`. The existing `check_models.sh` omits
  that source and fails before its checks; the equivalent direct compilation was
  used without changing that unrelated script.
- Modified shell scripts passed syntax checks; both repositories passed diff
  whitespace checks.

Reproduce the main checks from the Game Installer root:

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test --package-path Highball --filter 'SteamExecutableTests|SteamLibraryFoldersTests|BottleTests|EnvironmentTests|UpgradeInstallTests|UpgradeReplayTests|RecipeGameFolderTests'
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test --package-path Highball --skip 'RecipeEngineTests.testNoShippedEngineIsOfferedAnEarlierRevisionOfItsOwnLine'
HIGHBALL_STEAM_TEST_CLI="$PWD/macos/GameDownloaderMac/Sources/GameDownloaderMac/Resources/HighballHelper/highball" python3 -B -m unittest discover -s macos/backend/tests -p 'test_*.py' -v
python3 -B -m unittest discover -s spikes/highball -p 'test_*.py' -v
bash macos/package_highball_helper.sh
```

The `DEVELOPER_DIR` setting uses the installed full Xcode for XCTest; the selected
Command Line Tools toolchain did not provide XCTest.

A read-only `steam launch Games --check` also succeeded against the current live
Games bottle, and its bottle.json hash was unchanged. At verification time, this
Mac had Steam at the **x86** location and no file at the supplied Program Files
location, so the current live state differed from the original report. The
reported Program Files/Steam/Steam.exe-only layout was reproduced in isolated
fixtures and resolved correctly by both repositories.

## Remaining limits

- Real Steam cold start, running-client window behavior, sign-in, and gameplay
  have not been exercised against the user's bottle. Tests verify the generated
  request and path handling, not successful authentication or gameplay.
- The picker compiled as part of HighballApp; it was not clicked against a real
  bottle to avoid changing the user's configuration.
- Official game routing is deliberately conservative: the catalog must supply a
  valid app ID and Steam's Windows install manifest must match the exact folder.
  Missing/legacy manifest information is not treated as verified ownership.
- Launch dispatch still reports early failures and provides a log for later
  failures. A request accepted by the helper does not prove a window appeared.
- GUI replacement, consumer signing/notarization and release publication were
  outside this implementation. No issue or external publication was created.
- Existing unrelated catalog/tool edits in the Game Installer tree were left
  intact. The nested repository remains a separate Git working tree.

## Files changed

### Game Installer repository

- `.github/workflows/build.yml`
- `macos/GameDownloaderMac/Sources/GameDownloaderMac/ContentView.swift`
- `macos/GameDownloaderMac/Sources/GameDownloaderMac/HighballHelperInstaller.swift`
- `macos/GameDownloaderMac/Sources/GameDownloaderMac/HighballReadiness.swift`
- `macos/GameDownloaderMac/Sources/GameDownloaderMac/InstallQueue.swift`
- `macos/README.md`
- `macos/STEAM-PATH-FIX.md`
- `macos/backend/highball.py`
- `macos/backend/tests/test_backend.py`
- `macos/backend/tests/test_steam_integration.py`
- `macos/backend/worker.py`
- `macos/build_preview.sh`
- `macos/highball-steam.patch`
- `macos/package_highball_helper.sh`
- `spikes/highball/README.md`
- `spikes/highball/install_helper.sh`
- `spikes/highball/probe.py`
- `spikes/highball/test_probe.py`

### Highball repository

- `README.md`
- `Scripts/check-framegen.sh`
- `Sources/HighballApp/AppState.swift`
- `Sources/HighballApp/BottleView.swift`
- `Sources/HighballKit/Bottle.swift`
- `Sources/HighballKit/SteamExecutable.swift`
- `Sources/HighballKit/SteamLibrary.swift`
- `Sources/HighballKit/SteamRestart.swift`
- `Sources/HighballKit/Verifier.swift`
- `Sources/HighballKit/WineRunner.swift`
- `Sources/highball/main.swift`
- `Tests/HighballKitTests/EnvironmentTests.swift`
- `Tests/HighballKitTests/FrameGenerationTests.swift`
- `Tests/HighballKitTests/RecipeGameFolderTests.swift`
- `Tests/HighballKitTests/SteamExecutableTests.swift`
- `Tests/HighballKitTests/SteamFixture.swift`
- `Tests/HighballKitTests/SteamLibraryFoldersTests.swift`
- `Tests/HighballKitTests/UpgradeInstallTests.swift`
- `Tests/HighballKitTests/UpgradeReplayTests.swift`
- `docs/steam-executable.md`

## Delivery follow-up

The user subsequently requested committing, pushing, and delivering a usable app
update. Game Installer's main-branch workflow builds and publishes the updater's
Apple silicon ZIP after both platform builds pass. The workflow now also runs
the five real CLI integration tests against the packaged helper before producing
the app. Highball's upstream repository is not writable by this account; its
changes are delivered through a KevinAwesomeCoding fork. That fork does not
update Highball's official Sparkle feed. The Game Installer release still embeds
the patched 0.10.22 helper, so automatic Steam path discovery can be delivered
independently of an official Highball GUI release. The updated helper must be
installed from Game Installer Settings after updating the app.
