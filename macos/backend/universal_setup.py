"""Build-gated Highball setup and exact-build game patches.

This module prepares renderer, argument, and detected Wine-name DLL override
settings, applies hash-guarded game patches, and never fabricates Steam manifests.
"""
from pathlib import Path
import hashlib
import os
import stat
import tempfile

from archives import safe_path


RECIPE_FILE = Path(__file__).with_name("recipes.json")
UNIVERSAL_RENDERER = "d3dmetal"
UNIVERSAL_ARGUMENTS = ["-windowed", "-ResX=1280", "-ResY=720"]
WINE_COLLISION_DLLS = {
    "winmm", "dinput8", "dxgi", "version", "winhttp", "dnet", "steam_api64",
    "steam_api", "onlinefix64", "steamoverlay64", "custom", "stubdrm64",
    "eossdk-win64-shipping", "eosauthhooker64",
}


def recipe_catalog():
    import json
    return json.loads(RECIPE_FILE.read_text(encoding="utf-8"))


def _record_matches(expected, actual):
    return (isinstance(expected, list) and isinstance(actual, list) and len(expected) == len(actual)
            and all(isinstance(e, dict) and isinstance(a, dict)
                    and e.get("bytes") == a.get("bytes")
                    and e.get("sha256", "").lower() == a.get("sha256", "").lower()
                    for e, a in zip(expected, actual)))


def _steamapps_path():
    override = os.environ.get("HIGHBALL_STEAMAPPS")
    if override:
        path = Path(override).expanduser()
        if path.is_dir() and path.name.casefold() == "steamapps":
            return path
        return None

    highball_root = Path.home() / "Library/Application Support/Highball"
    if not highball_root.is_dir():
        return None
    for current, directories, _ in os.walk(highball_root, followlinks=False):
        directories.sort()
        if Path(current).name.casefold() == "steamapps":
            return Path(current)
    return None


def _collisions(root):
    found = []
    root = Path(root)
    if not root.is_dir():
        return found
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if not (Path(current) / d).is_symlink())
        for filename in files:
            if not filename.casefold().endswith(".dll"):
                continue
            stem = Path(filename).stem.casefold()
            if stem in WINE_COLLISION_DLLS:
                found.append(str((Path(current) / filename).relative_to(root)))
    return sorted(found, key=str.casefold)


def _report(step, status, detail):
    return {"step": step, "status": status, "detail": detail}


def _apply_managed_patch(root, patch):
    """Apply an exact-build IL patch using hash and byte guards, idempotently."""
    root = Path(root).resolve()
    target_relative = safe_path(patch["path"])
    candidate = root / target_relative
    if any((root / Path(*target_relative.parts[:index])).is_symlink()
           for index in range(1, len(target_relative.parts) + 1)):
        raise ValueError(f"Patch target contains a symbolic link: {patch['path']}")
    target = candidate.resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError(f"Patch target is missing or unsafe: {patch['path']}")
    current = target.read_bytes()
    current_hash = hashlib.sha256(current).hexdigest()
    expected = patch["sourceSha256"].lower()
    patched_hash = patch["patchedSha256"].lower()
    if current_hash == patched_hash:
        return "Patch is already applied; no changes made."
    if current_hash != expected:
        raise ValueError("Patch source DLL hash does not match the recorded build.")

    offset = patch["offset"]
    original = bytes.fromhex(patch["originalHex"])
    replacement = bytes.fromhex(patch["replacementHex"])
    if len(original) != len(replacement) or current[offset:offset + len(original)] != original:
        raise ValueError("Patch IL bytes do not match the recorded method body.")
    changed = bytearray(current)
    changed[offset:offset + len(replacement)] = replacement
    output = bytes(changed)
    if hashlib.sha256(output).hexdigest() != patched_hash:
        raise ValueError("Patched DLL hash differs from the expected output.")

    backup_relative = safe_path(patch["backupPath"])
    backup_candidate = root / backup_relative
    if any((root / Path(*backup_relative.parts[:index])).is_symlink()
           for index in range(1, len(backup_relative.parts) + 1)):
        raise ValueError("Patch backup path contains a symbolic link.")
    backup = backup_candidate.resolve()
    if not backup.is_relative_to(root) or backup == target:
        raise ValueError("Patch backup path is unsafe.")
    if backup.exists():
        if not backup.is_file() or hashlib.sha256(backup.read_bytes()).hexdigest() != expected:
            raise ValueError("An unrelated file already occupies the original-DLL backup path.")
    else:
        with backup.open("xb") as stream:
            stream.write(current)
            stream.flush()
            os.fsync(stream.fileno())

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix=target.name + ".", dir=target.parent,
                                         delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(output)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, stat.S_IMODE(target.stat().st_mode))
        os.replace(temp_path, target)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    if hashlib.sha256(target.read_bytes()).hexdigest() != patched_hash:
        raise ValueError("Patched DLL failed its post-write hash check.")
    return "Removed the known null-localization message calls before the Steam invite overlay; original DLL backed up."


