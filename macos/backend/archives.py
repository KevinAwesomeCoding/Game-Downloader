"""Extract into fresh staging only; validate paths before using native tools."""
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unicodedata
import zipfile

from downloads import checkpoint

PASSWORD = b"online-fix.me"


def safe_path(name):
    name = name.replace("\\", "/")
    path = PurePosixPath(name)
    if not name or "\0" in name or ":" in name or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe archive path: {name!r}")
    if not path.parts or any(ord(c) < 32 for c in name):
        raise ValueError("Archive contains an invalid filename.")
    return Path(*path.parts)


def validate_entries(entries):
    seen = set()
    for name, is_link in entries:
        safe_path(name)
        key = unicodedata.normalize("NFD", str(safe_path(name))).casefold()
        if is_link or key in seen:
            raise ValueError(f"Archive contains a link or duplicate/colliding path: {name}")
        seen.add(key)


def find_tool(name):
    # Archive helpers ship inside the app; no Homebrew setup is required.
    if name in {"unrar", "7zz"}:
        bundled = Path(__file__).resolve().parent / "Tools" / name
        if os.access(bundled, os.X_OK):
            return str(bundled)
        configured = os.environ.get(f"GAME_DOWNLOADER_{name.upper()}", "")
        if configured and os.access(configured, os.X_OK):
            return configured
    for directory in ["/opt/homebrew/bin", "/usr/local/bin"]:
        candidate = Path(directory) / name
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def run_tool(command, stop=lambda: False):
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        try:
            while process.poll() is None:
                checkpoint(stop)
                time.sleep(0.1)
            log.seek(0)
            output = log.read().decode("utf-8", "replace")
            if process.returncode:
                raise ValueError("Archive tool failed: " + output[-1000:])
            return output
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def list_archive_entries(archive, stop=lambda: False):
    """Return validated 7-Zip technical entries for RAR and 7z archives."""
    seven_zip = find_tool("7zz")
    if not seven_zip:
        raise ValueError("The bundled archive helper is missing. Reinstall Game Installer.")
    output = run_tool([seven_zip, "l", "-slt", "-ba", "-sccUTF-8",
                       f"-p{PASSWORD.decode()}", str(archive)], stop)
    entries = []
    for block in re.split(r"\n\s*\n", output.strip()):
        fields = {}
        for line in block.splitlines():
            key, separator, value = line.partition(" = ")
            if separator:
                if key in fields:
                    raise ValueError("Archive listing contains ambiguous entry metadata.")
                fields[key] = value
        name = fields.get("Path")
        if not name:
            continue
        attributes = fields.get("Attributes", "")
        is_directory = fields.get("Folder") == "+" or attributes.strip().startswith("D")
        is_link = bool(fields.get("Symbolic Link") or fields.get("Hard Link") or
                        " lrwx" in attributes.lower())
        try:
            size = int(fields.get("Size", "0"))
        except ValueError as exc:
            raise ValueError(f"Archive returned an invalid size for: {name}") from exc
        entries.append({"name": name, "size": size, "is_directory": is_directory,
                        "is_link": is_link})
    if not entries:
        raise ValueError("Archive tool returned an unsupported or empty listing.")
    validate_entries([(entry["name"], entry["is_link"]) for entry in entries])
    return entries


def verify_extracted_entries(entries, destination):
    destination = Path(destination)
    for entry in entries:
        if entry["is_directory"]:
            continue
        target = destination / safe_path(entry["name"])
        if (not target.is_file() or target.is_symlink() or
                target.stat().st_size != entry["size"]):
            raise ValueError(f"Archive entry did not extract completely: {entry['name']}")
    for current, directories, files in os.walk(destination):
        for name in directories + files:
            if (Path(current) / name).is_symlink():
                raise ValueError("Extracted symbolic links are not supported.")


def extract_with_7zz(archive, destination, stop=lambda: False,
                     progress=lambda done, total: None):
    destination = Path(destination)
    entries = list_archive_entries(archive, stop)
    required = sum(entry["size"] for entry in entries)
    if required > shutil.disk_usage(destination).free:
        raise ValueError("Not enough free space to extract this archive.")
    seven_zip = find_tool("7zz")
    run_tool([seven_zip, "x", "-y", "-bso0", "-bsp0",
              f"-p{PASSWORD.decode()}", f"-o{destination}", str(archive)], stop)
    verify_extracted_entries(entries, destination)
    progress(len(entries), len(entries))


