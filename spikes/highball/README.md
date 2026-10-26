# Highball integration feasibility spike

This directory is independent of `main.py`, the Windows UI, dependencies, and
release workflows. Its Python tools use the standard library. It contains no
Game-Downloader install, pin mutation, recipe application, or game launch
implementation. `readiness.py` checks Highball's configured home, installed
engines, and bottles after the helper identity check succeeds.

## Recommendation and verified upstream boundary

Use an **unmodified, version-matched standalone Highball CLI**, shipped as a
separate signed helper inside the eventual Mac installer, and call it with an
argument array. Do not assume a `highball` command exists on PATH. Keep Highball
responsible for engines, bottles, setup, renderer licenses, and Wine launches.

Verified on 2026-10-25 against official release **v0.10.22**, commit
`daa2095b9fd4abad0b9db7c57cacbd4d49a9e06e`:

| Route | What it supports | Assessment |
| --- | --- | --- |
| Standalone CLI | External executable pins, persistent arguments/environment/renderer, recipe application, running pins | Recommended complete boundary; needs our own build, signing, distribution, compatibility checks, and GPL compliance. |
| `highball://` play links | Launch an existing Steam/Epic entry or a pin identified by bottle and UUID; launcher links also exist | Useful later for GUI handoff. Cannot create/configure pins; pin UUID is not exposed by CLI listing. External requests require confirmation without Highball's per-install token. Do not read/copy that token to bypass confirmation. |
| GUI file-open / manual setup | Highball can open Windows programs and let the user add/configure them | Supported fallback, but not a complete automated recipe interface. |
| HighballKit | A public Swift package library used by both front ends | In-process integration rather than a running-app API; increases coupling and GPL considerations. Not needed for this spike. |
| Private bottle JSON or UI automation | Could manipulate implementation details | Do not use for automated configuration. No documented general configuration RPC/HTTP/AppleScript API was found in the inspected release. |

The official packaging script builds `HighballApp` and copies it into
`Highball.app/Contents/MacOS/Highball`; it does not bundle the `highball` CLI
product. On this Mac the app is `/Applications/Highball.app`, version 0.10.22.
Only the GUI executable was found in that bundle; no CLI was found on the
current PATH or at `/opt/homebrew/bin/highball` or `/usr/local/bin/highball`.
This does not claim that an unrelated source checkout elsewhere has no CLI.
A local development helper is now installed outside the repository at
`~/Library/Application Support/Game-Downloader/HighballHelper/0.10.22/`.
It is built from the reviewed commit below and ad-hoc signed without a Developer
ID. The adjacent `build-manifest.json` binds the local probe to its source
commit and installed binary hash. This is not a distributable signed/notarized
helper. Rebuild/reinstall it with `bash spikes/highball/install_helper.sh` from
the repository root; the script checks the app version, source commit, code
signature, and helper capabilities before replacing the installed copy.

The package declares Swift tools 5.10 and macOS 14. Highball documents Apple
Silicon/macOS 14+; engine-specific requirements and Rosetta still need checking
before actual game execution. The new native arm64 engine is not required for
this integration spike.

## Supported future operations (not executed by this spike)

```text
highball pin add <bottle> <name> <absolute-executable-path>
highball pin list <bottle>
highball pin set <bottle> <pin> args -- <argument> ...
highball pin set <bottle> <pin> renderer <wined3d|dxmt|d3dmetal|dxvk|default>
highball pin set <bottle> <pin> env KEY=VALUE
highball recipe apply <bottle> <absolute-recipe.json>
highball run <bottle> <pin-name>
```

`pin add` rejects duplicate names (case-insensitive); there is no pin remove or
path-update subcommand in this release. `pin set` changes args/renderer/env,
not executable paths. Empty `KEY=` removes that environment variable. Saved
settings are applied by `run`; additional arguments are appended. These are
not idempotent upsert commands. Production integration needs conflict handling
and serialized mutations coordinated with the GUI, whose settings may be stale.

Recipe IDs depend on checkout-relative lookup; use explicit files and resolve
recipe dependencies/resources deliberately. `run` may perform bottle preflight
and engine-fact adoption, so it is **not** a read-only readiness check. Even
The optional readiness check runs only `config`, `engine list`, and
`bottle list`. `engine list` invokes the installed engine's Wine version
executable (`wine --version`); it does not launch a game or modify a bottle.
It has a timeout and fails closed if command output changes. No install, pin,
recipe, renderer-license, bottle, or game-launch command is run.