def apply_universal_setup(game):
    """Prepare setup and apply only exact-build patches for an extracted build.

    `game` contains `gameID`, `installPath`, and archive records in `mainArchives`
    and `repairArchives`; each record has byte size and SHA-256. Settings are
    eligible only when both source archives exactly match a recorded build.
    """
    catalog = recipe_catalog()
    game_id = game.get("gameID") or game.get("id")
    build = catalog.get("builds", {}).get(game_id)
    delta = catalog.get("deltas", {}).get(game_id)
    report = []

    build_matches = bool(build and
                         _record_matches(build.get("main"), game.get("mainArchives")) and
                         _record_matches(build.get("repair"), game.get("repairArchives")))
    if build_matches:
        report.append(_report("Build check", "applied", "Archive sizes and SHA-256 match the recorded build."))
    elif not build:
        report.append(_report("Build check", "skipped", "No recorded build; automatic setup is disabled."))
    else:
        report.append(_report("Build check", "skipped", "Archive size or SHA-256 differs; no setup settings were applied."))

    install_root = Path(game.get("installPath", "."))
    missing = []
    if build_matches and delta:
        executable = delta.get("executable")
        if executable:
            path = (install_root / safe_path(executable)).resolve()
            if not path.is_relative_to(install_root.resolve()) or not path.is_file():
                missing.append(executable)
        for item in delta.get("extraFilesToVerify", []):
            path = (install_root / safe_path(item)).resolve()
            if not path.is_relative_to(install_root.resolve()) or not path.is_file():
                missing.append(item)
        for item in delta.get("extraDirectoriesToVerify", []):
            path = (install_root / safe_path(item)).resolve()
            if not path.is_relative_to(install_root.resolve()) or not path.is_dir():
                missing.append(item + "/")
    files_match = build_matches and not missing
    if build_matches:
        report.append(_report("Game files", "applied" if not missing else "skipped",
                              "Required game files are present." if not missing else
                              "Required game files are missing: " + ", ".join(missing)))

    if files_match:
        report.append(_report("Renderer", "applied", "D3DMetal prepared for the Highball pin."))
        report.append(_report("Launch arguments", "applied", "Windowed 1280×720 prepared for the Highball pin."))
        if (delta or {}).get("rendererOverride") == "vulkan":
            report.append(_report("Delta renderer", "skipped",
                                  "Vulkan is recorded for this game, but Highball's CLI has no Vulkan pin setting; D3DMetal remains selected."))
    else:
        report.append(_report("Renderer", "skipped", "Build gate did not pass."))
        report.append(_report("Launch arguments", "skipped", "Build gate did not pass."))

    steam_first = True
    report.append(_report("Steam first", "applied" if build_matches else "skipped",
                          "Launch prerequisite recorded: open and sign in to Steam in this bottle first."
                          if build_matches else "Build gate did not pass; no setup requirements were applied."))

    collisions = _collisions(install_root) if build_matches else []
    if collisions:
        names = sorted({Path(item).stem.casefold() for item in collisions})
        detail = f"Found {len(names)} Wine-name DLLs; native-first overrides prepared ({', '.join(names)})."
        status = "applied"
    elif build_matches:
        detail, status = "No Wine-name DLL collisions found; no overrides needed.", "skipped"
    else:
        detail, status = "Build gate did not pass; folder scan was skipped.", "skipped"
    report.append(_report("DLL overrides", status, detail))

    managed_patches = (delta or {}).get("managedPatches", [])
    patch_ok = True
    if managed_patches:
        if not files_match:
            patch_ok = False
            report.append(_report("Game patch", "skipped", "Build or required-file gate did not pass."))
        else:
            details = []
            for patch in managed_patches:
                try:
                    details.append(_apply_managed_patch(install_root, patch))
                except (OSError, ValueError, KeyError) as error:
                    patch_ok = False
                    details.append(str(error))
            report.append(_report("Game patch", "applied" if patch_ok else "failed", "; ".join(details)))

    steamapps = _steamapps_path()
    report.append(_report("Steam manifest", "skipped",
                          "The installer does not create or modify Steam install records."))
    if steamapps is None:
        report.append(_report("Steam bottle", "skipped",
                              "Could not find a bottle steamapps folder; HIGHBALL_STEAMAPPS may point to one."))
    else:
        report.append(_report("Steam bottle", "applied", "Located the selected Steam bottle directory."))

    settings = None
    ready_for_highball = files_match and patch_ok
    if ready_for_highball:
        settings = {
            "renderer": UNIVERSAL_RENDERER,
            "arguments": UNIVERSAL_ARGUMENTS + (delta or {}).get("extraArgs", []),
            "requiresSteamFirst": steam_first,
            "engine": build["engine"],
        }
    return {
        "gameID": game_id,
        "buildMatched": bool(build_matches),
        "readyForHighball": bool(ready_for_highball),
        "settings": settings,
        "report": report,
        "collidingDlls": collisions,
        "steamappsPath": str(steamapps) if steamapps else None,
    }


def report_lines(report):
    """Compact status lines suitable for the Downloads card."""
    labels = {
        "Build check": "Build check",
        "Game files": "Game files",
        "Renderer": "Renderer",
        "Launch arguments": "Launch args",
        "Steam first": "Steam first",
        "DLL overrides": "DLL overrides",
        "Steam manifest": "Steam manifest",
        "Steam bottle": "Steam bottle",
    }
    return [f"{labels.get(item['step'], item['step'])}: {item['detail']}" for item in report]