def is_rar_volume_set(archives, stop=lambda: False):
    """Detect a RAR5 volume set from UnRAR's parsed archive metadata."""
    archives = [Path(item) for item in archives]
    unrar = find_tool("unrar")
    if len(archives) < 2 or not unrar:
        return False
    for archive in archives:
        with archive.open("rb") as stream:
            if not stream.read(8).startswith(b"Rar!\x1a\x07"):
                return False
    listing = run_tool([unrar, "l", "-v", f"-p{PASSWORD.decode()}", str(archives[0])], stop)
    return bool(re.search(r"^Details:\s+RAR\s+\d+,\s+volume\s+1\b", listing, re.IGNORECASE | re.MULTILINE))


def extract_rar_volume_set(archives, destination, stop=lambda: False,
                           progress=lambda done, total: None):
    """Extract a complete RAR volume set once, starting at its first volume."""
    archives, destination = [Path(item) for item in archives], Path(destination)
    unrar = find_tool("unrar")
    if not unrar:
        raise ValueError("This split RAR build needs the bundled UnRAR helper. Rebuild or reinstall the Game Installer app.")
    if not archives or not is_rar_volume_set(archives, stop):
        raise ValueError("The supplied files are not a complete RAR volume set.")

    entries = list_archive_entries(archives[0], stop)
    required = sum(entry["size"] for entry in entries)
    destination.mkdir()
    if required > shutil.disk_usage(destination).free:
        raise ValueError("Not enough free space to extract this archive.")
    run_tool([unrar, "x", "-idq", "-o-", f"-p{PASSWORD.decode()}",
              str(archives[0]), str(destination) + os.sep], stop)
    verify_extracted_entries(entries, destination)
    progress(len(entries), len(entries))


def extract(archive, destination, stop=lambda: False, progress=lambda done, total: None):
    archive, destination = Path(archive), Path(destination)
    destination.mkdir()  # Never extract onto existing files, even in a failed staging directory.
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zipped:
            entries = zipped.infolist()
            validate_entries([(entry.filename, stat.S_ISLNK(entry.external_attr >> 16)) for entry in entries])
            required = sum(entry.file_size for entry in entries)
            if required > shutil.disk_usage(destination).free:
                raise ValueError("Not enough free space to extract this archive.")
            last_report = 0
            for index, entry in enumerate(entries):
                checkpoint(stop)
                target = destination / safe_path(entry.filename)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with zipped.open(entry, pwd=PASSWORD) as source, target.open("xb") as output:
                            while chunk := source.read(256 * 1024):
                                checkpoint(stop)
                                output.write(chunk)
                    except NotImplementedError as exc:
                        raise ValueError("This ZIP encryption/compression is not supported by the preview.") from exc
                now = time.monotonic()
                if now - last_report >= 0.2 or index + 1 == len(entries):
                    progress(index + 1, len(entries))
                    last_report = now
    else:
        with archive.open("rb") as stream:
            signature = stream.read(8)
        if not signature.startswith((b"Rar!\x1a\x07", b"7z\xbc\xaf\x27\x1c")):
            raise ValueError("Downloaded file is not a supported ZIP, RAR, or 7z archive.")
        extract_with_7zz(archive, destination, stop, progress)


def content_root(directory):
    directory = Path(directory)
    while True:
        children = list(directory.iterdir())
        if len(children) == 1 and children[0].is_dir() and not children[0].is_symlink():
            directory = children[0]
        else:
            return directory


def merge_archive_part(source, target):
    """Merge a separately packaged catalog archive into the staged game tree.

    Catalog entries may list several independent archives. They are extracted
    separately so every archive's manifest is checked, then combined without
    allowing one volume to silently overwrite another.
    """
    source, target = Path(source), Path(target)
    target.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        relative = safe_path(str(item.relative_to(source)))
        destination = target / relative
        if item.is_symlink() or destination.is_symlink():
            raise ValueError("Archive contains a symbolic link.")
        if item.is_dir():
            if destination.exists() and not destination.is_dir():
                raise ValueError(f"Archive parts collide at: {relative}")
            destination.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            if destination.exists():
                raise ValueError(f"Archive parts contain a duplicate file: {relative}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, destination)
        else:
            raise ValueError(f"Archive contains an unsupported filesystem entry: {relative}")


def merge_repair(source, target, stop=lambda: False):
    """Merge into the new staged installation, never into an existing user's game."""
    source, target = Path(source), Path(target)
    while True:
        children = list(source.iterdir())
        if any((target / item.name).exists() for item in children):
            break
        if len(children) == 1 and children[0].is_dir():
            source = children[0]
        else:
            break
    for item in source.rglob("*"):
        checkpoint(stop)
        relative = safe_path(str(item.relative_to(source)))
        destination = target / relative
        if item.is_symlink() or destination.is_symlink():
            raise ValueError("Repair contains a symbolic link.")
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with item.open("rb") as source_file, destination.open("wb") as target_file:
                while chunk := source_file.read(256 * 1024):
                    checkpoint(stop)
                    target_file.write(chunk)
