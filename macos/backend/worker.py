"""JSON-lines worker for the local Mac preview; no actions run on import."""
import argparse
import ctypes
import fcntl
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading

from archives import content_root, extract, merge_archive_part, merge_repair, safe_path
from downloads import Downloader, Stopped, atomic_json, checkpoint, fetch_with_backup, parts, sha256
from highball import Highball
from universal_setup import apply_universal_setup, recipe_catalog, report_lines


def emit(status, message, **fields):
    print(json.dumps(dict(status=status, message=message, **fields)), flush=True)


def recipes():
    return recipe_catalog()


def highball_recipe(receipt):
    """Combine a verified universal profile with only this game's delta."""
    setup = receipt.get("universalSetup") or {}
    settings = setup.get("settings")
    catalog = recipes()
    build = catalog.get("builds", {}).get(receipt.get("gameID"))
    delta = catalog.get("deltas", {}).get(receipt.get("gameID"))
    if not setup.get("readyForHighball") or not settings or not build or not delta:
        return None
    renderer = settings["renderer"]
    override = delta.get("rendererOverride")
    # Highball's CLI has no Vulkan renderer setting; retain the delta for
    # visibility but apply the supported universal renderer.
    supported_renderers = {"wined3d", "dxmt", "d3dmetal", "dxvk"}
    if override in supported_renderers:
        renderer = override
    return {
        "executable": delta["executable"],
        "arguments": settings["arguments"],
        "renderer": renderer,
        "environment": {},
        "mainHashes": receipt["mainHashes"],
        "repairHashes": receipt["repairHashes"],
        "requiredRecipes": ["steam", "vcrun2022"],
        "engine": settings["engine"],
    }


def publish_directory(source, destination):
    # macOS's exclusive rename atomically refuses even an empty existing target.
    if sys.platform != "darwin":
        raise ValueError("Publishing this Mac installation requires macOS.")
    library = ctypes.CDLL(None, use_errno=True)
    rename = library.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(bytes(source), bytes(destination), 0x00000004):  # RENAME_EXCL, macOS stdio.h
        error = ctypes.get_errno()
        raise OSError(error, "Could not publish installation without replacing an existing path", str(destination))


