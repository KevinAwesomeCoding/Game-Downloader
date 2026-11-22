"""Volume-local workspace validation for Mac install jobs."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile


class WorkspaceError(ValueError):
    pass


MIN_INSTALL_FREE_BYTES = 1024 ** 3


def expected_workspace(job):
    job_id = str(job.get("id", ""))
    if not job_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for character in job_id):
        raise WorkspaceError("The install job identifier is invalid; no files were written.")
    base = Path(job.get("installFolder", "")).expanduser()
    if not base.is_absolute():
        raise WorkspaceError("The selected install destination is invalid. Choose the destination folder again.")
    return base / ".game-installer-jobs" / job_id


def _mounted_volume(base):
    """Reject missing /Volumes destinations instead of letting mkdir use the boot disk."""
    if not str(base).startswith("/Volumes/"):
        return
    volume_root = Path("/Volumes") / base.parts[2]
    if not volume_root.exists() or not os.path.ismount(volume_root):
        raise WorkspaceError(f"The selected volume for {base} is not mounted. Reconnect it, then resume this job.")
    try:
        base.resolve(strict=False).relative_to(volume_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise WorkspaceError(f"The selected destination is not on the mounted volume {volume_root}.") from error


def check_workspace(job, *, create=False, required_free=MIN_INSTALL_FREE_BYTES,
                    free_space=None, probe_writable=True):
    """Check the selected destination and ensure the workspace stays on its volume."""
    configured = job.get("workspacePath")
    workspace = Path(configured).expanduser() if configured else expected_workspace(job)
    if not workspace.is_absolute():
        raise WorkspaceError("The saved job workspace is invalid. Choose the destination folder again.")
    selected = Path(job["installFolder"]).expanduser()
    if (workspace.name != str(job["id"]) or workspace.parent.name != ".game-installer-jobs"
            or os.path.commonpath((os.path.abspath(workspace), os.path.abspath(selected))) != os.path.abspath(selected)):
        raise WorkspaceError("The saved job workspace must stay inside the selected folder and be organized by job ID.")
    try:
        _mounted_volume(selected)
        volume_anchor = selected
        while not volume_anchor.exists():
            parent = volume_anchor.parent
            if parent == volume_anchor:
                raise WorkspaceError(f"The selected volume or destination is unavailable: {selected}. Reconnect it, then resume.")
            volume_anchor = parent
        if not volume_anchor.is_dir():
            raise WorkspaceError("The selected install destination is not a folder.")
        volume_anchor = volume_anchor.resolve(strict=True)
        workspace_parent = workspace.parent
        while not workspace_parent.exists() and workspace_parent != workspace_parent.parent:
            workspace_parent = workspace_parent.parent
        if workspace_parent.resolve(strict=True).stat().st_dev != volume_anchor.stat().st_dev:
            raise WorkspaceError("The job workspace is on a different volume from the install destination.")
        for component in (workspace.parent, workspace, workspace / ".staging"):
            if component.is_symlink():
                raise WorkspaceError("A job workspace path is a symbolic link. It was not used.")
        free = (free_space or shutil.disk_usage)(volume_anchor).free
        if free < required_free:
            raise WorkspaceError(
                f"Not enough space on the selected volume. Need {required_free} bytes free; {free} bytes are available. "
                "Free space on that volume, then resume."
            )
        # A short-lived probe detects read-only volumes and permission changes.
        if probe_writable:
            descriptor, probe = tempfile.mkstemp(prefix=".game-installer-write-check-", dir=volume_anchor)
            os.close(descriptor)
            os.unlink(probe)
        if create:
            selected.mkdir(parents=True, exist_ok=True)
            if selected.resolve(strict=True).stat().st_dev != volume_anchor.stat().st_dev:
                raise WorkspaceError("The selected install destination is on a different volume.")
        if workspace.exists() and workspace.stat().st_dev != volume_anchor.stat().st_dev:
            raise WorkspaceError("The job workspace is not on the selected volume.")
        if create:
            workspace.mkdir(parents=True, exist_ok=True)
            if workspace.stat().st_dev != volume_anchor.stat().st_dev:
                raise WorkspaceError("The job workspace is not on the selected volume.")
        return workspace
    except WorkspaceError:
        raise
    except OSError as error:
        if error.errno in (28,):
            raise WorkspaceError("The selected volume is out of space. Free space on that volume, then resume.") from error
        if not selected.exists():
            raise WorkspaceError(f"The selected volume or destination is unavailable: {selected}. Reconnect it, then resume.") from error
        raise WorkspaceError(f"The selected volume is unavailable or not writable: {error}. Check it, then resume.") from error


def verify_tree(source, destination):
    """Check file paths, sizes, and content after a migration copy."""
    source, destination = Path(source), Path(destination)
    for current, directories, files in os.walk(source, followlinks=False):
        relative = Path(current).relative_to(source)
        target_dir = destination / relative
        if not target_dir.is_dir() or target_dir.is_symlink():
            return False
        if any((Path(current) / name).is_symlink() for name in directories + files):
            return False
        for name in files:
            original, copied = Path(current) / name, target_dir / name
            if not copied.is_file() or original.stat().st_size != copied.stat().st_size:
                return False
            def digest(path):
                value = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        value.update(block)
                return value.digest()
            if digest(original) != digest(copied):
                return False
    return True


def remove_legacy_cache_preserving_receipt(source):
    """Move a small job receipt back into internal metadata after archive migration."""
    source = Path(source)
    receipt = source / "receipt.json"
    receipt_data = receipt.read_bytes() if receipt.is_file() and not receipt.is_symlink() else None
    shutil.rmtree(source)
    if receipt_data is not None:
        source.mkdir(parents=True)
        (source / "receipt.json").write_bytes(receipt_data)


def migrate_legacy_workspace(job, job_file, legacy_workspace, notify):
    """Explicitly move legacy job files after verified copy; never replace a target."""
    from downloads import atomic_json

    source = Path(legacy_workspace)
    configured = job.get("workspacePath")
    if configured and Path(configured).expanduser().absolute() != source.absolute():
        raise WorkspaceError("The saved workspace is not the old internal job folder; it was left untouched.")
    if source.is_symlink():
        raise WorkspaceError("The old job workspace is a symbolic link; migration stopped without changing it.")
    target = expected_workspace(job)
    target_job = dict(job, workspacePath=str(target))
    if target.exists():
        if source.exists() and verify_tree(source, target):
            check_workspace(target_job, required_free=0)
            job["workspacePath"] = str(target)
            atomic_json(job_file, job)
            remove_legacy_cache_preserving_receipt(source)
            notify("migrated", "Previously copied job files passed verification and were moved to the selected volume.",
                   workspacePath=str(target))
            return
        raise WorkspaceError(f"The destination workspace already exists at {target}; no files were overwritten.")
    total = sum(path.stat().st_size for path in source.rglob("*") if path.is_file()) if source.exists() else 0
    check_workspace(target_job, required_free=total + 1024 * 1024)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".migration-" + str(os.getpid()))
    if staging.exists():
        raise WorkspaceError(f"A previous migration staging folder exists at {staging}; it was left untouched.")
    try:
        if source.exists():
            shutil.copytree(source, staging, symlinks=True)
        else:
            staging.mkdir()
        check_workspace(target_job, required_free=0)
        if source.exists() and not verify_tree(source, staging):
            raise WorkspaceError("Copied job files did not pass verification. The original files were kept.")
        staging.rename(target)
        job["workspacePath"] = str(target)
        atomic_json(job_file, job)
        # This operation is user initiated. The verified destination is now the source of truth.
        if source.exists():
            remove_legacy_cache_preserving_receipt(source)
        notify("migrated", "Existing job files were copied, verified, and moved to the selected volume.",
               workspacePath=str(target))
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