## Probe contract

```sh
python3 -B spikes/highball/probe.py
python3 -B spikes/highball/probe.py \
  --app /Applications/Highball.app \
  --helper /absolute/path/to/trusted/highball \
  --manifest /absolute/path/to/helper-build.json
python3 -B -m unittest discover -s spikes/highball -p 'test_*.py' -v
python3 -B spikes/highball/readiness.py \
  --app /Applications/Highball.app \
  --helper /absolute/path/to/trusted/highball \
  --manifest /absolute/path/to/helper-build.json
```

Custom app locations use `--app`. Default detection checks `/Applications` and
`~/Applications` using bundle metadata; it never opens the GUI. Helper discovery
is intentionally explicit, with no PATH fallback and no downloads or builds.

Highball 0.10.22 does **not** declare a CLI version string/`--version` flag.
Therefore the trusted build pipeline must supply provenance alongside the
finished binary, after signing (which changes its hash):

```json
{
  "schema_version": 1,
  "highball_version": "0.10.22",
  "source_commit": "daa2095b9fd4abad0b9db7c57cacbd4d49a9e06e",
  "binary_sha256": "REPLACE_WITH_SHA256_OF_THE_FINAL_HELPER_BINARY"
}
```

A sidecar and hash bind a **declared build identity** to bytes, not proof that
those bytes were built from the declared source. This developer probe only
accepts caller-trusted binaries/manifests. A consumer build must authenticate
both through its signed/notarized bundle and verified distribution pipeline;
do not use arbitrary downloaded sidecars as a trust mechanism. Updating the
reviewed version/commit requires a new upstream review and validation.

The probe rejects the app GUI (including symlinks), missing/unexecutable helpers,
unknown provenance, version mismatch, and hash mismatch **before execution**.
After those checks, the entire subprocess allowlist is:

```text
pin add --help
pin list --help
pin set --help
run --help
config --help
```

Each request uses `shell=False`, a five-second timeout, a temporary working
directory, and a temporary `HIGHBALL_HOME`. This is defense in depth, not a
security sandbox for arbitrary executables. Only stdout help is accepted, with
required usage/settings tokens. Unsupported help, timeout, loader errors and
nonzero exit fail closed. Temporary storage is removed on exit.

`probe.py` output is JSON. Exit 0 means **helper interface accepted**; it does
not establish runtime readiness. `readiness.py` first repeats that helper
identity/capability check, then queries the active Highball home and parses the
engine and bottle lists. It reports missing/incomplete engines, unknown Wine
versions, unavailable renderers, licenses not accepted, invalid bottle-to-engine
pairings, and damaged bottles. Exit 0 means every listed bottle has a complete
engine and available configured renderer. Exit 2 means unavailable, unaccepted,
or needing attention. The report explicitly notes that `engine list` executed
Wine's version executable. No Highball configuration or game files are changed.

## Verification performed on this Mac

- Built the unmodified release CLI in temporary storage with Apple Swift 6.3.3,
  using the exact commit above and unchanged `Package.resolved`. The source
  checkout remained clean. Only the CLI product was built; Highball.app was not
  rebuilt or replaced. Upstream unused-result compiler warnings were non-fatal.
- Installed the arm64 CLI at
  `~/Library/Application Support/Game-Downloader/HighballHelper/0.10.22/highball`.
  It passed all five help checks through `probe.py`, with exit 0 and
  `helper_ready=true`. The adjacent manifest records SHA-256
  `412470466f70e28f8f85771bc1acef83d7d742bbd99739c6dba80526f973398f`.
- The live `readiness.py` check returned `ready`: Highball resolved its usual
  Application Support home; engine `x64-sikarugir10.0_6-r28` was complete and
  reported Wine 10.0; the `Games` bottle loaded with D3DMetal and had no pairing
  issues. It reported one engine, one bottle, nine pins, two recipes, and no
  damaged bottles. `engine list` ran `wine --version`; no game was launched and
  no Highball configuration was changed.
- The real CLI then passed a disposable integration smoke test: created a
  temporary Highball home and bottle, pinned that bottle's built-in `cmd.exe`,
  saved `/c exit 0`, and ran the pin successfully with exit 0. The bottle was
  stopped and the temporary home removed. The user's `Games` bottle was not
  used or changed.
- Root `--help` confirmed the absence of a `--version` flag and created no files
  in its temporary Highball home.
