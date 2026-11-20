"""Platform-neutral release selection and update-notice state."""

from dataclasses import dataclass, field
from datetime import datetime
import base64
import hashlib
import os
import re
from urllib.parse import quote
import requests


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
    commit_title: str = ''
    commit_sha: str = ''
    created_at: str = ''
    commit_history: list = field(default_factory=list)
    tag_commits: dict = field(default_factory=dict)

    @property
    def display_changes(self) -> str:
        if self.commit_title:
            suffix = f' ({self.commit_sha[:8]})' if self.commit_sha else ''
            return f'Latest release commit: {self.commit_title}{suffix}'
        return 'Change details are unavailable for this older release.'

    def changes_for(self, installed_tag: str) -> str:
        base = self.tag_commits.get(installed_tag)
        if isinstance(base, str):
            entries = []
            for item in self.commit_history:
                if not isinstance(item, dict):
                    break
                sha, title = item.get('sha'), item.get('title')
                if not isinstance(sha, str) or not isinstance(title, str):
                    break
                if sha == base:
                    return 'Changes since your installed version:\n' + ('\n'.join(entries) if entries else 'No newer commits.')
                entries.append(f'• {title} ({sha[:8]})')
        return self.display_changes + '\nFull changes since your installed version are unavailable.'

    @property
    def build_time(self) -> str | None:
        if not self.created_at:
            return None
        try:
            date = datetime.fromisoformat(self.created_at.replace('Z', '+00:00'))
            if date.tzinfo is None:
                return None
            return date.astimezone().strftime('%B %d, %Y at %I:%M %p %Z')
        except ValueError:
            return None

    @property
    def build_date(self) -> str | None:
        """Human-readable UTC build date from the generated release tag."""
        match = re.fullmatch(r'v?\d+(?:\.\d+)*-(\d{8})-\d+', self.tag)
        if not match:
            return None
        try:
            date = datetime.strptime(match.group(1), '%Y%m%d')
        except ValueError:
            return None
        return f'{date:%B} {date.day}, {date.year} (UTC)'

    @property
    def self_updatable(self) -> bool:
        return bool(self.download_url and self.sha256 and self.manual_reason is None)


def compatible_release(release: dict, current_version: str, system: str, machine: str,
                       packaged: bool = True) -> UpdateRelease | None:
    """Find a newer release for this binary target; this app ships Windows x64."""
    if system.lower() not in ("windows", "win32") or machine.lower() not in ("amd64", "x86_64"):
        return None
    # Newer manifests can carry a complete release per platform. Legacy
    # manifests continue to use the shared top-level release unchanged.
    platforms = release.get('platforms')
    if isinstance(platforms, dict):
        platform_release = platforms.get('windows')
        if not isinstance(platform_release, dict):
            return None
        release = platform_release
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
        commit_title=str(release.get('commit_title') or '').strip(),
        commit_sha=str(release.get('commit_sha') or '').strip(),
        created_at=str(release.get('created_at') or '').strip(),
        commit_history=release.get('commit_history') if isinstance(release.get('commit_history'), list) else [],
        tag_commits=release.get('tag_commits') if isinstance(release.get('tag_commits'), dict) else {},
    )


def release_change_history(installed_tag: str, update_tag: str, get=requests.get) -> dict | None:
    """Load commits between two release tags from GitHub's compare API."""
    if not installed_tag or not update_tag or installed_tag == update_tag:
        return None
    base = quote(installed_tag, safe="")
    head = quote(update_tag, safe="")
    url = ("https://api.github.com/repos/KevinAwesomeCoding/Game-Downloader/compare/"
           f"{base}...{head}")
    try:
        response = get(url, timeout=(5, 10), headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "GameInstaller-updater",
        })
        response.raise_for_status()
        comparison = response.json()
        base_sha = (comparison.get("base_commit") or {}).get("sha")
        commits = comparison.get("commits")
        if not isinstance(base_sha, str) or not isinstance(commits, list):
            return None
        history = []
        for commit in commits:
            if not isinstance(commit, dict):
                continue
            sha = commit.get("sha")
            message = (commit.get("commit") or {}).get("message")
            title = message.splitlines()[0].strip() if isinstance(message, str) else ""
            if isinstance(sha, str) and title:
                history.append({"sha": sha, "title": title})
        latest = history[-1] if history else {}
        # Match the legacy manifest shape consumed by changes_for(): a base
        # commit at the tail marks the end of the requested comparison range.
        history.append({"sha": base_sha, "title": ""})
        return {
            "commit_history": history,
            "tag_commits": {installed_tag: base_sha},
            "commit_title": latest.get("title", ""),
            "commit_sha": latest.get("sha", ""),
        }
    except Exception:
        return None


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
    """Build a deferred updater that waits for every process locking current_exe."""
    target = current_exe.replace("'", "''")
    wait_script = f"""
$target = [System.IO.Path]::GetFullPath('{target}')
$updaterPid = {pid}
$imageName = [System.IO.Path]::GetFileName($target).Replace("'", "''")
$filter = "Name = '$imageName'"
$deadline = [DateTime]::UtcNow.AddSeconds(120)
while ([DateTime]::UtcNow -lt $deadline) {{
    try {{
        $processes = @(Get-CimInstance Win32_Process -Filter $filter -ErrorAction Stop)
    }} catch {{
        exit 2
    }}
    $running = @($processes | Where-Object {{
        $_.ProcessId -eq $updaterPid -or
        (-not $_.ExecutablePath) -or
        [String]::Equals([System.IO.Path]::GetFullPath($_.ExecutablePath), $target,
            [StringComparison]::OrdinalIgnoreCase)
    }})
    if ($running.Count -eq 0) {{
        Start-Sleep -Milliseconds 1200
        exit 0
    }}
    Start-Sleep -Milliseconds 500
}}
exit 1
"""
    encoded_wait_script = base64.b64encode(wait_script.encode("utf-16le")).decode("ascii")
    return (
        "@echo off\n"
        "chcp 65001 >nul\n"
        f'powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand {encoded_wait_script} >NUL 2>&1\n'
        'if errorlevel 1 goto update_failed\n'
        f'if not exist "{new_exe}" goto update_failed\n'
        'set "attempt=0"\n'
        ':replace_update\n'
        f'move /y "{new_exe}" "{current_exe}" >NUL\n'
        'if not errorlevel 1 goto update_installed\n'
        'set /a attempt+=1 >NUL\n'
        'if %attempt% geq 30 goto update_failed\n'
        'timeout /t 1 /nobreak >NUL\n'
        'goto replace_update\n'
        ':update_installed\n'
        f'if not exist "{current_exe}" goto update_failed\n'
        'set "PYINSTALLER_RESET_ENVIRONMENT=1"\n'
        f'start "" "{current_exe}"\n'
        f'echo installed>"{status_file}"\n'
        'del "%~f0"\n'
        'exit /b 0\n'
        ':update_failed\n'
        f'echo failed>"{status_file}"\n'
        'set "PYINSTALLER_RESET_ENVIRONMENT=1"\n'
        f'if exist "{current_exe}" start "" "{current_exe}"\n'
        'del "%~f0"\n'
        'exit /b 1\n'
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
