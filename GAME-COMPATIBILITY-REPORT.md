# Game-Downloader compatibility report

**Compiled:** 2026-10-26
**Catalog:** `games.json` (47 entries)
**Primary test setup in the saved Mac notes:** Apple M5, 16 GB RAM, macOS 26.5.1, Highball 0.10.22, Games bottle, Wine engine `x64-sikarugir10.0_6-r28`. Most documented launches used Highball’s D3DMetal setting; Buckshot Roulette used Vulkan through MoltenVK. Results describe this setup and the specific downloaded builds, not every Mac or future build.

## How to read this report

- **Opened/menu** means there is evidence the game process or a rendered game screen started. It does not imply a full playthrough.
- **Friends UI** means a friend-list or invite-selection panel opened. It does not prove an invitation reached another person.
- **Multiplayer/invite works** is marked only where Kevin explicitly reported a working multiplayer session or invitation.
- **Unverified** means the available notes do not establish the result. Where a checklist message only said a title was “recorded,” I have not guessed the outcome.
- “Failed/blocked” describes the observed attempt, not a universal incompatibility verdict.

## Results for all catalog games

| # | Game | Launch / screen result | Friends, invitations, multiplayer | Notes |
|---:|---|---|---|---|
| 1 | Gang Beasts | Opened successfully. | Not verified. | No confirmed friend-list or invitation result in the checklist notes. |
| 2 | MIMESIS | Unity loaded `MainMenuScene`; the process stayed running. | Kevin reported multiplayer works. Specific invite flow not documented. | D3DMetal; per-game DLL-loading settings were saved. Log included `KOS Login Failed: SteamServerError` despite the multiplayer report. |
| 3 | Human Fall Flat | Opened. | Steam friends window opened; sending/accepting an invite was not verified. | No full co-op session documented. |
| 4 | Gamble With Your Friends | Opened. | Friend-list contents and invite not verified. | — |
| 5 | MECCHA CHAMELEON | Launched after a per-game DLL-loading adjustment; Kevin confirmed it worked. | Online play and invitations not tested. | D3DMetal. Initial error: “Failed to get OnlineFix interface.” A per-game DLL-loading adjustment preceded the successful relaunch. Args recorded: `-windowed -ResX=1280 -ResY=720`. |
| 6 | Among Us | Opened successfully. | Not verified. | — |
| 7 | Hardware Store Simulator | Opened. | Friends window opened; invite not verified. | — |
| 8 | Liar’s Bar | Launch did not produce a usable game window in the recorded attempt. | Not tested. | The attempt happened during a period when macOS was also failing to create processes; no stable compatibility conclusion. |
| 9 | Backseat Drivers | Blocked before game launch. | Not tested. | Its Steam install link did not produce an installable game in the tested flow. |
| 10 | Night Shippers | Opened. | Steam friends window opened; invite not verified. | — |
| 11 | Rewind 99 | Opened. | Not verified. | — |
| 12 | Cut That Wire | Reached its main menu. | Kevin reported multiplayer works; exact invite flow not separately recorded. | D3DMetal setting; Unity reported Direct3D 11 feature level 11.1. Unity Authentication logged HTTP 401 `invalid token`; this did not prevent the menu or the user-reported multiplayer result. Args recorded: windowed 1280×720; per-game DLL-loading settings were saved. |
| 13 | Golf Gambit | Launch failed to produce a game window in the recorded attempt. | Not tested. | — |
| 14 | Keep Talking and Nobody Explodes | Opened. | Friends window opened; friend-list contents and invite not verified. | — |
| 15 | Knights End | Rendered the message “You have brought shame upon your King for the last time.” Kevin said it worked; reaching the main menu was not confirmed in the saved note. | Not tested. | D3DMetal; process exited after about 68 seconds. An unsupported shader-barrier warning was logged. Windowed 1280×720 and per-game DLL-loading settings were recorded. |
| 16 | Mage Arena | Opened. | Steam friends window opened; invite not verified. | — |
| 17 | The Jackbox Party Pack 11 | Opened. | Friends window opened; invite action not verified. | — |
| 18 | R.E.P.O. | Opened. | Friends window opened; invite not verified. | — |
| 19 | Buckshot Roulette | Main menu and multiplayer lobby opened after starting Steam in the Games bottle first. | Kevin reported Lobby ID sharing works. The Steam “Invite Friends” button did nothing in the observed attempt; a live remote join was not independently confirmed. | Vulkan through MoltenVK. Steamworks initialized and lobby creation was logged. A prior “Steam is not launched” popup cleared after Steam was started. |
| 20 | Midnight Heist | Opened. | Friends window opened; invite not verified. | — |
| 21 | Backrooms Rec | Recorded as a launch failure. | Not tested. | An earlier attempt was delayed by host process exhaustion; the checklist later recorded launch failure without preserving a specific error in the summary. |
| 22 | Lethal Company | Titled game window and Unity graphics initialization were observed. | Not verified. | The saved result establishes a successful open check, not full gameplay or multiplayer. |
| 23 | Airport Security Sucks! | Game and credits windows were observed. | Not verified. | — |
| 24 | The Game of Life 2 | Download and extraction completed, but the game test was blocked before verification. | Not tested. | macOS process exhaustion prevented a reliable launch check at the time. |
| 25 | Fears to Fathom – Scratch Creek | Reached a playable main menu after repairs. | Steam’s “Choose Friend to Invite” dialog opened and displayed friends. A sent invite, joining, co-op, and voice were not tested. | Invite initially threw an exception because localization data was missing. A local assembly workaround removed the missing text lookups while preserving the Steam invite call. D3DMetal with Direct3D 11. |
| 26 | Funnel Runners | Outcome not preserved in the checklist messages available for this report. | Not verified. | The title was marked as recorded, but the actual launch result was not included in the retained summary. |
| 27 | Forest Escape: Last Train | Playable main menu confirmed; Kevin said the game works. | Kevin confirmed multiplayer works. | D3DMetal; per-game DLL-loading settings were saved. A brief menu capture showed low frame rate; sustained performance was not tested. |
| 28 | The Backrooms: Rescue Expedition | Reached a rendered in-game screen. Kevin later said it worked; the game folder was removed afterward at his request. | Friend List panel opened but appeared empty. Co-op/invites were not verified. | D3DMetal. Logs included `Failed to load "SDL3.dll"` and a GStreamer video assertion; their effect was unknown. |
| 29 | Happy’s Humble Burger Cult | Blocked by an installer/archive issue before a verified game launch. | Not tested. | The checklist recorded the title as blocked; no menu result was retained. |
| 30 | Scamper | Marked as recorded, but the actual launch result was not preserved in the checklist messages available here. | Not verified. | — |
| 31 | Pyramidion | Launch failure recorded. | Not tested. | No more specific error was preserved in the summary. |
| 32 | Shift At Midnight | Marked as recorded, but the actual launch result was not preserved in the checklist messages available here. | Not verified. | — |
| 33 | Big Walk | No main menu reached. The observed attempt showed a “Steamworks Fix Activation” prompt over a black screen. | Not tested. | Kevin has now asked to skip this title; it is excluded from further testing. |
| 34 | Party Animals | Kevin ultimately confirmed the game works / reaches the game screen. | Invite Friends panel opened, but the friend list appeared empty; invitation success remains unverified. | Initial attempts had a missing `UnityPlayer.dll` from an incomplete multipart extraction. The installer’s multipart handling was fixed and verified separately. Do not treat that installer fix as proof that every game archive is complete. |
| 35 | Happy Wheels | Marked as recorded, but the actual launch result was not preserved in the checklist messages available here. | Not verified. | — |
| 36 | Dub Together | Testing was underway and the title was marked as recorded; the actual result was not preserved in the checklist messages available here. | Not verified. | — |
| 37 | How to Fish | Marked as recorded, but the actual launch result was not preserved in the checklist messages available here. | Not verified. | — |
| 38 | Machine Party | Marked as recorded, but the actual launch result was not preserved in the checklist messages available here. | Not verified. | — |
| 39 | Mimic Party | Marked as recorded, but the actual launch result was not preserved in the checklist messages available here. | Not verified. | — |
| 40 | BOMBANANA! | Marked as recorded, but the actual launch result was not preserved in the checklist messages available here. | Not verified. | — |
| 41 | Scam With Your Friends | Kevin confirmed it works after adding native-first DLL overrides to its Highball pin. The successful run used DXMT. | Friends, invitations, and multiplayer were not verified. | The initial DXMT launch exited 53. The installed folder contained `winmm.dll`, `StubDRM64.dll`, `OnlineFix64.dll`, `SteamOverlay64.dll`, and `steam_api64.dll`; adding native-first overrides for those five let the game stay running. One Unity log later showed HTTP 401 responses from its auth endpoints, so online authentication was not established by this launch. The Mac installer now discovers these known DLL collisions when adding a game to Highball, including with “Choose .exe & add.” |
| 42 | How To Train Your Cock | Marked as recorded, but the actual launch result was not preserved in the checklist messages available here. | Not verified. | — |
| 43 | Lucid Cats | Reached its hosting flow. Kevin confirmed hosting worked after native-first DLL overrides were applied. | Host flow works; joining, invitations, and voice were not tested. | Steam was already running in the same Games bottle. The Highball pin lacked overrides for `winmm.dll`, `OnlineFix64.dll`, and `SteamOverlay64.dll`; setting all three to native-first fixed hosting. The installer had skipped setup because this exact archive build was not yet recorded. |
| 44 | MOGGED: Looksmaxx or Die | Kevin confirmed the game/menu works. | Kevin confirmed the friend-invite check works. | This is user-confirmed; the detailed steps and renderer were not present in the retained summary. |
| 45 | Needle In A Haystack | **Storage issue checked off:** v1.2.7 reaches the Lobby with Steam initialized, and the campaign-storage initialization error is cleared by a verified local workaround. The game’s own save store passed an isolated save/update/backup/reload round trip. | Real campaign gameplay and multiplayer remain unverified. | Investigation and local fix are complete for this build. Unity Mono returned a corrupted cached Windows error when checking an absent save candidate; redirecting two file-attribute checks to a helper that reads the native error directly restores storage availability. This remains a local build-specific workaround, not an automatic installer recipe. |
| 46 | Cheater's Table | Kevin reports that the game works. | Multiplayer, invites, and voice were not specified. | User-confirmed; exact setup and tested gameplay scope were not specified. |
| 47 | Supermarket Chaos | Kevin reports that the game works. | Multiplayer, invites, and voice were not specified. | User-confirmed; exact setup and tested gameplay scope were not specified. |

