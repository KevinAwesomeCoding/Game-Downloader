"""Read-only Highball engine and bottle readiness checks.

Requires a helper that passed ``probe.probe``. The check runs only ``config``,
``engine list`` and ``bottle list``. ``engine list`` invokes each installed
engine's Wine version executable; it does not launch a game or change a bottle.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from probe import probe


READINESS_COMMANDS = (("config",), ("engine", "list"), ("bottle", "list"))
TIMEOUT_SECONDS = 20


class ReadinessError(RuntimeError):
    """A command failed or Highball output changed in an unrecognized way."""


def _run(helper: Path, args: tuple[str, ...], *, env: dict[str, str], cwd: str, runner) -> str:
    try:
        result = runner([str(helper), *args], cwd=cwd, env=env,
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, errors="replace",
                        timeout=TIMEOUT_SECONDS, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReadinessError(f"{' '.join(args)} failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:500]
        raise ReadinessError(f"{' '.join(args)} exited {result.returncode}: {detail}")
    return result.stdout


def _parse_home(output: str) -> tuple[str, str | None]:
    match = re.search(r"^home: (.+?)(?: \((?:default|chosen)\))?$", output, re.MULTILINE)
    if not match:
        raise ReadinessError("Unrecognized `highball config` output; home was not accepted.")
    home = str(Path(match.group(1)).expanduser().resolve())
    unavailable = re.search(r"chosen location not available right now: (.+?); running on the default meanwhile", output)
    warning = f"Configured Highball home is unavailable: {unavailable.group(1)}" if unavailable else None
    return home, warning


def _parse_engines(output: str) -> dict[str, dict]:
    engines = {}
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith("no engines installed"):
            continue
        match = re.fullmatch(r"([^\t]+)\t([^\t]+)\trenderers: ([^\t]*)(?:\tINCOMPLETE, missing: (.*))?", line)
        if not match:
            raise ReadinessError("Unrecognized `highball engine list` output; no engine status was accepted.")
        engine_id, wine_version, renderers_text, missing = match.groups()
        renderers = {}
        for item in filter(None, (part.strip() for part in renderers_text.split(","))):
            lic = "licence not accepted" in item.lower() or "license not accepted" in item.lower()
            name = re.sub(r"\s*\((?:licen[cs]e not accepted)\)", "", item, flags=re.IGNORECASE)
            renderers[name] = {"available": not lic, "license_accepted": not lic}
        engines[engine_id] = {
            "id": engine_id,
            "wine_version": wine_version,
            "complete": missing is None,
            "missing_files": [item.strip() for item in (missing or "").split(",") if item.strip()],
            "renderers": renderers,
        }
    return engines


def _parse_bottles(output: str, engines: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    bottles = []
    damaged = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line == "no bottles":
            continue
        fields = line.split("\t")
        if len(fields) >= 2 and "DAMAGED" in fields[1]:
            damaged.append({"name": fields[0], "detail": "\t".join(fields[1:])})
            continue
        if len(fields) < 4:
            raise ReadinessError("Unrecognized `highball bottle list` output; no bottle status was accepted.")
        values = {}
        for field in fields[1:]:
            if "=" not in field:
                raise ReadinessError("Unrecognized `highball bottle list` field; no bottle status was accepted.")
            key, value = field.split("=", 1)
            values[key] = value
        if not all(key in values for key in ("engine", "renderer", "pins", "recipes")):
            raise ReadinessError("Highball bottle output is missing a required field.")
        engine = engines.get(values["engine"])
        renderer = values["renderer"]
        issues = []
        if engine is None:
            issues.append("assigned engine is not installed")
        elif not engine["complete"]:
            issues.append("assigned engine is incomplete")
        elif engine["wine_version"] == "?":
            issues.append("Wine version could not be read")
        elif renderer not in engine["renderers"]:
            issues.append(f"renderer {renderer} is not available in the assigned engine")
        elif not engine["renderers"][renderer]["available"]:
            issues.append(f"renderer {renderer} needs its license accepted in Highball")
        try:
            pin_count = int(values["pins"])
        except ValueError as exc:
            raise ReadinessError("Highball reported a nonnumeric pin count.") from exc
        bottles.append({
            "name": fields[0], "engine": values["engine"], "renderer": renderer,
            "pins": pin_count, "recipes": values["recipes"],
            "ready": not issues, "issues": issues,
        })
    return bottles, damaged


def runtime_readiness(helper: Path, *, runner=None) -> dict:
    """Query the selected Highball home without installing or modifying anything."""
    run = runner or subprocess.run
    base_env = dict(os.environ)
    base_env.update(LC_ALL="C", TERM="dumb", NO_COLOR="1")
    with tempfile.TemporaryDirectory(prefix="highball-readiness-") as cwd:
        home_output = _run(helper, READINESS_COMMANDS[0], env=base_env, cwd=cwd, runner=run)
        home, warning = _parse_home(home_output)
        active_env = dict(base_env, HIGHBALL_HOME=home)
        engine_output = _run(helper, READINESS_COMMANDS[1], env=active_env, cwd=cwd, runner=run)
        bottle_output = _run(helper, READINESS_COMMANDS[2], env=active_env, cwd=cwd, runner=run)

    engines = _parse_engines(engine_output)
    bottles, damaged = _parse_bottles(bottle_output, engines)
    if not engines:
        status = "no_engines"
    elif not bottles and not damaged:
        status = "no_bottles"
    elif all(bottle["ready"] for bottle in bottles) and not damaged:
        status = "ready"
    else:
        status = "needs_attention"
    return {
        "status": status,
        "home": home,
        "warning": warning,
        "checks": {
            "config": "read active Highball home",
            "engine_list": "checks engine completeness, Wine version, and renderer availability",
            "bottle_list": "checks loadable bottles and each bottle's engine/renderer pairing",
        },
        "engine_list_runs_wine_version": True,
        "engines": list(engines.values()),
        "bottles": bottles,
        "damaged_bottles": damaged,
        "mutations_performed": False,
        "game_launched": False,
    }


def check(app: Path | None, helper: Path | None, manifest: Path | None, *, runner=None) -> dict:
    """Run the existing helper identity check, then runtime readiness if accepted."""
    run = runner or subprocess.run
    apps = [app] if app else None
    interface = probe(apps, helper, manifest, runner=run)
    if not interface["helper_ready"]:
        return {"status": "helper_not_ready", "interface": interface, "readiness": None}
    runtime = runtime_readiness(Path(helper), runner=run)
    return {"status": runtime["status"], "interface": interface, "readiness": runtime}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, help="Highball.app at a custom location")
    parser.add_argument("--helper", type=Path, help="Explicit trusted standalone highball binary")
    parser.add_argument("--manifest", type=Path, help="Trusted build provenance JSON")
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        report = {"status": "unsupported_platform", "readiness": None}
    else:
        try:
            report = check(args.app, args.helper, args.manifest)
        except ReadinessError as exc:
            report = {"status": "readiness_failed", "detail": str(exc), "readiness": None}
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
