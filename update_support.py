"""Platform-neutral release selection and update-notice state."""

from dataclasses import dataclass
import hashlib
import os
import re
import requests


def normalize_gitlab_release(release: dict, checksums: dict) -> dict:
    """Map GitLab's release links and checksum manifest to the updater model."""
    links = release.get("assets", {}).get("links", [])
    assets = []
    for link in links:
        name = str(link.get("name") or "")
        url = link.get("direct_asset_url") or link.get("url")
        if not name or not url:
            continue
        digest = str(checksums.get(name) or "").lower()
        assets.append({
            "name": name,
            "browser_download_url": url,
            "digest": "sha256:" + digest if re.fullmatch(r"[0-9a-f]{64}", digest) else None,
        })
    return {
        "tag_name": release.get("tag_name"),
        "body": release.get("description"),
        "html_url": release.get("_links", {}).get("self", ""),
        "assets": assets,
    }


def version_key(tag: str) -> tuple[int, ...]:
    """Order project tags such as v1.4-20261027-23 by version/date/run."""
    core, *suffix = tag.lstrip("v").split("-")
    try:
        version = [int(part) for part in core.split(".")]
        sequence = [int(part) for part in suffix if part.isdigit()]
    except ValueError:
        return (0, 0, 0)
    while len(version) < 3:
        version.append(0)
    return tuple(version + sequence)


@dataclass(frozen=True)
class UpdateRelease:
    tag: str
    notes: str
    release_url: str
    download_url: str | None
    sha256: str | None
    manual_reason: str | None = None

    @property
    def self_updatable(self) -> bool:
        return bool(self.download_url and self.sha256 and self.manual_reason is None)


def compatible_release(release: dict, current_version: str, system: str, machine: str,
                       packaged: bool = True) -> UpdateRelease | None:
    """Find a newer release for this binary target; this app ships Windows x64."""
    if system.lower() not in ("windows", "win32") or machine.lower() not in ("amd64", "x86_64"):
        return None
    tag = str(release.get("tag_name") or "")
    if not tag or version_key(tag) <= version_key(current_version):
        return None
    assets = release.get("assets", [])
    asset = next((item for name in ("GameInstaller-windows-x64.exe", "GameInstaller.exe")
                  for item in assets if item.get("name") == name), None)
    digest = str((asset or {}).get("digest") or "").removeprefix("sha256:").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        digest = None
    manual_reason = None
    if not asset:
        manual_reason = "This release has no Windows x64 app download. Review the release page manually."
    elif not digest:
        manual_reason = "This release has no valid SHA-256 checksum. Review and download it manually."
    elif not packaged:
        manual_reason = "Self-updates are available only in the packaged Windows app. Download and replace it manually."
    return UpdateRelease(
        tag=tag,
        notes=str(release.get("body") or "").strip(),
        release_url=str(release.get("html_url") or ""),
        download_url=(str(asset.get("browser_download_url")) if asset and asset.get("browser_download_url") else None),
        sha256=digest,
        manual_reason=manual_reason,
    )


def verify_file_sha256(path: str, expected: str) -> bool:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().lower() == expected.lower()


def schedule_background_check(single_shot, callback, delay_ms: int = 15_000) -> None:
    """Schedule work on the GUI's event loop without executing it inline."""
    single_shot(delay_ms, callback)


def download_verified_file(url: str, destination: str, expected_sha256: str,
                           get=requests.get, progress=None) -> str:
    """Stream and verify a release asset, deleting partial or invalid files."""
    try:
        with get(url, stream=True, timeout=(10, 30)) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            digest = hashlib.sha256()
            with open(destination, "wb") as output:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    output.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(int(done * 100 / total) if total else -1)
        if digest.hexdigest().lower() != expected_sha256.lower():
            raise ValueError("The update checksum did not match.")
        return destination
    except Exception:
        try:
            os.remove(destination)
        except OSError:
            pass
        raise


def windows_update_script(new_exe: str, current_exe: str, pid: int, status_file: str) -> str:
    """Build the Windows-only deferred swap; current_exe stays untouched until exit."""
    return (
        "@echo off\n"
        "chcp 65001 >nul\n"
        f'powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -Command "Wait-Process -Id {pid} -ErrorAction SilentlyContinue" >NUL 2>&1\n'
        f'if not exist "{new_exe}" (echo failed>"{status_file}" & start "" "{current_exe}" & del "%~f0" & exit /b 1)\n'
        f'move /y "{new_exe}" "{current_exe}"\n'
        f'if errorlevel 1 (echo failed>"{status_file}" & start "" "{current_exe}" & del "%~f0" & exit /b 1)\n'
        f'if not exist "{current_exe}" (echo failed>"{status_file}" & exit /b 1)\n'
        f'echo installed>"{status_file}"\n'
        f'start "" "{current_exe}"\n'
        'del "%~f0"\n'
    )


class UpdateNoticeState:
    """Keep an offered release visible while details can be dismissed/reopened."""

    def __init__(self):
        self.release: UpdateRelease | None = None
        self.details_open = False

    @property
    def indicator_visible(self) -> bool:
        return self.release is not None

    def offer(self, release: UpdateRelease) -> None:
        self.release = release

    def open_details(self) -> UpdateRelease | None:
        if self.release is not None:
            self.details_open = True
        return self.release

    def later(self) -> None:
        self.details_open = False
