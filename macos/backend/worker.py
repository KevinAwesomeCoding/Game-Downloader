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
import time
import urllib.parse
import urllib.request

from archives import (content_root, extract, extract_rar_volume_set, find_tool,
                      is_rar_volume_set, merge_archive_part, merge_repair, safe_path)
from downloads import (Downloader, Stopped, atomic_json, checkpoint, download_host_name,
                       fetch_with_backup, fetch_with_retry, parts, sha256)
from host_resolver_client import provider_for, resolve_source
from highball import Highball
from itch_login import sign_in, token_destination
from storage_workspace import (MIN_INSTALL_FREE_BYTES, WorkspaceError,
                               check_workspace, expected_workspace,
                               migrate_legacy_workspace)
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
    collision_names = sorted({Path(item).stem.casefold() for item in setup.get("collidingDlls", [])})
    environment = {}
    if collision_names:
        environment["WINEDLLOVERRIDES"] = ";".join(
            f"{name}=native,builtin" for name in collision_names
        )
    return {
        "executable": delta["executable"],
        "arguments": settings["arguments"],
        "renderer": renderer,
        "environment": environment,
        "mainHashes": receipt["mainHashes"],
        "repairHashes": receipt["repairHashes"],
        "requiredRecipes": delta.get("requiredRecipes", []),
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


def validate_workspace_volume(job, workspace, minimum_free=MIN_INSTALL_FREE_BYTES):
    return check_workspace(dict(job, workspacePath=str(workspace)), create=True,
                           required_free=minimum_free)


class JobStaging:
    """Keep extraction staging after failures; remove the attempt only after success."""
    def __init__(self, parent):
        self.parent = Path(parent)
        self.path = None

    def __enter__(self):
        if self.parent.is_symlink():
            raise WorkspaceError("The job staging folder is a symbolic link; no files were written.")
        self.parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(tempfile.mkdtemp(prefix="attempt-", dir=self.parent))
        return str(self.path)

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is None and self.path is not None:
            shutil.rmtree(self.path, ignore_errors=True)
        return False


def cleanup_staging_after_success(parent):
    shutil.rmtree(parent, ignore_errors=True)


class ArchiveSetProgress:
    """Track verified sizes across one archive set without inventing unknown sizes."""

    def __init__(self, label, sources, backups, cache, stop):
        self.label, self.sources, self.backups = label, sources, backups
        self.sizes = [None] * len(sources)
        self.cached = [0] * len(sources)
        self.stop = stop
        self.lock = threading.Lock()
        for index, source in enumerate(sources):
            suffix = f".part{index + 1}.rar" if len(sources) > 1 else ".archive"
            filename = cache / (label + suffix)
            try:
                receipt = json.loads(Path(str(filename) + ".json").read_text())
                choices = {source["url"]}
                if backups:
                    choices.add(backups[index]["url"])
                size = receipt.get("expected_size") or receipt.get("total")
                if receipt.get("source") not in choices or type(size) is not int or size <= 0:
                    continue
                self.sizes[index] = size
                if receipt.get("complete") and filename.is_file() and filename.stat().st_size == size:
                    self.cached[index] = size
                elif not backups and (receipt.get("etag") or receipt.get("modified")):
                    partial = Path(str(filename) + ".part")
                    if partial.is_file():
                        self.cached[index] = min(size, partial.stat().st_size)
            except (OSError, ValueError):
                pass
        if len(sources) > 1:
            threading.Thread(target=self._probe, daemon=True, name="mac-archive-size-probe").start()

    def _probe(self):
        for index, source in enumerate(self.sources):
            if self.stop():
                return
            with self.lock:
                if self.sizes[index] is not None:
                    continue
            url = source["url"]
            parsed = urllib.parse.urlsplit(url)
            size = None
            try:
                provider = provider_for(url)
                if provider in ("gofile", "filekeeper"):
                    deadline = time.monotonic() + 15
                    metadata = resolve_source(url, stop=lambda: self.stop() or time.monotonic() >= deadline)
                    size = metadata.get("size")
                elif (parsed.scheme == "https" and not parsed.username and not parsed.password
                      and parsed.port in (None, 443) and (
                          (parsed.hostname in ("archive.org", "www.archive.org") and parsed.path.startswith("/download/") and not parsed.query)
                          or (parsed.hostname == "drive.usercontent.google.com" and parsed.path == "/download")
                          or (parsed.hostname == "pixeldrain.com" and parsed.path.startswith("/api/file/"))
                          or (parsed.hostname == "files.catbox.moe" and parsed.path.count("/") == 1))):
                    request = urllib.request.Request(url, method="HEAD", headers={"Accept-Encoding": "identity"})
                    with urllib.request.urlopen(request, timeout=6) as response:
                        kind = response.headers.get("Content-Type", "").lower()
                        if response.status == 200 and not any(x in kind for x in ("text/html", "application/json", "text/plain")):
                            size = int(response.headers.get("Content-Length", "0"))
            except Exception:
                pass  # Size metadata is optional; the download must continue.
            if type(size) is int and size > 0 and not self.stop():
                with self.lock:
                    if self.sizes[index] is None:
                        self.sizes[index] = size

    def fields(self, index, done, total):
        with self.lock:
            if total > 0:
                self.sizes[index] = total
            sizes = list(self.sizes)
            cached = list(self.cached)
        known = all(size is not None for size in sizes)
        archive_done = (sum(size or 0 for size in sizes[:index]) + max(done, 0)
                        + sum(cached[index + 1:]))
        archive_total = sum(sizes) if known else 0
        remaining = (max((sizes[index] or 0) - done, 0)
                     + sum(max(size - cached[i], 0) for i, size in enumerate(sizes) if i > index)) if known else 0
        return {"archiveDone": archive_done, "archiveTotal": archive_total,
                "archiveRemaining": remaining, "partIndex": index}


def install(job, cache, stop=lambda: False, notify=emit, downloader=None, state=None):
    game = job["game"]
    if game.get("type") == "fix" or str(game.get("zipUrl", "")).startswith("steam:"):
        raise ValueError("This entry installs through Windows Steam. Use Open in Windows Steam; repair-only installs are not supported yet.")
    main, repairs = parts(game.get("zipUrl")), parts(game.get("fixZipUrl"))
    updates = parts(game.get("updateZipUrl"))
    main_backups = parts(game.get("zipBackupUrl"))
    update_backups = parts(game.get("updateZipBackupUrl"))
    repair_backups = parts(game.get("fixBackupUrl"))
    if not main and main_backups:
        main, main_backups = main_backups, []
    if not repairs and repair_backups:
        repairs, repair_backups = repair_backups, []
    if not main:
        raise ValueError("This catalog entry has no download configured.")
    for label, sources, backups in (("game", main, main_backups), ("update", updates, update_backups), ("repair", repairs, repair_backups)):
        if backups and len(backups) != len(sources):
            raise ValueError(f"The {label} backup links must match the number of archive parts.")
    folder = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", game["name"]).strip().rstrip(".")
    if not folder or folder in {".", ".."}:
        raise ValueError("Invalid game folder name.")
    workspace = Path(job.get("workspacePath") or expected_workspace(job)).expanduser()
    workspace_job = dict(job, workspacePath=str(workspace))
    cache = check_workspace(workspace_job, create=True)
    base = Path(job["installFolder"]).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    destination = base / folder
    receipt_path = Path(state or cache) / "receipt.json"
    if destination.exists() or destination.is_symlink():
        # Recover only a completed installation owned by this exact job.
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            marker_path = destination / ".game-installer-receipt.json"
            marker = json.loads(marker_path.read_text()) if marker_path.is_file() else {}
            if (receipt.get("jobID") == job["id"] and receipt.get("installPath") == str(destination)
                    and marker.get("jobID") == job["id"] and marker.get("mainHashes") == receipt.get("mainHashes")
                    and marker.get("repairHashes") == receipt.get("repairHashes")
                    and marker.get("updateHashes", []) == receipt.get("updateHashes", [])):
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
                cleanup_staging_after_success(Path(cache) / ".staging")
                return receipt
        raise ValueError("Destination already exists. Choose another install folder; no existing files were overwritten.")
    all_hashes = []
    all_records = []
    archives = []
    for label, sources, backups in [("main", main, main_backups), ("update", updates, update_backups), ("repair", repairs, repair_backups)]:
        if not sources:
            all_hashes.append([])
            all_records.append([])
            archives.append([])
            continue
        progress = ArchiveSetProgress(label, sources, backups, cache, stop)
        hashes, files = [], []
        for index, source in enumerate(sources):
            checkpoint(stop)
            suffix = f".part{index + 1}.rar" if len(sources) > 1 else ".archive"
            filename = cache / (label + suffix)
            active_host = {"name": download_host_name(source["url"])}
            backup_source = backups[index]["url"] if backups else ""

            def fallback(_error):
                active_host["name"] = download_host_name(backup_source)
                notify("downloading",
                       f"Primary {label} link failed; trying backup {index + 1} via {active_host['name']}…",
                       done=0, total=0)

            def report_progress(done, total, part=index):
                check_workspace(workspace_job, required_free=1, probe_writable=False)
                notify("downloading", f"Downloading {label} {part + 1}/{len(sources)} via {active_host['name']}",
                       done=done, total=total, **progress.fields(part, done, total))

            notify("downloading", f"Downloading {label} archive {index + 1} of {len(sources)} via {active_host['name']}…",
                   done=0, total=0, **progress.fields(index, 0, 0))
            transfer = downloader or Downloader(stop=stop, progress=report_progress,
                status=lambda message: notify("downloading", message, done=0, total=0),
                storage_check=lambda required_bytes=0: check_workspace(
                    workspace_job, required_free=required_bytes, probe_writable=False))
            fetch_once = lambda: fetch_with_backup(
                transfer, source["url"], backup_source, filename,
                on_fallback=fallback,
            )
            if job.get("autoRetryDownloads", True):
                digest = fetch_with_retry(fetch_once, stop=stop,
                                          status=lambda message: notify("downloading", message, done=0, total=0))
            else:
                digest = fetch_once()
            hashes.append(digest)
            files.append(filename)
        all_hashes.append(hashes)
        all_records.append([{"bytes": archive.stat().st_size, "sha256": digest}
                            for archive, digest in zip(files, hashes)])
        archives.append(files)
    checkpoint(stop)
    check_workspace(workspace_job, required_free=1)
    # All install/repair changes happen in a private sibling directory; publish only on success.
    staging_parent = cache / ".staging"
    with JobStaging(staging_parent) as temporary:
        staging = Path(temporary)
        check_workspace(workspace_job, required_free=1)
        main_parts = archives[0]
        root = None
        if is_rar_volume_set(main_parts, stop):
            notify("extracting", f"Extracting one RAR archive across {len(main_parts)} volumes…", done=0, total=0)
            root = staging / "main-volume-set"
            extract_rar_volume_set(main_parts, root, stop,
                                  lambda done, total: notify(
                                      "extracting", "Extracting the complete RAR volume set…",
                                      done=done, total=total))
            root = content_root(root)
        else:
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
        if updates:
            if len(updates) != 1:
                raise ValueError("Multipart update archives are not supported yet.")
            notify("repairing", "Extracting and applying the game update…", done=0, total=0)
            extract(archives[1][0], staging / "update", stop)
            merge_repair(staging / "update", root, stop)
        if repairs:
            notify("repairing", "Extracting and applying the catalog repair…", done=0, total=0)
            extract(archives[2][0], staging / "repair", stop)
            merge_repair(staging / "repair", root, stop)
        checkpoint(stop)
        check_workspace(workspace_job, required_free=1)
        setup = apply_universal_setup({
            "gameID": game["id"], "installPath": root,
            "mainArchives": all_records[0], "repairArchives": all_records[2],
        })
        matched = setup["readyForHighball"]
        receipt = {"jobID": job["id"], "gameID": game["id"], "gameName": game["name"],
                   "thumbnail": game.get("thumbnail"),
                   "installPath": str(destination), "mainHashes": all_hashes[0], "updateHashes": all_hashes[1], "repairHashes": all_hashes[2],
                   "mainArchiveRecords": all_records[0], "updateArchiveRecords": all_records[1], "repairArchiveRecords": all_records[2],
                   "recordedRecipeAvailable": matched, "universalSetup": setup}
        atomic_json(root / ".game-installer-receipt.json", receipt)
        atomic_json(receipt_path, receipt)  # Allows recovery if app exits just after the directory rename.
        check_workspace(workspace_job, required_free=1)
        if destination.exists() or destination.is_symlink():
            raise ValueError("Destination appeared during installation; existing files were not overwritten.")
        publish_directory(root, destination)
    cleanup_staging_after_success(staging_parent)
    message = "Installed. Add to Highball when ready; multiplayer compatibility is not established by installation."
    message += "\n" + "\n".join(report_lines(setup["report"]))
    notify("installed", message,
           installPath=str(destination), recipeAvailable=matched)
    return receipt


def prepare_highball(bottle, recipe_ids, stop=lambda: False, notify=emit):
    highball = Highball()
    return highball.apply_prerequisites(
        bottle, recipe_ids,
        lambda message: notify("preparing", message),
        stop=stop,
    )


def cleanup_downloads(job, cache, notify=emit, state=None):
    """Remove only source archives verified against this job's completed receipt."""
    if job.get("workspacePath"):
        check_workspace(job, required_free=0)
    cache = Path(cache).resolve()
    receipt = json.loads(((Path(state) if state else cache) / "receipt.json").read_text())
    if receipt.get("jobID") != job["id"] or receipt.get("gameID") != job["game"]["id"]:
        raise ValueError("The saved installation receipt does not match this job; download files were kept.")
    install_path = Path(receipt["installPath"]).expanduser().resolve()
    marker = json.loads((install_path / ".game-installer-receipt.json").read_text())
    if (marker.get("jobID") != job["id"] or marker.get("mainHashes") != receipt.get("mainHashes")
            or marker.get("repairHashes") != receipt.get("repairHashes")
            or marker.get("updateHashes", []) != receipt.get("updateHashes", [])):
        raise ValueError("The installed game receipt could not be verified; download files were kept.")

    main = parts(job["game"].get("zipUrl")) or parts(job["game"].get("zipBackupUrl"))
    updates = parts(job["game"].get("updateZipUrl"))
    repairs = parts(job["game"].get("fixZipUrl")) or parts(job["game"].get("fixBackupUrl"))
    expected = []
    for label, sources, expected_hashes in (
            ("main", main, receipt.get("mainHashes", [])),
            ("update", updates, receipt.get("updateHashes", [])),
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
    parser.add_argument("action", choices=["install", "prepare", "register", "launch", "steam", "tools", "cleanup", "migrate", "bettercrewlink", "itch-login"])
    parser.add_argument("--job", type=Path)
    parser.add_argument("--bottle")
    parser.add_argument("--uri")
    parser.add_argument("--recipe", action="append", default=[])
    parser.add_argument("--executable")
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--registered", action="store_true")
    parser.add_argument("--itch-client-id")
    args = parser.parse_args()
    stopped = threading.Event()

    def control():
        # EOF means the parent app exited. Downloads stop without discarding partials.
        sys.stdin.readline()
        stopped.set()

    threading.Thread(target=control, daemon=True).start()
    lock = None
    try:
        if args.action == "tools":
            rar_available = bool(find_tool("7zz") and find_tool("unrar"))
            emit("tools", "ZIP, RAR, and 7z extractors are bundled with Game Installer; no Homebrew tools are required.",
                 rarAvailable=rar_available)
            return 0
        if args.action == "steam":
            result = Highball().steam_install(args.bottle, args.uri, lambda message: emit("running", message))
            message = (
                "Windows Steam opened in this bottle. Launch log: "
                if result.get("steamOpened") else
                "Request sent to Windows Steam. Check its window for download progress. Launch log: "
            )
            emit("handedOff", message + result["logPath"] + " Steam executable: " + result["steamExecutable"], **result)
            return 0
        if args.action == "prepare":
            if not args.recipe:
                raise ValueError("No Highball prerequisites were requested.")
            prepared = prepare_highball(args.bottle, args.recipe, stopped.is_set)
            emit("prepared", "Highball prerequisites are ready in the selected bottle.", recipes=prepared)
            return 0
        job = json.loads(args.job.read_text())
        if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", job["id"]):
            raise ValueError("Invalid install job identifier.")
        state = args.job.parent / job["id"]
        legacy_cache_has_files = state.exists() and any(
            item.name not in {".worker.lock", "receipt.json"} for item in state.iterdir()
        )
        state.mkdir(parents=True, exist_ok=True)
        lock = (args.job.parent / f".{job['id']}.worker.lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("This install job is already running in another process.") from exc
        workspace = None
        if args.action == "install":
            if not job.get("workspacePath") and legacy_cache_has_files:
                raise WorkspaceError("This saved job still has files in the old internal cache. Use ‘Copy files to selected volume’ before resuming.")
            workspace = Path(job.get("workspacePath") or expected_workspace(job)).expanduser()
            job["workspacePath"] = str(workspace)
            check_workspace(job, create=True)
            atomic_json(args.job, job)
            install(job, workspace, stopped.is_set, state=state)
            return 0
        if args.action == "migrate":
            migrate_legacy_workspace(job, args.job, state, emit)
            return 0
        path = state / "receipt.json"
        if args.action == "cleanup":
            if not args.registered:
                raise ValueError("Add the installed game to Highball before cleaning its download files.")
            if job.get("workspacePath"):
                workspace = Path(job["workspacePath"]).expanduser()
                check_workspace(job, required_free=0)
            else:
                # Legacy installed jobs can still use the explicit, receipt-verified cleanup action.
                workspace = state
            cleanup_downloads(job, workspace, state=state)
            return 0
        receipt = json.loads(path.read_text())
        highball = Highball()
        if args.action == "itch-login":
            sign_in(args.itch_client_id, token_destination(highball, receipt), lambda message: emit("running", message))
            emit("itchLoginSaved", "Login saved. Launch Among Us to check sign-in; compatibility with this OAuth application is not yet verified.")
            return 0
        if args.action == "bettercrewlink":
            executable = highball.install_bettercrewlink(receipt, lambda message: emit("running", message))
            emit("crewlinkInstalled", f"BetterCrewLink was installed and its Highball entry was verified in {receipt['registration']['bottle']}: {executable}. Launch the game and BetterCrewLink to check detection and voice.")
            return 0
        if args.action == "launch":
            result = highball.launch_steam_game(
                receipt, job["game"].get("steamAppId"),
                (receipt.get("registration") or {}).get("bottle") or args.bottle,
                lambda message: emit("running", message))
            if result:
                emit("launchRequested", "Request sent through Windows Steam; Steam checks ownership and sign-in. Steam executable: " + result["steamExecutable"] + ". Log: " + result["logPath"], **result)
                return 0
            if not (receipt.get("registration") or {}).get("complete"):
                args.action = "register"  # Same first-click registration for ordinary local games.
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
            fallback = registration.get("rendererFallback")
            if fallback:
                message += f" D3DMetal is unavailable in this Highball engine, so this pin uses the bottle's available {fallback['used']} renderer instead."
            if registration.get("executableSource") == "automatic detection":
                message += f" Automatically detected game executable: {registration['executable']}. Use Choose .exe & add if you want to change it."
            elif registration.get("executableSource") == "manual":
                message += f" Game executable selected manually: {registration['executable']}."
            dll_overrides = registration.get("nativeFirstDlls", [])
            if dll_overrides:
                message += f" Native-first DLL overrides applied: {len(dll_overrides)} ({', '.join(dll_overrides)})."
            emit("registered", message)
        else:
            result = highball.launch(receipt, lambda message: emit("running", message))
            atomic_json(path, receipt)
            if result.get("steamStarted"):
                emit("steamStarted", "Windows Steam opened at " + result["steamExecutable"] + ". Sign in, then click Play again.", **result)
                return 0
            message = "Launch requested through Highball; game startup is not yet verified. Log: " + result["logPath"]
            emit("launchRequested", message, **result)
        return 0
    except Stopped:
        emit("stopped", "Stopped. Partial downloads and completed archives are kept for resume.")
        return 3
    except Exception as exc:
        emit("failed", str(exc))
        return 2
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