def install(job, cache, stop=lambda: False, notify=emit, downloader=None):
    game = job["game"]
    if game.get("type") == "fix" or str(game.get("zipUrl", "")).startswith("steam:"):
        raise ValueError("This entry installs through Windows Steam. Use Open in Windows Steam; repair-only installs are not supported yet.")
    main, repairs = parts(game.get("zipUrl")), parts(game.get("fixZipUrl"))
    main_backups = parts(game.get("zipBackupUrl"))
    repair_backups = parts(game.get("fixBackupUrl"))
    if not main and main_backups:
        main, main_backups = main_backups, []
    if not repairs and repair_backups:
        repairs, repair_backups = repair_backups, []
    if not main:
        raise ValueError("This catalog entry has no download configured.")
    for label, sources, backups in (("game", main, main_backups), ("repair", repairs, repair_backups)):
        if backups and len(backups) != len(sources):
            raise ValueError(f"The {label} backup links must match the number of archive parts.")
    folder = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", game["name"]).strip().rstrip(".")
    if not folder or folder in {".", ".."}:
        raise ValueError("Invalid game folder name.")
    base = Path(job["installFolder"]).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    destination = base / folder
    receipt_path = Path(cache) / "receipt.json"
    if destination.exists() or destination.is_symlink():
        # Recover only a completed installation owned by this exact job.
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            marker_path = destination / ".game-installer-receipt.json"
            marker = json.loads(marker_path.read_text()) if marker_path.is_file() else {}
            if (receipt.get("jobID") == job["id"] and receipt.get("installPath") == str(destination)
                    and marker.get("jobID") == job["id"] and marker.get("mainHashes") == receipt.get("mainHashes")
                    and marker.get("repairHashes") == receipt.get("repairHashes")):
                setup = apply_universal_setup({
                    "gameID": game["id"], "installPath": destination,
                    "mainArchives": receipt.get("mainArchiveRecords", []),
                    "repairArchives": receipt.get("repairArchiveRecords", []),
                })
                matched = setup["readyForHighball"]
                receipt["universalSetup"] = setup
                receipt["recordedRecipeAvailable"] = matched
                atomic_json(receipt_path, receipt)
                atomic_json(marker_path, receipt)
                message = "Existing completed installation recovered."
                message += "\n" + "\n".join(report_lines(setup["report"]))
                notify("installed", message, installPath=str(destination), recipeAvailable=matched)
                return receipt
        raise ValueError("Destination already exists. Choose another install folder; no existing files were overwritten.")
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    all_hashes = []
    all_records = []
    archives = []
    for label, sources, backups in [("main", main, main_backups), ("repair", repairs, repair_backups)]:
        hashes, files = [], []
        for index, source in enumerate(sources):
            checkpoint(stop)
            suffix = f".part{index + 1}.rar" if len(sources) > 1 else ".archive"
            filename = cache / (label + suffix)
            notify("downloading", f"Downloading {label} archive {index + 1} of {len(sources)}…", done=0, total=0)
            transfer = downloader or Downloader(stop=stop, progress=lambda done, total: notify(
                "downloading", f"Downloading {label} {index + 1}/{len(sources)}", done=done, total=total))
            backup_source = backups[index]["url"] if backups else ""
            hashes.append(fetch_with_backup(
                transfer, source["url"], backup_source, filename,
                on_fallback=lambda _error, label=label, index=index: notify(
                    "downloading",
                    f"Primary {label} link failed; trying backup {index + 1}…",
                    done=0, total=0,
                ),
            ))
            files.append(filename)
        all_hashes.append(hashes)
        all_records.append([{"bytes": archive.stat().st_size, "sha256": digest}
                            for archive, digest in zip(files, hashes)])
        archives.append(files)
    checkpoint(stop)
    # All install/repair changes happen in a private sibling directory; publish only on success.
    with tempfile.TemporaryDirectory(prefix=".game-installer-", dir=base) as temporary:
        staging = Path(temporary)
        main_parts = archives[0]
        root = None
        for index, archive in enumerate(main_parts):
            notify("extracting", f"Extracting game archive {index + 1} of {len(main_parts)}…", done=0, total=0)
            part_directory = staging / f"main-part-{index + 1}"
            extract(archive, part_directory, stop,
                    lambda done, total, i=index: notify(
                        "extracting", f"Extracting game archive {i + 1} of {len(main_parts)}…",
                        done=done, total=total))
            part_root = content_root(part_directory)
            if root is None:
                root = part_root
            else:
                merge_archive_part(part_root, root)
        if repairs:
            notify("repairing", "Extracting and applying the catalog repair…", done=0, total=0)
            extract(archives[1][0], staging / "repair", stop)
            merge_repair(staging / "repair", root, stop)
        checkpoint(stop)
        setup = apply_universal_setup({
            "gameID": game["id"], "installPath": root,
            "mainArchives": all_records[0], "repairArchives": all_records[1],
        })
        matched = setup["readyForHighball"]
        receipt = {"jobID": job["id"], "gameID": game["id"], "gameName": game["name"],
                   "thumbnail": game.get("thumbnail"),
                   "installPath": str(destination), "mainHashes": all_hashes[0], "repairHashes": all_hashes[1],
                   "mainArchiveRecords": all_records[0], "repairArchiveRecords": all_records[1],
                   "recordedRecipeAvailable": matched, "universalSetup": setup}
        atomic_json(root / ".game-installer-receipt.json", receipt)
        atomic_json(receipt_path, receipt)  # Allows recovery if app exits just after the directory rename.
        if destination.exists() or destination.is_symlink():
            raise ValueError("Destination appeared during installation; existing files were not overwritten.")
        publish_directory(root, destination)
    message = "Installed. Add to Highball when ready; multiplayer compatibility is not established by installation."
    message += "\n" + "\n".join(report_lines(setup["report"]))
    notify("installed", message,
           installPath=str(destination), recipeAvailable=matched)
    return receipt