## Detailed fixes and useful setup records

These are the games with the clearest saved setup recipes or troubleshooting history. Settings are specific to the recorded builds and Games bottle.

### MECCHA CHAMELEON

- Base archive: 4,435,111,962 bytes; patch archive: 27,388,066 bytes. The saved notebook contains SHA-256 values and source paths.
- Add `PenguinHotel-Win64-Shipping.exe` from `Chameleon/Binaries/Win64` to Highball.
- Use D3DMetal, `-windowed -ResX=1280 -ResY=720`, and the per-program DLL-loading setting recorded for this test.
- This resolved the observed “Failed to get OnlineFix interface” startup error. Kevin confirmed the relaunch worked. Full gameplay and online play were not tested.

### Fears to Fathom – Scratch Creek

- Reached a playable menu after the archive repair and a local compatibility change.
- Initial invite-button click caused an exception while the game read missing localization strings. The backed-up assembly change bypassed those lookups; Steam’s friend chooser then opened and showed friends.
- Settings recorded: D3DMetal bottle, windowed 1280×720, per-game DLL-loading settings.
- Invitation delivery, joining, co-op, and voice remain unverified.

### Buckshot Roulette

- Start Steam inside the Games bottle before starting the game. This cleared “Steam is not launched.”
- Vulkan through MoltenVK initialized and the multiplayer lobby appeared. Kevin says Lobby ID sharing works; the Steam invite button did nothing in the recorded attempt.
- The saved Highball pin contains per-game DLL-loading settings. A live join and invitation acceptance were not independently verified.