- Built binary: arm64, deployment minimum macOS 14.0, SDK 26.5; linked libraries
  were system/Swift libraries and system frameworks, not Sparkle or paths into
  the checkout. This local build is ad-hoc signed with no Developer ID/team;
  it is **not** a distributable signed/notarized helper.
- The temporary source checkout was removed after testing. The helper and its
  generated manifest remain in the Application Support path above; neither is
  committed to the repository. Rebuilds/signing may produce different bytes.
- All **28 unit tests** passed, covering missing/invalid apps and helpers, GUI
  and symlink rejection, provenance/version/hash failures, missing capabilities,
  process errors/timeouts, JSON exit behavior, and the subprocess help allowlist.
- Readiness parser/subprocess tests cover complete, missing and incomplete
  engines, unaccepted renderer licenses, valid and damaged bottles, changed
  output, timeouts, and the exact three-command read-only allowlist.
- Before installation, a run without `--helper` detected the installed app and
  returned `helper_missing` (exit 2). The GUI plist/executable, Highball home
  configuration, and existing `Games/bottle.json` retained their original
  hashes after testing.
- No game, engine setup, pin mutation, bottle mutation, renderer license
  acceptance, or actual game launch was performed. GUI refresh/concurrent writes
  and game launch through the helper remain untested.

## Packaging and licensing work before distribution

Build the CLI product from the exact reviewed commit, using its pinned
`Package.resolved` dependencies (`swift build -c release --product highball`).
Keep a build record including source/dependency commits, toolchain, target,
deployment minimum, final binary digest and signing identity. Inspect actual
runtime library dependencies; do not assume GUI frameworks/resources or
development-machine paths will work in a distributed CLI. Test on a clean Mac.
For native arm64 engines, upstream can locate the installed app's signed
WineLoader helper; custom app locations may need `HIGHBALL_WINELOADER`. Preserve
that upstream boundary instead of copying or modifying its entitled loader.
Also validate microphone/local-network permission attribution for CLI-launched
Wine processes before claiming parity with launching from Highball's GUI.

The upstream CLI, app and HighballKit are GPL-3.0. A redistributed helper needs
copyright/license notices and corresponding source, dependencies as required,
and build scripts/instructions for the exact shipped version under GPL section
6. Preserve notices and identify modifications if any. Hosting a moving upstream
branch is not a substitute for maintaining corresponding source availability.
Review whether a proposed combined distribution is an aggregate or a combined
work; a subprocess boundary alone is not a blanket licensing exemption.

Highball's recipes/database are separately CC0. This spike vendors no upstream
code, helper binary, engine, renderer, or recipe data. Do not redistribute
D3DMetal as part of the helper or accept Apple's license automatically. Review
third-party notices and the existing PyQt distribution license separately.

Before adding mutation/launch code: supply a signed helper build, confirm update
coordination with Highball, decide how to avoid stale GUI writes, and establish
engine/bottle readiness checks. Then test against a new disposable bottle only
after explicit authorization; preserve the existing `Games` bottle.

## Official sources

- [Current release](https://github.com/gauthierpiarrette/highball/releases/latest)
- [Package targets](https://github.com/gauthierpiarrette/highball/blob/daa2095b9fd4abad0b9db7c57cacbd4d49a9e06e/Package.swift)
- [CLI implementation](https://github.com/gauthierpiarrette/highball/blob/daa2095b9fd4abad0b9db7c57cacbd4d49a9e06e/Sources/highball/main.swift)
- [GUI packaging](https://github.com/gauthierpiarrette/highball/blob/daa2095b9fd4abad0b9db7c57cacbd4d49a9e06e/Scripts/make-app.sh)
- [Play links](https://github.com/gauthierpiarrette/highball/blob/daa2095b9fd4abad0b9db7c57cacbd4d49a9e06e/Sources/HighballKit/PlayLink.swift)
- [GUI URL handling](https://github.com/gauthierpiarrette/highball/blob/daa2095b9fd4abad0b9db7c57cacbd4d49a9e06e/Sources/HighballApp/AppState.swift)
- [Home resolution](https://github.com/gauthierpiarrette/highball/blob/daa2095b9fd4abad0b9db7c57cacbd4d49a9e06e/Sources/HighballKit/Paths.swift)
- [GPL-3.0](https://github.com/gauthierpiarrette/highball/blob/daa2095b9fd4abad0b9db7c57cacbd4d49a9e06e/LICENSE)