def cleanup_downloads(job, cache, notify=emit):
    """Remove only source archives verified against this job's completed receipt."""
    cache = Path(cache).resolve()
    receipt = json.loads((cache / "receipt.json").read_text())
    if receipt.get("jobID") != job["id"] or receipt.get("gameID") != job["game"]["id"]:
        raise ValueError("The saved installation receipt does not match this job; download files were kept.")
    install_path = Path(receipt["installPath"]).expanduser().resolve()
    marker = json.loads((install_path / ".game-installer-receipt.json").read_text())
    if (marker.get("jobID") != job["id"] or marker.get("mainHashes") != receipt.get("mainHashes")
            or marker.get("repairHashes") != receipt.get("repairHashes")):
        raise ValueError("The installed game receipt could not be verified; download files were kept.")

    main = parts(job["game"].get("zipUrl")) or parts(job["game"].get("zipBackupUrl"))
    repairs = parts(job["game"].get("fixZipUrl")) or parts(job["game"].get("fixBackupUrl"))
    expected = []
    for label, sources, expected_hashes in (
            ("main", main, receipt.get("mainHashes", [])),
            ("repair", repairs, receipt.get("repairHashes", []))):
        if len(sources) != len(expected_hashes):
            raise ValueError("Archive receipt is incomplete; download files were kept.")
        for index, expected_hash in enumerate(expected_hashes):
            suffix = f".part{index + 1}.rar" if len(sources) > 1 else ".archive"
            archive = cache / (label + suffix)
            if archive.is_symlink() or not archive.is_file():
                raise ValueError("A completed archive is missing or unsafe; download files were kept.")
            if sha256(archive) != expected_hash:
                raise ValueError("A cached archive no longer matches its receipt; download files were kept.")
            expected.append(archive)

    removed_bytes = 0
    removed_files = 0
    for archive in expected:
        removed_bytes += archive.stat().st_size
        archive.unlink()
        removed_files += 1
        for suffix in (".json", ".part", ".part.json"):
            sidecar = archive.with_name(archive.name + suffix)
            if sidecar.is_file() and not sidecar.is_symlink():
                sidecar.unlink()
                removed_files += 1
    notify("cleaned", f"Removed {removed_files} verified download files ({removed_bytes} bytes). The installed game and receipt were kept.",
           done=removed_bytes, total=removed_files)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "register", "launch", "steam", "tools", "cleanup"])
    parser.add_argument("--job", type=Path)
    parser.add_argument("--bottle")
    parser.add_argument("--uri")
    parser.add_argument("--executable")
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--registered", action="store_true")
    args = parser.parse_args()
    stopped = threading.Event()

    def control():
        # EOF means the parent app exited. Downloads stop without discarding partials.
        sys.stdin.readline()
        stopped.set()

    threading.Thread(target=control, daemon=True).start()
    try:
        if args.action == "tools":
            from archives import find_tool
            emit("tools", "ZIP is built in. RAR/7z uses local unar/lsar.", rarAvailable=bool(find_tool("unar") and find_tool("lsar")))
            return 0
        if args.action == "steam":
            result = Highball().steam_install(args.bottle, args.uri, lambda message: emit("running", message))
            emit("handedOff", "Request sent to Windows Steam. Check its window for download progress. Launch log: " + result["logPath"], **result)
            return 0
        job = json.loads(args.job.read_text())
        if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", job["id"]):
            raise ValueError("Invalid install job identifier.")
        cache = args.job.parent / job["id"]
        cache.mkdir(exist_ok=True)
        lock = (cache / ".worker.lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("This install job is already running in another process.") from exc
        if args.action == "install":
            install(job, cache, stopped.is_set)
            return 0
        path = cache / "receipt.json"
        if args.action == "cleanup":
            if not args.registered:
                raise ValueError("Add the installed game to Highball before cleaning its download files.")
            cleanup_downloads(job, cache)
            return 0
        receipt = json.loads(path.read_text())
        highball = Highball()
        if args.action == "register":
            selected_recipe = None if args.manual else highball_recipe(receipt)
            try:
                highball.register(receipt, args.bottle, selected_recipe, args.executable)
            finally:
                atomic_json(path, receipt)  # Partial pin creation can be retried without touching other pins.
            registration = receipt.get("registration", {})
            message = "Added to Highball. Play sends the launch request through its checked helper."
            if selected_recipe and receipt.get("universalSetup", {}).get("settings", {}).get("requiresSteamFirst"):
                message += " Start and sign in to Steam in this bottle before launching."
            delta = recipes().get("deltas", {}).get(receipt.get("gameID"), {})
            if delta.get("rendererOverride") == "vulkan":
                message += " The game delta requests Vulkan, which this Highball CLI cannot set; the pin uses D3DMetal."
            if registration.get("coverStatus") == "set":
                message = "Added to Highball with the game name and catalog cover."
            elif registration.get("coverStatus") in {"failed", "unavailable"}:
                message = "Added to Highball with the game name; its cover could not be set automatically."
                detail = registration.get("coverMessage")
                if detail:
                    message += " " + detail
            emit("registered", message)
        else:
            result = highball.launch(receipt, lambda message: emit("running", message))
            atomic_json(path, receipt)
            if result.get("steamStarted"):
                emit("steamStarted", "Windows Steam opened in this bottle. Sign in, then click Play in the installer again.", **result)
                return 0
            emit("launchRequested", "Launch requested through Highball; game startup is not yet verified. Log: " + result["logPath"], **result)
        return 0
    except Stopped:
        emit("stopped", "Stopped. Partial downloads and completed archives are kept for resume.")
        return 3
    except Exception as exc:
        emit("failed", str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
