"""Isolated Highball helper feasibility probe; never configures or launches games."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
from xml.parsers.expat import ExpatError


REVIEWED_VERSION = "0.10.22"
REVIEWED_COMMIT = "daa2095b9fd4abad0b9db7c57cacbd4d49a9e06e"
BUNDLE_ID = "app.highball.Highball"

# Only these help requests can be executed. No bottle names or game paths enter argv.
# Usage checks deliberately fail closed if upstream changes the command interface.
HELP_CHECKS = {
    "pin_add": (("pin", "add", "--help"), ("pin add <bottle> <name> <path>",)),
    "pin_list": (("pin", "list", "--help"), ("pin list <bottle>",)),
    "pin_settings": (
        ("pin", "set", "--help"),
        ("pin set <bottle> <pin> <setting>", "args", "renderer", "env", "KEY=VALUE"),
    ),
    "steam_launch": (("steam", "launch", "--help"), ("steam launch <bottle>", "--uri", "--check")),
    "steam_game": (("steam", "game", "--help"), ("steam game <bottle>", "--install-path", "--check")),
    "run": (("run", "--help"), ("run <bottle> <program>", "--renderer", "--verbose")),
    "config": (("config", "--help"), ("highball config",)),
}


def detect_app(candidates: list[Path] | None = None) -> dict:
    """Read bundle metadata only. Custom app locations may be supplied explicitly."""
    if candidates is None:
        candidates = [Path("/Applications/Highball.app"), Path.home() / "Applications/Highball.app"]
    errors = []
    for candidate in candidates:
        try:
            app = candidate.expanduser().resolve()
            if not app.exists():
                continue
            with (app / "Contents/Info.plist").open("rb") as stream:
                info = plistlib.load(stream)
            if not isinstance(info, dict) or info.get("CFBundleIdentifier") != BUNDLE_ID:
                raise ValueError("bundle identifier is not Highball")
            version = info.get("CFBundleShortVersionString")
            executable = info.get("CFBundleExecutable")
            if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise ValueError("missing or invalid app version")
            if not isinstance(executable, str) or Path(executable).name != executable:
                raise ValueError("missing or invalid bundle executable")
            gui = app / "Contents/MacOS" / executable
            if not gui.is_file():
                raise ValueError("GUI executable is missing")
            return {"status": "found", "path": str(app), "version": version, "gui": str(gui.resolve())}
        except (OSError, ValueError, RuntimeError, ExpatError, plistlib.InvalidFileException) as exc:
            errors.append(f"{candidate}: {exc}")
    return {"status": "invalid" if errors else "missing", "errors": errors}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe(apps: list[Path] | None = None, helper: Path | None = None,
          manifest: Path | None = None, *, runner=None) -> dict:
    """Check declared build identity and help, not engine/bottle/game readiness.

    The manifest must come from a trusted build. A matching hash binds its claims
    to a file; it does not authenticate a caller-supplied binary or sidecar.
    """
    report = {
        "status": "app_missing", "helper_ready": False,
        "scope": "helper interface only; engines, bottles and games were not checked",
        "app": detect_app(apps), "capabilities": {},
    }

    def stop(status, detail):
        report.update(status=status, detail=detail)
        return report

    app = report["app"]
    if app["status"] != "found":
        return stop("app_" + app["status"], "Select an installed Highball.app.")
    if helper is None:
        return stop("helper_missing", "Supply an explicit standalone CLI; PATH and the GUI are not used.")
    try:
        helper = helper.expanduser().resolve(strict=True)
    except FileNotFoundError:
        return stop("helper_missing", "Standalone helper does not exist.")
    except (OSError, RuntimeError) as exc:
        return stop("helper_invalid", str(exc))
    report["helper"] = {"path": str(helper)}
    if str(helper) == app["gui"]:
        return stop("gui_rejected", "The Highball GUI executable is not the CLI.")
    if not helper.is_file():
        return stop("helper_missing", "Standalone helper does not exist.")
    if not os.access(helper, os.X_OK):
        return stop("helper_not_executable", "Helper is not executable; permissions were not changed.")
    if manifest is None:
        return stop("provenance_required", "CLI has no upstream version flag; trusted build metadata is required.")
    try:
        metadata = json.loads(manifest.expanduser().read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError("manifest must be an object")
        expected = {
            "schema_version": 1, "highball_version": REVIEWED_VERSION,
            "source_commit": REVIEWED_COMMIT, "steam_integration": "steam-path-v1",
        }
        if any(metadata.get(key) != value for key, value in expected.items()):
            return stop("unreviewed_build", "Build metadata does not match the reviewed upstream release.")
        if app["version"] != metadata["highball_version"]:
            return stop("version_mismatch", "App and helper build versions differ; no helper was executed.")
        digest = _sha256(helper)
        if metadata.get("binary_sha256") != digest:
            return stop("hash_mismatch", "Helper bytes do not match the build manifest.")
        report["helper"].update(version=metadata["highball_version"], source_commit=REVIEWED_COMMIT,
                                sha256=digest, version_basis="build manifest plus binary hash")
    except (OSError, ValueError) as exc:
        return stop("invalid_manifest", str(exc))

    run = runner or subprocess.run
    # Defense in depth, not a sandbox: only reviewed help requests are safe here.
    # HIGHBALL_HOME overrides any configured production home. No app data is read.
    with tempfile.TemporaryDirectory(prefix="highball-help-probe-") as temporary:
        env = dict(os.environ, HIGHBALL_HOME=str(Path(temporary) / "data"),
                   LC_ALL="C", TERM="dumb", NO_COLOR="1")
        for capability, (arguments, required) in HELP_CHECKS.items():
            try:
                result = run([str(helper), *arguments], cwd=temporary, env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, errors="replace", timeout=5,
                             shell=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return stop("probe_failed", f"{capability}: {exc}")
            if result.returncode != 0:
                return stop("probe_failed", f"{capability}: exit {result.returncode}: {result.stderr[:500]}")
            output = " ".join(result.stdout.split())
            supported = "USAGE:" in output and all(token in output for token in required)
            report["capabilities"][capability] = supported
            if not supported:
                return stop("capability_missing", f"Unexpected help for {capability}; interface was not accepted.")
    report["helper_ready"] = True
    return stop("helper_ready", "Reviewed helper identity and help capabilities match; runtime readiness is untested.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, help="Highball.app at a custom location")
    parser.add_argument("--helper", type=Path, help="Explicit trusted standalone highball binary")
    parser.add_argument("--manifest", type=Path, help="Trusted build provenance JSON")
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        report = {"status": "unsupported_platform", "helper_ready": False}
    else:
        report = probe([args.app] if args.app else None, args.helper, args.manifest)
    print(json.dumps(report, indent=2))
    return 0 if report["helper_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