### Lucid Cats

- Build 2.4 reached the hosting flow. Steam was confirmed running in the same Highball Games bottle; the failure was not caused by Steam being closed.
- The folder contained `winmm.dll`, `OnlineFix64.dll`, and `SteamOverlay64.dll`, but the Highball pin had no `WINEDLLOVERRIDES`. Setting these three to `native,builtin` fixed the host flow; Kevin confirmed it worked.
- The installer now has an exact archive size/SHA-256 build gate for this main archive and repair archive. It applies the universal D3DMetal/windowed setup and detects these DLLs for native-first overrides before adding the game to Highball.
- Joining, invite delivery, voice, and extended gameplay have not been tested.

### Needle In A Haystack

- Earlier direct launches failed because no App ID was available. `steam_appid.txt` containing `5085740` remains in the game folder. With native-first overrides for `winmm`, `steam_api64`, `OnlineFix64`, and `SteamOverlay64`, current logs confirm Steam ready as the signed-in user, networking ready, and the authored Lobby loaded. Earlier Steam initialization failures are historical; the remaining failure was campaign storage.
- On 2026-10-26, v1.2.7 (Unity `6000.3.21f1`) was tested in the Games bottle with `x64-sikarugir10.0_6-r28` and DXMT. The profile directory was writable but empty. A temporary logging-only assembly change captured the exact failure: `FileSystem.GetAttributes` → `File.GetAttributes` → `JsonCampaignSaveSlotsStore.IsRegularFilePresent` → `LoadNewestValidCandidate`. It failed on the absent `campaign-save-slots.json.tmp` before parsing, writing, flushing, or replacing save data.
- A probe inside this game's Unity process confirmed `GetFileAttributesW` returned invalid attributes and native `GetLastError` returned `2` (file not found), while `Marshal.GetLastWin32Error` returned an unrelated value. Managed `File.GetAttributes` therefore threw `IOException` instead of the missing-file exceptions the loader handles. Existing-file attributes, durable writes, moves, and atomic replacement succeeded in a separate diagnostic folder. A [reported Wine/Unity failure](https://github.com/mikaelhug/Silo/issues/6) describes similar cached-error symptoms; the local probe, rather than that report, establishes this result.
- A local workaround redirects exactly two `File.GetAttributes` calls in `Assembly-CSharp.dll` (the regular-file checks in `JsonCampaignSaveStore` and `JsonCampaignSaveSlotsStore`) to the added `NeedleHighballIO.dll`. The helper reads the native Windows error directly, with the error-reader import resolved before the failing call. It preserves missing-file/directory and access-denied exceptions and throws on other failures. Save serialization, write/flush/replace behavior, profile identity, and Steam configuration remain unchanged.
- Verification inside the game reported `persistenceAvailable=True` with an empty persistence error. In a separate diagnostic directory, the game's own save store serialized a clone of its genuine initialized state and passed: empty load, first save/load, second save, backup creation, and reload through a fresh store at transaction revision `2`. No campaign data was invented or written to the user's profile. Temporary logging and verification hooks/DLLs were removed from the final install. A subsequent clean launch reached the Lobby with Steam ready and no campaign-storage errors in `Player.log`.
- The original and patched assemblies, helper source, hashes, pre-change save-folder copy, and diagnostic logs are retained locally under `~/Library/Application Support/Game-Downloader/Diagnostics/Needle-20261026-194923/`. Original assembly SHA-256: `0bdefa08bf3e28b275be86bb39164b7bb1d75ba6cd131d4a9b534cd05e403922`. To roll back with the game closed, restore `Assembly-CSharp.original.dll` as the installed `Assembly-CSharp.dll` and remove only the added helper. An update/reinstall can replace the workaround; it must not be copied to other builds.
- The user's profile remained empty. Actual gameplay-created campaign persistence, cloud synchronization, hosting, joining, invitations, and voice remain unverified. Computer Use confirmed Highball reports Steam running, but the Wine game window was not independently selectable; no visual confirmation of the warning's disappearance is claimed. This workaround is not yet an automatic installer recipe.

### Cut That Wire

- Main menu reached. The game used Direct3D 11 feature level 11.1 through the D3DMetal bottle setting.
- Windowed 1280×720; per-game DLL-loading settings.
- Unity Authentication logged HTTP 401 `invalid token`; Kevin still reported multiplayer works.

### Knights End

- Rendered a startup message and Kevin said it worked, but the saved evidence does not confirm a main menu. Process exited after about 68 seconds.
- Windowed 1280×720; per-game DLL-loading settings; D3DMetal.
- Multiplayer and gameplay remain untested.

### Forest Escape: Last Train

- GameInstaller inside Highball completed the catalog install. Main menu showed Create Game, Join Friend, and Public Games; Kevin confirmed multiplayer works.
- D3DMetal; per-game DLL-loading settings were saved.
- A short menu capture was around 14 FPS. Long-session performance, audio, saving, and full gameplay were not tested.

### MIMESIS

- Unity loaded `MainMenuScene`; Kevin reported multiplayer works.
- D3DMetal; per-game DLL-loading settings were saved.
- The log reported a D3D11 multithread-protection issue, software video decoding, and `KOS Login Failed Error Code : SteamServerError`; these did not prevent the user-reported multiplayer result.

### The Backrooms: Rescue Expedition

- Reached an in-game screen, but the friend list was empty. Online co-op and invitation delivery were not verified.
- D3DMetal; the first launch lacked the saved per-game DLL-loading settings. They were later configured, but the saved detailed note says a corrected relaunch was still pending verification.
- Kevin requested deletion after the test; the game folder was removed. Preserve the compatibility note even though the installed files are gone.

### Party Animals

- Initial launch attempts exposed an incomplete multi-part extraction: `UnityPlayer.dll` was missing. The installer’s extraction handling was updated; its backend verification passed 34 tests and the rebuilt app’s code-signature check passed.
- Kevin later confirmed the game works. The invite panel opened but displayed no friends, so actual invitation success is not established.

## Overall takeaway

There is evidence that many catalog games opened, and some reached their main menu or multiplayer screens. The strongest multiplayer confirmations are for **MIMESIS, Cut That Wire, and Forest Escape: Last Train**; Kevin also reported **Buckshot Roulette Lobby ID sharing** works. **MOGGED** has a user-confirmed working friend-invite check. Several other games only had their friend window opened, and many checklist entries lack a retained detailed outcome. Empty friend lists, an opened Steam picker, or a lobby screen are not the same as a successful invitation and join.

The earlier per-game notes are in `/Users/kevinanil/Documents/Game-Downloader Mac compatibility/`. This report intentionally leaves unknown results marked unknown rather than filling gaps with assumptions.

## Installer recipe progress

The Mac installer’s build-matched recipe catalog now includes **MECCHA CHAMELEON, Cut That Wire, Buckshot Roulette, Party Animals, and Lucid Cats**. Buckshot keeps its normal Vulkan path and shows a reminder to start Steam in the bottle. Party Animals’ exact multipart build is checked for `UnityPlayer.dll` and its data directory before installation is published. Lucid Cats’ recorded archives are hash-gated; the installer detects its `winmm.dll`, `OnlineFix64.dll`, and `SteamOverlay64.dll` collisions and prepares native-first overrides. Scratch Creek still relies on extra runtime/content and a local game-file patch, while several other recorded setups are not yet automated.
