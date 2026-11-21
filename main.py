"""
Game Installer — a lightweight Windows desktop game launcher / installer.

Built with PyQt6 + requests. Two-stage install:
  1) download + extract the main game archive into the chosen folder
  2) (optional) download a repair/fix archive and merge it on top, overwriting
     matching files and merging folders recursively — with smart
     wrapper-folder detection so the patch never nests one level too deep.

Archives may be ZIP (handled natively with the zipfile module), RAR, or 7Z
(both extracted via a bundled 7-Zip binary). The type is detected from the file
signature, falling back to the URL extension. ZIP_PASSWORD is passed
automatically for all password-protected archives; non-protected archives
ignore it silently.

Threading model
---------------
Every long-running task runs OFF the GUI thread:
  - ManifestLoader (QObject)  -> its own QThread
  - InstallWorker  (QObject)  -> its own QThread
  - thumbnail fetches         -> QThreadPool / QRunnable
Workers only ever emit Qt signals. All widget mutation happens in GUI-thread
slots. Cancellation uses a threading.Event polled inside the download and
extraction/merge loops — the thread is never force-killed.
"""

import ctypes
import hashlib
import logging
import os
import platform
import sys
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.request
import webbrowser
import winreg
import zipfile
from datetime import datetime

import requests
from download_support import (Downloader as HostedDownloader, Stopped as DownloadStopped,
                              canonical_source, choose_faster_source, download_host_name)
from host_resolver_client import provider_for, helper_command, resolve_source
from update_support import (UpdateNoticeState, UpdateRelease, compatible_release,
                            download_verified_file, schedule_background_check,
                            release_change_history, windows_update_script)
from search_support import matches_game_search
from macos.backend.itch_login import sign_in as itch_sign_in
from desktop_profile import desktop_profile
try:
    from PyQt6.QtCore import (
        Qt, QObject, QThread, QThreadPool, QRunnable, QSize, QRect, QPoint,
        QTimer, QVariantAnimation, QEasingCurve, QRectF, pyqtSignal, pyqtSlot,
    )
    from PyQt6.QtGui import QPixmap, QImage, QPainter, QPainterPath, QCursor, QColor, QFont, QFontDatabase, QPen
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout,
        QHBoxLayout, QScrollArea, QFrame, QStackedWidget, QDialog, QLineEdit,
        QProgressBar, QFileDialog, QMessageBox, QSizePolicy, QLayout, QCheckBox,
        QComboBox, QPlainTextEdit, QMenu, QGraphicsOpacityEffect, QGraphicsBlurEffect, QGraphicsEffect,
    )
except ImportError as _qt_err:
    _msg = (
        f"Could not load Qt6 libraries:\n{_qt_err}\n\n"
        "If you are running through CrossOver or Wine:\n"
        "  1. Use the GameInstaller.zip release (not a single .exe).\n"
        "  2. Install 'Visual C++ Redistributable 2019' inside your bottle\n"
        "     (Bottle → Install Software → search vcrun2019).\n"
        "  3. Run GameInstaller.exe from the extracted folder, not from inside the zip."
    )
    try:
        ctypes.windll.user32.MessageBoxW(0, _msg, "GameInstaller — Qt load error", 0x10)
    except Exception:
        print(_msg, file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
CONFIG = {
    "manifest_url": "https://raw.githubusercontent.com/KevinAwesomeCoding/Game-Downloader/main/games.json",
    "window_title": "Game Installer",
    "default_install_subfolder": True
}
PREVIOUS_CATALOG_URLS = {
    "https://gitlab.com/kevinanil-projects/Game-Downloader/-/raw/main/games.json",
    "https://gitlab.com/api/v4/projects/88321703/releases/permalink/latest/downloads/games.json",
    "https://gitlab.com/kevinanil-projects/Game-Downloader/-/releases/permalink/latest/downloads/games.json",
}

# Bundled fallback manifest, looked up next to this file / the exe.
LOCAL_MANIFEST = "games.json"
CATALOG_AUTO_REFRESH_INTERVAL_MS = 5_000
ZIP_PASSWORD = b"online-fix.me"

# Network timeouts (connect, read) in seconds, and download chunk size.
REQUEST_TIMEOUT = (10, 30)
DOWNLOAD_CHUNK = 64 * 1024  # 64 KiB

# Optional Google Drive API key for the alt=media download fallback.
# Leave as "" to disable the API fallback (interstitial scraping still works).
GDRIVE_API_KEY = "AIzaSyBWA4KDNJFzRhhRW7HnA6HeGdMiN39MDtg"

SPACEWAR_APP_ID = 480
SETTINGS_FILE = "settings.json"

# Current build version.  Set this to match the semver in the VERSION file
# before each release so the self-update check knows what is installed.
VERSION = "v1.0.0"
RELEASES_API = "https://api.github.com/repos/KevinAwesomeCoding/Game-Downloader/releases?per_page=100"

# ---------------------------------------------------------------------------
# DEBUG — temporary instrumentation to diagnose fixZipUrl download failures.
# Remove this block and all _log.* calls once the root cause is confirmed.
# Log file: %TEMP%\gameinstaller_debug.log  (overwritten on each launch)
# ---------------------------------------------------------------------------
_DEBUG_LOG = os.path.join(tempfile.gettempdir(), "gameinstaller_debug.log")
_log = logging.getLogger("GameInstaller")
_log.setLevel(logging.DEBUG)
if not _log.handlers:
    _fh = logging.FileHandler(_DEBUG_LOG, mode="w", encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
    _log.addHandler(_fh)

# ---------------------------------------------------------------------------
# UPDATER DEBUG LOG — persists across runs (append mode), written next to the
# exe so it is easy to find.  Each line is timestamped.
# Log file: <app_dir>\updater_debug.log
# ---------------------------------------------------------------------------
def _make_updater_logger() -> logging.Logger:
    """Create (or retrieve) the updater-specific logger.

    Called once at import time; safe to call again (idempotent thanks to the
    handler-existence check).
    """
    logger = logging.getLogger("GameInstaller.updater")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        # app_dir() is not yet defined here, so we compute it inline.
        if getattr(sys, "frozen", False):
            _base = os.path.dirname(sys.executable)
        else:
            _base = os.path.dirname(os.path.abspath(__file__))
        _ulog_path = os.path.join(_base, "updater_debug.log")
        _ufh = logging.FileHandler(_ulog_path, mode="a", encoding="utf-8")
        _ufh.setFormatter(
            logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s")
        )
        logger.addHandler(_ufh)
        # Prevent messages from bubbling up to the root/gameinstaller handler.
        logger.propagate = False
    return logger

_ulog = _make_updater_logger()


def app_dir() -> str:
    """Directory of the running app (works for source and PyInstaller exe)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.environ.get("GAMEINSTALLER_LAUNCHER", sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# Hide the console window when launching 7-Zip from a windowed app. bruh
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED_PROCESS = 0x00000008  # Windows: child survives after parent exits
_SEVENZIP_PATH = None  # cached result of find_7zip()
_SEVENZIP_SEARCHED = False


def find_7zip():
    """Locate a 7-Zip executable (cached).

    Checks for a bundled copy first (placed next to the exe by PyInstaller),
    then falls back to any system install on PATH or in Program Files.
    Returns the executable path, or None if 7-Zip is unavailable.
    """
    global _SEVENZIP_PATH, _SEVENZIP_SEARCHED
    if _SEVENZIP_SEARCHED:
        return _SEVENZIP_PATH

    # 1. Bundled copy — present in both onedir and onefile PyInstaller builds.
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        candidate = os.path.join(base, "bin", "7z.exe")
        if os.path.exists(candidate):
            _SEVENZIP_PATH = candidate
            _SEVENZIP_SEARCHED = True
            return _SEVENZIP_PATH

    # 2. System install fallback (useful during development).
    candidates = []
    for name in ("7z", "7za", "7zr"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env)
        if base:
            candidates.append(os.path.join(base, "7-Zip", "7z.exe"))

    _SEVENZIP_PATH = next((p for p in candidates if p and os.path.exists(p)), None)
    _SEVENZIP_SEARCHED = True
    return _SEVENZIP_PATH


def _parse_version(tag: str) -> tuple:
    """Compare semantic version first, then the date/run suffix used by CI."""
    core, *suffix = tag.lstrip("v").split("-")
    parts = core.split(".")
    try:
        semantic = tuple(int(p) for p in parts)
    except ValueError:
        return (0, 0, 0)
    extra = tuple(int(p) for p in suffix if p.isdigit())
    return semantic + extra


def _launch_update_bat(new_exe: str, current_exe: str) -> None:
    """Write and launch a detached .bat that waits for this process to exit,
    atomically replaces current_exe with new_exe, relaunches, then self-deletes.

    On Windows a running exe cannot be overwritten directly, so the bat polls
    for the PID to disappear before issuing the move.
    """
    pid = os.getpid()
    os.makedirs(_prefs_dir(), exist_ok=True)
    status_path = os.path.join(_prefs_dir(), "update-status.txt")
    fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="gameupdate_")
    os.close(fd)
    script = windows_update_script(new_exe, current_exe, pid, status_path)
    with open(bat_path, "w", encoding="utf-8") as fh:
        fh.write(script)
    subprocess.Popen(
        ["cmd.exe", "/c", bat_path],
        creationflags=_DETACHED_PROCESS | _NO_WINDOW,
        close_fds=True,
    )


# ---------------------------------------------------------------------------
# App settings — lightweight JSON file stored next to the exe.
# ---------------------------------------------------------------------------
def load_settings() -> dict:
    path = os.path.join(app_dir(), SETTINGS_FILE)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(data: dict):
    path = os.path.join(app_dir(), SETTINGS_FILE)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass  # settings are best-effort; never crash for this


# ---------------------------------------------------------------------------
# User preferences — stored in %APPDATA%\GameInstaller\prefs.json so they
# follow the user across app versions and download locations.
# (Contrast with settings.json above, which lives next to the exe and resets
# intentionally when the user downloads a fresh copy.)
# ---------------------------------------------------------------------------
def _prefs_dir() -> str:
    return os.path.join(os.environ.get("APPDATA", app_dir()), "GameInstaller")


def load_prefs() -> dict:
    path = os.path.join(_prefs_dir(), "prefs.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict) and data.get("catalog_url") in PREVIOUS_CATALOG_URLS:
                data["catalog_url"] = CONFIG["manifest_url"]
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_prefs(data: dict):
    d = _prefs_dir()
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "prefs.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        return True
    except OSError:
        _log.exception("Could not save preferences")
        return False


def user_catalog_path() -> str:
    return os.path.join(_prefs_dir(), "games.json")


def normalize_catalog_url(value: str) -> str:
    """Convert a Pastebin page link to its raw JSON endpoint; keep raw URLs."""
    from urllib.parse import urlparse
    value = (value or "").strip()
    if not value:
        raise ValueError("Enter a catalog URL first.")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Catalog links must use HTTPS.")
    host = parsed.hostname.casefold()
    if host in {"pastebin.com", "www.pastebin.com"}:
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[0] == "raw":
            return f"https://pastebin.com/raw/{parts[-1]}"
        if parts:
            return f"https://pastebin.com/raw/{parts[-1]}"
        raise ValueError("That Pastebin link does not include a paste ID.")
    return value


def consume_update_status() -> str:
    path = os.path.join(_prefs_dir(), "update-status.txt")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            status = fh.read().strip()
        os.remove(path)
        return status
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Persistent download cache — partially-downloaded archives live here (keyed by
# game + stage + source URLs) so that a failed or interrupted download can be
# *resumed* on the next attempt instead of restarting from zero. Game downloads
# live beside the selected destination on the same volume; entries are removed
# on successful installation. The legacy local-app-data cache is swept at startup.
# ---------------------------------------------------------------------------
CACHE_MAX_AGE_DAYS = 14  # stale partials older than this are swept on startup


def _cache_job_id(game_id, dest_path):
    identity = os.path.normcase(os.path.abspath(os.path.expanduser(dest_path)))
    return hashlib.sha256(f"{game_id}|{identity}".encode("utf-8")).hexdigest()[:24]


def _cache_root(dest_path=None, game_id="job") -> str:
    if dest_path:
        # Keep archives and extraction staging on the selected volume. Never
        # let tempfile or a failed mkdir silently redirect work to app storage.
        d = os.path.join(os.path.dirname(os.path.abspath(dest_path)),
                         ".gameinstaller-cache", _cache_job_id(game_id, dest_path))
        ensure_destination_volume(dest_path)
        os.makedirs(d, exist_ok=True)
        return d
    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or app_dir())
    d = os.path.join(base, "GameInstaller", "cache")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


MIN_INSTALL_FREE_BYTES = 1024 ** 3


def ensure_destination_volume(dest_path, minimum_free=MIN_INSTALL_FREE_BYTES):
    """Fail closed if the chosen destination volume is absent, read-only, or full."""
    target = os.path.abspath(os.path.expanduser(dest_path))
    drive, _ = os.path.splitdrive(target)
    if os.name == "nt":
        root = drive + os.sep if drive else os.path.abspath(os.sep)
        if not os.path.isdir(root):
            raise OSError(f"The selected install volume {root} is unavailable. Reconnect it and retry.")
    else:
        root = os.path.abspath(os.sep)
        for candidate in (target, os.path.dirname(target), os.path.dirname(os.path.dirname(target))):
            if os.path.ismount(candidate):
                root = candidate
                break
    probe_dir = os.path.dirname(target)
    while not os.path.isdir(probe_dir):
        parent = os.path.dirname(probe_dir)
        if parent == probe_dir:
            raise OSError(f"The selected install volume for {target} is unavailable. Reconnect it and retry.")
        probe_dir = parent
    try:
        fd, probe = tempfile.mkstemp(prefix=".gameinstaller-write-check-", dir=probe_dir)
        os.close(fd)
        os.remove(probe)
    except OSError as exc:
        raise OSError(f"The selected install volume is not writable ({probe_dir}). Check its connection and permissions, then retry. {exc}") from exc
    try:
        free = shutil.disk_usage(probe_dir).free
    except OSError as exc:
        raise OSError(f"Could not check free space on the selected install volume: {exc}") from exc
    if free < minimum_free:
        raise OSError(f"The selected install volume has only {free // (1024 ** 2)} MB free. Free at least {minimum_free // (1024 ** 3)} GB, then retry.")


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def cache_set_dir(game_id: str, kind: str, parts, dest_path=None) -> str:
    """Return (creating if needed) the folder that holds every volume of one
    download set. Stable across retries for the same game+stage+URLs, so the
    ``.part`` files from an earlier attempt are found and resumed.

    All volumes of a multi-part set share this one folder, which is also what
    7-Zip needs to pair sibling ``.partN.rar`` files during extraction.
    """
    urls = "|".join((p.get("url") or "") for p in (parts or []))
    identity = os.path.normcase(os.path.abspath(dest_path)) if dest_path else ""
    key = hashlib.sha1(f"{game_id}|{kind}|{urls}|{identity}".encode("utf-8")).hexdigest()[:16]
    d = os.path.join(_cache_root(dest_path, game_id), key)
    os.makedirs(d, exist_ok=True)
    if dest_path:
        # Older builds stored set directories directly under the cache root.
        # Copy without replacing anything, verify every file, and keep the old
        # copy so a failed migration can always be retried safely.
        legacy = os.path.join(os.path.dirname(os.path.abspath(dest_path)),
                              ".gameinstaller-cache", key)
        if os.path.abspath(legacy) != os.path.abspath(d) and os.path.isdir(legacy):
            for root, dirs, files in os.walk(legacy):
                relative_root = os.path.relpath(root, legacy)
                target_root = d if relative_root == "." else os.path.join(d, relative_root)
                os.makedirs(target_root, exist_ok=True)
                for name in dirs:
                    os.makedirs(os.path.join(target_root, name), exist_ok=True)
                for name in files:
                    old_file = os.path.join(root, name)
                    new_file = os.path.join(target_root, name)
                    if os.path.exists(new_file):
                        if (os.path.getsize(old_file) != os.path.getsize(new_file)
                                or _sha256_file(old_file) != _sha256_file(new_file)):
                            raise OSError("Legacy and volume workspace files differ. Both were kept; review them before resuming.")
                        continue
                    shutil.copy2(old_file, new_file)
                    if (os.path.getsize(old_file) != os.path.getsize(new_file)
                            or _sha256_file(old_file) != _sha256_file(new_file)):
                        raise OSError("Download workspace migration verification failed; the original files were kept.")
    return d


def _download_identity(game):
    """Fingerprint sources without recording links or credentials in job records."""
    groups = (game.zip_parts, game.zip_backup_parts, game.update_parts,
              game.update_backup_parts, game.fix_parts, game.fix_backup_parts)
    data = json.dumps(groups, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def _resume_job_path(game_id, target):
    key = hashlib.sha256((game_id + '|' + os.path.normcase(os.path.abspath(target))).encode('utf-8')).hexdigest()
    return os.path.join(_prefs_dir(), 'download-jobs', key + '.json')


def _save_resume_job(game, target, fix_only=False, main_done=False):
    path = _resume_job_path(game.id, target)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.job-', dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                json.dump({'id': game.id, 'target': os.path.abspath(target),
                           'fix_only': bool(fix_only), 'main_done': bool(main_done),
                           'identity': _download_identity(game)}, fh)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError:
        _log.exception('Unable to save download resume record')


def _remove_resume_job(game, target):
    try:
        os.remove(_resume_job_path(game.id, target))
    except FileNotFoundError:
        pass
    except OSError:
        _log.exception('Unable to remove download resume record')


def _legacy_cache_exists(game, target):
    """Find cached downloads without creating or touching cache files."""
    root = os.path.join(os.path.dirname(os.path.abspath(target)), '.gameinstaller-cache')
    job_root = os.path.join(root, _cache_job_id(game.id, target))
    for kind, parts in (('main', game.zip_parts + game.zip_backup_parts),
                        ('game-update', game.update_parts + game.update_backup_parts),
                        ('fix', game.fix_parts + game.fix_backup_parts)):
        if not parts:
            continue
        urls = '|'.join((p.get('url') or '') for p in parts)
        identity = os.path.normcase(os.path.abspath(target))
        key = hashlib.sha1(f'{game.id}|{kind}|{urls}|{identity}'.encode('utf-8')).hexdigest()[:16]
        for folder in (os.path.join(job_root, key), os.path.join(root, key)):
            try:
                with os.scandir(folder) as entries:
                    if any(entry.is_file() and not entry.name.endswith('.json')
                           and entry.stat().st_size > 0 for entry in entries):
                        return True
            except OSError:
                pass
    return False


def sweep_cache(max_age_days: int = CACHE_MAX_AGE_DAYS):
    """Delete cache subfolders not touched in ``max_age_days`` days. Best-effort;
    called once at startup so abandoned partials don't accumulate forever."""
    root = _cache_root()
    cutoff = time.time() - max_age_days * 86400
    try:
        entries = os.listdir(root)
    except OSError:
        return
    for name in entries:
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Desktop shortcut creation — optional post-install step (off by default).
#
# The tricky part is picking the *main* game executable out of a folder that
# often also contains redistributables, crash handlers, engine helpers, etc.
# We score every .exe rather than naively taking the largest: a filename that
# resembles the game's title wins, then executables nearer the top of the
# install folder (launchers usually sit at the root), with file size only as a
# tiebreaker. Obvious non-game exes are pushed to the bottom.
# ---------------------------------------------------------------------------
_NON_GAME_EXE_RE = re.compile(
    r"unins|uninstall|setup|install|vc_?redist|vcredist|dxsetup|dxwebsetup|"
    r"directx|dotnet|ndp\d|netfx|redist|prereq|oalinst|openal|"
    r"crashhandler|crashreport|crashpad|unitycrashhandler|"
    r"unrealcefsubprocess|cefsubprocess|epicwebhelper|eossdk|"
    r"activation|touchup|notification|helper|python|node",
    re.IGNORECASE,
)


def _normalize_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def find_main_executable(root: str, game_name: str = ""):
    """Best guess at a game folder's main launchable .exe, or None if it has
    no executables. See the scoring rationale in the section comment above."""
    candidates = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.lower().endswith(".exe"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            rel = os.path.relpath(dirpath, root)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            candidates.append((full, fn, size, depth))
    if not candidates:
        return None

    game_key = _normalize_title(game_name)

    def score(entry):
        full, fn, size, depth = entry
        stem = _normalize_title(os.path.splitext(fn)[0])
        pts = 0.0
        if _NON_GAME_EXE_RE.search(fn):
            pts -= 1_000_000
        if game_key and stem and (game_key in stem or stem in game_key):
            pts += 1_000_000
        pts -= depth * 1000                          # shallower is better
        pts += min(size, 4_000_000_000) / 1_000_000  # size in MB, mild nudge
        return pts

    return max(candidates, key=score)[0]


def _ps_single_quote(value: str) -> str:
    """Quote a value as a PowerShell single-quoted string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def _desktop_dir() -> str:
    """The user's Desktop, honoring OneDrive/redirected folders via the
    registry, with a plain ~/Desktop fallback."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        )
        try:
            val, _ = winreg.QueryValueEx(key, "Desktop")
            val = os.path.expandvars(val)
            if os.path.isdir(val):
                return val
        finally:
            winreg.CloseKey(key)
    except OSError:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def create_desktop_shortcut(target_exe: str, shortcut_name: str):
    """Create a .lnk on the user's Desktop that launches target_exe. Uses
    PowerShell's WScript.Shell COM object so no extra dependency is needed.
    Returns the .lnk path on success, else None."""
    if not target_exe or not os.path.isfile(target_exe):
        return None
    safe = re.sub(r'[<>:"/\\|?*]', "", shortcut_name).strip() or "Game"
    lnk_path = os.path.join(_desktop_dir(), safe + ".lnk")
    workdir = os.path.dirname(target_exe)
    ps = (
        "$ErrorActionPreference='Stop';"
        f"$w=(New-Object -ComObject WScript.Shell).CreateShortcut({_ps_single_quote(lnk_path)});"
        f"$w.TargetPath={_ps_single_quote(target_exe)};"
        f"$w.WorkingDirectory={_ps_single_quote(workdir)};"
        f"$w.IconLocation={_ps_single_quote(target_exe + ',0')};"
        "$w.Save()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            creationflags=_NO_WINDOW, timeout=30,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return lnk_path if os.path.isfile(lnk_path) else None


def create_shortcuts_for_installed(base_dir, progress=None, cancel=None):
    """Walk ``base_dir``'s immediate subfolders — each one an installed game —
    and drop a Desktop shortcut for every folder that has a launchable .exe.

    This is the retroactive counterpart to the per-install shortcut option: it
    lets someone create shortcuts for games they installed before that option
    existed. The main .exe of each folder is picked with the same scoring used
    right after an install.

    Returns ``(created, skipped)`` where ``created`` is a list of game names
    that got a shortcut and ``skipped`` is a list of ``(name, reason)``.
    ``progress(name)`` — if given — is called before each folder is processed;
    ``cancel`` may be a threading.Event / zero-arg callable to stop early.
    """
    created, skipped = [], []

    def cancelled() -> bool:
        if cancel is None:
            return False
        return cancel.is_set() if hasattr(cancel, "is_set") else bool(cancel())

    try:
        entries = sorted(os.listdir(base_dir))
    except OSError:
        return created, skipped

    for name in entries:
        if cancelled():
            break
        folder = os.path.join(base_dir, name)
        if not os.path.isdir(folder):
            continue
        if progress is not None:
            progress(name)
        exe = find_main_executable(folder, name)
        if not exe:
            skipped.append((name, "no program found"))
            continue
        if create_desktop_shortcut(exe, name):
            created.append(name)
        else:
            skipped.append((name, "could not create shortcut"))
    return created, skipped


class ShortcutSweepWorker(QObject):
    """Runs create_shortcuts_for_installed off the UI thread so scanning large
    game folders doesn't freeze the window."""
    progress = pyqtSignal(str)          # folder currently being processed
    done = pyqtSignal(list, list)       # created names, skipped (name, reason)

    def __init__(self, base_dir):
        super().__init__()
        self.base_dir = base_dir
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    @pyqtSlot()
    def run(self):
        created, skipped = create_shortcuts_for_installed(
            self.base_dir, progress=self.progress.emit, cancel=self._cancel)
        self.done.emit(created, skipped)


# ---------------------------------------------------------------------------
# Spacewar helpers
# ---------------------------------------------------------------------------
def is_spacewar_installed() -> bool:
    """Return True if Steam has Spacewar (app 480) registered in the registry.

    Steam (typically 32-bit) writes under the WOW6432Node redirector.  We
    check both the 32-bit and 64-bit registry views so this works regardless
    of whether the calling process is 32- or 64-bit.
    """
    key_path = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        r"\Steam App 480"
    )
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for access in (
            winreg.KEY_READ | winreg.KEY_WOW64_32KEY,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ):
            try:
                key = winreg.OpenKey(hive, key_path, 0, access)
                winreg.CloseKey(key)
                return True
            except OSError:
                pass
    return False


def launch_spacewar_install() -> bool:
    """Open steam://install/480 via the Windows shell (ShellExecuteW).

    Returns False if the URI handler is not registered (Steam not installed).
    """
    try:
        os.startfile(f"steam://install/{SPACEWAR_APP_ID}")
        return True
    except OSError:
        return False


def launch_steam_client() -> bool:
    """Open the Steam client through its registered Windows protocol handler."""
    try:
        os.startfile("steam://open/main")
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Microsoft Defender exclusion helpers
#
# The Settings preference controls exclusions for new installs. Temporary
# exclusions are removed only when added by this installer run; existing paths
# are left alone. Add helpers return the paths they actually added.
# ---------------------------------------------------------------------------

def is_admin() -> bool:
    """Return True if the current process has Windows administrator privileges."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _ps_escape(s: str) -> str:
    """Escape a value for use inside a single-quoted PowerShell string."""
    return s.replace("'", "''")


def _run_elevated_ps(script: str, timeout_ms: int = 30_000) -> int:
    """Write *script* to a temp .ps1 file, execute it as administrator via
    ShellExecuteExW with the 'runas' verb, wait for completion, and return
    the process exit code.  Returns -1 if elevation was denied or failed.

    SEE_MASK_NOCLOSEPROCESS keeps the process handle open so we can call
    WaitForSingleObject and GetExitCodeProcess before closing it.
    """
    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0

    class _SEI(ctypes.Structure):
        _fields_ = [
            ("cbSize",         ctypes.c_uint32),
            ("fMask",          ctypes.c_uint32),
            ("hwnd",           ctypes.c_void_p),
            ("lpVerb",         ctypes.c_wchar_p),
            ("lpFile",         ctypes.c_wchar_p),
            ("lpParameters",   ctypes.c_wchar_p),
            ("lpDirectory",    ctypes.c_wchar_p),
            ("nShow",          ctypes.c_int),
            ("hInstApp",       ctypes.c_void_p),
            ("lpIDList",       ctypes.c_void_p),
            ("lpClass",        ctypes.c_wchar_p),
            ("hkeyClass",      ctypes.c_void_p),
            ("dwHotKey",       ctypes.c_uint32),
            ("hIconOrMonitor", ctypes.c_void_p),
            ("hProcess",       ctypes.c_void_p),
        ]

    fd, script_path = tempfile.mkstemp(suffix=".ps1", prefix="defex_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(script)

        args = (
            f'-NoProfile -NonInteractive -WindowStyle Hidden '
            f'-ExecutionPolicy Bypass -File "{script_path}"'
        )
        sei = _SEI()
        sei.cbSize = ctypes.sizeof(sei)
        sei.fMask = SEE_MASK_NOCLOSEPROCESS
        sei.lpVerb = "runas"
        sei.lpFile = "powershell.exe"
        sei.lpParameters = args
        sei.nShow = SW_HIDE

        if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
            return -1  # UAC denied or launch error

        if sei.hProcess:
            ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, timeout_ms)
            code = ctypes.c_uint32(0)
            ctypes.windll.kernel32.GetExitCodeProcess(
                sei.hProcess, ctypes.byref(code)
            )
            ctypes.windll.kernel32.CloseHandle(sei.hProcess)
            return code.value
        return 0
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass


def defender_exclusion_exists(folder: str) -> bool:
    """Return True if *folder* is already in Defender's ExclusionPath list.

    Reads Get-MpPreference without elevation (read-only access).  Returns
    False on any error rather than raising so the caller never crashes.
    """
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-MpPreference).ExclusionPath"],
            capture_output=True, text=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
        if result.returncode == 0:
            existing = {
                p.strip().lower()
                for p in result.stdout.splitlines()
                if p.strip()
            }
            return folder.strip().lower() in existing
    except Exception:
        pass
    return False


def _defender_access_denied(stderr: str) -> bool:
    """True when a PowerShell failure looks like 'needs admin' rather than a
    real error. Used to decide whether an elevated retry is worth a UAC prompt."""
    keywords = (
        "0x80070005", "Access", "privilege", "administrator",
        "Unauthorized", "UnauthorizedAccess",
    )
    return any(kw in stderr for kw in keywords)


def _run_mp_script(command: str, timeout: int = 15) -> tuple:
    """Run one Add-/Remove-MpPreference *command*, elevating only if needed.

    Returns (ok, message). *command* must already be fully built with escaped
    paths. Elevation happens at most once and only after a permission error.
    """
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        if result.returncode == 0:
            return (True, "")
        stderr = (result.stderr or result.stdout or "").strip()
        if not _defender_access_denied(stderr):
            return (False, stderr or "PowerShell returned a non-zero exit code.")
    except subprocess.TimeoutExpired:
        return (False, "PowerShell timed out.")
    except FileNotFoundError:
        return (False, "PowerShell was not found on this system.")

    # Elevated retry — the user sees exactly one UAC prompt.
    result_path = os.path.join(
        tempfile.gettempdir(), f"defex_result_{os.getpid()}.txt"
    )
    rp_esc = _ps_escape(result_path)
    script = (
        f"try {{\n"
        f"    {command}\n"
        f"    Set-Content -LiteralPath '{rp_esc}' -Value 'OK'\n"
        f"}} catch {{\n"
        f"    Set-Content -LiteralPath '{rp_esc}' -Value \"FAIL: $_\"\n"
        f"}}\n"
    )

    if _run_elevated_ps(script) == -1:
        return (False, "Administrator access was denied or the UAC prompt was cancelled.")

    try:
        with open(result_path, "r", encoding="utf-8") as fh:
            text = fh.read().strip()
        os.unlink(result_path)
    except FileNotFoundError:
        return (False, "The elevated process produced no result (it may have been blocked by policy).")
    except OSError as exc:
        return (False, f"Could not read the result file: {exc}")

    if text == "OK":
        return (True, "")
    prefix = "FAIL: "
    return (False, text[len(prefix):].strip() if text.startswith(prefix) else text)


def add_defender_exclusions(folders: list) -> tuple:
    """Add multiple folders to Defender exclusions in a single operation.

    Returns (success: bool, message: str, added: list[str]).

    ``added`` lists ONLY the paths this call created. Callers must use that
    list — never their input list — to decide what may later be removed: a
    path that was already excluded before we ran belongs to the user and must
    survive us.

    Folders that are already excluded are filtered out first, so one UAC prompt
    at most covers everything genuinely new.
    """
    to_add = [f for f in folders if not defender_exclusion_exists(f)]
    if not to_add:
        return (True, "Already excluded — nothing was added.", [])

    # PowerShell accepts a comma-separated array: -ExclusionPath 'a','b'
    paths_ps = ",".join(f"'{_ps_escape(f)}'" for f in to_add)
    ok, detail = _run_mp_script(f"Add-MpPreference -ExclusionPath {paths_ps}")

    if ok:
        _log.debug("add_defender_exclusions  added=%r", to_add)
        return (True, "Defender exclusions added.", to_add)
    _log.debug("add_defender_exclusions  FAIL  %r  detail=%r", to_add, detail)
    return (False, detail or "Unknown error while adding exclusions.", [])


def add_defender_exclusion(folder: str) -> tuple:
    """Add a single folder to Defender exclusions.

    Thin wrapper over add_defender_exclusions() kept for single-path callers.
    Returns (success: bool, message: str, added: list[str]).
    """
    return add_defender_exclusions([folder])


def remove_defender_exclusion(folder: str) -> tuple:
    """Remove *folder* from Defender's ExclusionPath.

    Call this only for paths the installer itself added — see
    add_defender_exclusions(). Failure is non-fatal; the exclusion simply
    stays in place.

    Returns (success: bool, message: str).
    """
    if not defender_exclusion_exists(folder):
        return (True, "Folder was not in the exclusion list.")

    ok, detail = _run_mp_script(f"Remove-MpPreference -ExclusionPath '{_ps_escape(folder)}'")
    if ok:
        _log.debug("remove_defender_exclusion  OK  folder=%r", folder)
        return (True, "Defender exclusion removed.")
    _log.debug("remove_defender_exclusion  FAIL  folder=%r  detail=%r", folder, detail)
    return (False, detail or "Unknown error while removing the exclusion.")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
def _normalize_parts(raw):
    """Normalize a zipUrl / fixZipUrl manifest value into an ordered list of
    ``{"url": str, "name": str}`` dicts.

    Accepts, simplest first:
      * ``"https://…/file.rar"``                       → one volume
      * ``"https://…/p1, https://…/p2, https://…/p3"`` → multi-volume, just the
        links in order on one line, separated by commas (or newlines)
      * ``["https://…/p1", "https://…/p2"]``           → same, as a JSON array
      * ``[{"url": "…", "name": "Game.part1.rar"}, …]`` → explicit local
        filenames (only needed if you want to override the auto-naming)

    For the plain-link forms the **order you list the links in is the order
    they are joined** (link 1 → part 1, link 2 → part 2, …), so just paste them
    in sequence. When manifest entries carry a ``…partN.rar`` name they are
    additionally sorted by that number, so explicit-name lists may be in any
    order.
    """
    # A single string holding several comma/newline-separated links expands
    # into a multi-volume list — "zipUrl": "url1, url2, url3" just works.
    if isinstance(raw, str) and ("," in raw or "\n" in raw):
        raw = re.split(r"[,\n]", raw)

    items = raw if isinstance(raw, list) else [raw]
    parts = []
    for item in items:
        if isinstance(item, dict):
            url = str(item.get("url", "")).strip()
            name = str(item.get("name", "")).strip()
        else:
            url = str(item).strip()
            name = ""
        if url:
            parts.append({"url": url, "name": name})

    def _part_key(entry):
        m = re.search(r"\.part0*(\d+)\.", entry["name"], re.IGNORECASE)
        return (0, int(m.group(1))) if m else (1, 0)

    parts.sort(key=_part_key)
    return parts


class Game:
    """A single game entry parsed from the manifest."""

    def __init__(self, raw: dict):
        self._raw = dict(raw)
        candidate = raw.get("morePlayers")
        self.more_players = candidate if isinstance(candidate, dict) and _normalize_parts(candidate.get("zipUrl", "")) else None
        self.id = str(raw.get("id", "")).strip()
        self.name = str(raw.get("name", "Untitled")).strip()
        self.description = str(raw.get("description", "")).strip()
        self.version = str(raw.get("version", "")).strip()
        self.size = str(raw.get("size", "")).strip()
        self.players = str(raw.get("players", "")).strip()
        self.thumbnail = str(raw.get("thumbnail", "")).strip()
        executable = raw.get("launchExecutable", "")
        self.launch_executable = executable if isinstance(executable, str) and re.fullmatch(r"[^\\/:*?\"<>|]+\.exe", executable, re.IGNORECASE) else ""
        color = raw.get("cardColor", "")
        self.card_color = color if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color) else ""
        # Primary and backup link lists are independently normalized. Backup
        # entries correspond by index to the primary volumes; they are never
        # concatenated into the primary list because that means multipart data.
        self.zip_parts = _normalize_parts(raw.get("zipUrl", ""))
        self.update_parts = _normalize_parts(raw.get("updateZipUrl", ""))
        self.update_backup_parts = _normalize_parts(raw.get("updateZipBackupUrl", ""))
        self.fix_parts = _normalize_parts(raw.get("fixZipUrl", ""))
        self.zip_backup_parts = _normalize_parts(raw.get("zipBackupUrl", ""))
        self.fix_backup_parts = _normalize_parts(raw.get("fixBackupUrl", ""))
        if not self.zip_parts and self.zip_backup_parts:
            self.zip_parts, self.zip_backup_parts = self.zip_backup_parts, []
        if not self.fix_parts and self.fix_backup_parts:
            self.fix_parts, self.fix_backup_parts = self.fix_backup_parts, []
        self.zip_url = self.zip_parts[0]["url"] if self.zip_parts else ""
        self.fix_url = self.fix_parts[0]["url"] if self.fix_parts else ""
        self.entry_type = str(raw.get("type", "game")).strip().lower()
        _reqs = raw.get("requirements")
        self.requirements = _reqs if isinstance(_reqs, dict) else {}
        # macCompatibility is injected by the publish-time batch classifier
        # (compat/batchClassify.py). The app only reads — never computes — this.
        _compat = raw.get("macCompatibility")
        self.mac_compat: dict | None = _compat if isinstance(_compat, dict) else None

    def more_players_game(self):
        """Create an isolated download identity without inheriting normal repairs."""
        if not self.more_players:
            raise ValueError("No more-players edition is configured.")
        raw = dict(self._raw)
        raw.update(id=self.id + "--more-players", name=self.name + " (More Players)",
                   zipUrl=self.more_players["zipUrl"], zipBackupUrl="", fixZipUrl="", fixBackupUrl="",
                   version=self.more_players.get("version", ""),
                   description=self.more_players.get("description", ""),
                   macCompatibility={"tier": "Unverified", "confidence": "Low",
                                     "reasoning": "The more-players edition has not been tested on Mac."})
        raw.pop("morePlayers", None)
        return Game(raw)


    @property
    def is_available(self) -> bool:
        return bool(self.zip_url)

    @property
    def has_fix(self) -> bool:
        return bool(self.fix_url)

    @property
    def is_patch(self) -> bool:
        return self.entry_type == "fix"

    @property
    def is_multipart(self) -> bool:
        return len(self.zip_parts) > 1

    @property
    def safe_folder_name(self) -> str:
        """A filesystem-safe folder name derived from the game's name."""
        invalid = '<>:"/\\|?*'
        # Windows strips trailing dots and spaces when creating directories;
        # retaining them breaks later child-file lookups in the same folder.
        cleaned = "".join(c for c in self.name if c not in invalid).strip().rstrip(" .")
        return cleaned or (self.id or "Game")


def game_launch_executable(root, game):
    """Honor an explicit catalog executable without silently launching a different one."""
    if not game.launch_executable:
        return find_main_executable(root, game.name)
    matches = []
    for directory, _dirs, files in os.walk(root):
        for name in files:
            if name.casefold() == game.launch_executable.casefold():
                matches.append(os.path.join(directory, name))
    return matches[0] if len(matches) == 1 else None


def parse_manifest(text: str) -> list:
    """Parse manifest JSON text into a list of Game objects."""
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("Manifest must be a JSON array of games.")
    return [Game(item) for item in data if isinstance(item, dict)]


def validate_manifest(text: str) -> list:
    games = parse_manifest(text)
    if not games:
        raise ValueError("The catalog has no game entries.")
    ids = [game.id for game in games]
    if any(not game_id for game_id in ids):
        raise ValueError("Every catalog entry must have an ID.")
    if len(set(ids)) != len(ids):
        raise ValueError("The catalog contains duplicate game IDs.")
    return games


# ---------------------------------------------------------------------------
# System spec detection + "will it run?" requirement matching.
#
# RAM, OS (version + bitness), free storage and DirectX are checked for real
# and drive the verdict. CPU and GPU are shown for comparison only — reliably
# deciding whether a CPU/GPU *model name* meets a requirement needs a benchmark
# database, so we detect and display them and let the user judge rather than
# lie with a fake pass/fail.
# ---------------------------------------------------------------------------
_SYSTEM_SPECS = None  # cached dict, computed once

_WIN_NAME_TO_VER = {
    "xp": 5.1, "vista": 6.0, "7": 7.0, "8": 8.0, "8.1": 8.1, "10": 10.0, "11": 11.0,
}


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _detect_ram_gb():
    try:
        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return stat.ullTotalPhys / (1024 ** 3)
    except Exception:
        pass
    return None


def _detect_cpu_name():
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as key:
            return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    except OSError:
        return ""


def _detect_gpu_name():
    """Primary display adapter's DriverDesc. Best-effort, registry-only (no
    subprocess, to avoid both latency and AV heuristics)."""
    try:
        base = (r"SYSTEM\CurrentControlSet\Control\Class"
                r"\{4d36e968-e325-11ce-bfc1-08002be10318}")
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as k:
                        desc = winreg.QueryValueEx(k, "DriverDesc")[0].strip()
                    if desc:
                        return desc
                except OSError:
                    continue
    except OSError:
        pass
    return ""


def _detect_os():
    """Return (display_name, numeric_version, is_64bit)."""
    is64 = (os.environ.get("PROCESSOR_ARCHITECTURE", "").endswith("64") or
            os.environ.get("PROCESSOR_ARCHITEW6432", "").endswith("64"))
    try:
        v = sys.getwindowsversion()
    except Exception:
        return ("Unknown OS", None, is64)
    major, minor, build = v.major, v.minor, v.build
    if major == 10 and build >= 22000:
        return ("Windows 11", 11.0, is64)
    if major == 10:
        return ("Windows 10", 10.0, is64)
    mapping = {
        (6, 3): ("Windows 8.1", 8.1), (6, 2): ("Windows 8", 8.0),
        (6, 1): ("Windows 7", 7.0), (6, 0): ("Windows Vista", 6.0),
        (5, 1): ("Windows XP", 5.1),
    }
    if (major, minor) in mapping:
        name, num = mapping[(major, minor)]
        return (name, num, is64)
    return (f"Windows {major}.{minor}", float(major), is64)


def detect_system_specs():
    """Detect and cache this PC's specs. Safe to call repeatedly."""
    global _SYSTEM_SPECS
    if _SYSTEM_SPECS is None:
        os_name, os_ver, is64 = _detect_os()
        _SYSTEM_SPECS = {
            "os_name": os_name, "os_version": os_ver, "is_64bit": is64,
            "ram_gb": _detect_ram_gb(),
            "cpu_name": _detect_cpu_name(), "gpu_name": _detect_gpu_name(),
        }
        _log.debug("detect_system_specs  %r", _SYSTEM_SPECS)
    return _SYSTEM_SPECS


def _parse_gb(text):
    """Pull a GB figure out of a requirement string like '8 GB' / '512 MB'."""
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(TB|GB|MB)", str(text), re.IGNORECASE)
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2).upper()
    return val * 1024 if unit == "TB" else val / 1024 if unit == "MB" else val


def _min_windows_version(os_req):
    """Lowest Windows version named in the requirement (it's a floor)."""
    if not os_req:
        return None
    found = [_WIN_NAME_TO_VER.get(t) for t in
             re.findall(r"8\.1|11|10|\b8\b|\b7\b|vista|xp", os_req.lower())]
    found = [f for f in found if f]
    return min(found) if found else None


def _free_space_gb(path):
    """Free space (GB) on the drive of *path*, walking up to the nearest
    existing directory since the install folder may not exist yet."""
    target = path or os.path.expanduser("~")
    while target and not os.path.isdir(target):
        parent = os.path.dirname(target)
        if parent == target:
            break
        target = parent
    try:
        return shutil.disk_usage(target).free / (1024 ** 3)
    except OSError:
        return None


def evaluate_requirements(reqs, install_path=None):
    """Compare a game's requirements dict against this PC.

    Returns {'verdict': pass|warn|fail|unknown, 'summary': str,
             'checks': [{'label','required','yours','status','note'}, ...]}.
    """
    if not reqs:
        return {"verdict": "unknown",
                "summary": "No system requirements are listed for this game.",
                "checks": []}

    specs = detect_system_specs()
    checks = []

    def add(label, required, yours, status, note=""):
        checks.append({"label": label, "required": str(required or "—"),
                       "yours": str(yours or "—"), "status": status, "note": note})

    # OS — version floor + bitness
    os_req = reqs.get("os", "")
    if os_req:
        bitness = " 64-bit" if specs["is_64bit"] else " 32-bit"
        status, note = "pass", ""
        floor = _min_windows_version(os_req)
        if "64" in os_req and specs["is_64bit"] is False:
            status, note = "fail", "This game needs 64-bit Windows."
        elif floor and specs["os_version"] and specs["os_version"] < floor:
            status, note = "fail", "Your Windows version is older than required."
        add("OS", os_req, specs["os_name"] + bitness, status, note)

    # RAM
    ram_req = _parse_gb(reqs.get("ram"))
    if ram_req:
        yours = specs["ram_gb"]
        if yours is None:
            add("RAM", reqs.get("ram"), None, "unknown")
        else:
            ok = yours + 0.5 >= ram_req  # tolerate reserved/rounded RAM
            add("RAM", reqs.get("ram"), f"{yours:.1f} GB",
                "pass" if ok else "fail",
                "" if ok else "You have less RAM than required.")

    # Storage — free space on the install drive
    store_req = _parse_gb(reqs.get("storage"))
    if store_req:
        free = _free_space_gb(install_path)
        if free is None:
            add("Storage", reqs.get("storage"), None, "unknown")
        else:
            ok = free >= store_req
            add("Storage", reqs.get("storage"), f"{free:.1f} GB free",
                "pass" if ok else "warn",
                "" if ok else "Not enough free space — free some up first.")

    # DirectX — Win10/11 ship DX12, which covers DX9–12 apps
    dx_req = reqs.get("directx", "")
    if dx_req:
        m = re.search(r"(\d+)", dx_req)
        dxnum = int(m.group(1)) if m else None
        if dxnum and specs["os_version"] and specs["os_version"] >= 10 and dxnum <= 12:
            add("DirectX", dx_req, "Included with Windows 10/11", "pass")
        else:
            add("DirectX", dx_req, "", "info")

    # CPU / GPU — informational (see module note above)
    if reqs.get("cpu"):
        add("CPU", reqs.get("cpu"), specs["cpu_name"], "info")
    if reqs.get("gpu"):
        add("GPU", reqs.get("gpu"), specs["gpu_name"], "info")

    statuses = [c["status"] for c in checks]
    if "fail" in statuses:
        verdict, summary = "fail", "This game may not run well on your PC."
    elif "warn" in statuses:
        verdict, summary = "warn", "This game should run — see the note below."
    else:
        verdict, summary = "pass", "This game should run on your PC."
    return {"verdict": verdict, "summary": summary, "checks": checks}


# ---------------------------------------------------------------------------
# FlowLayout — wraps child widgets to the available width (responsive grid).
# Adapted from the classic Qt FlowLayout example.
# ---------------------------------------------------------------------------
class FlowLayout(QLayout):
    def __init__(self, parent=None, margin=0, spacing=16):
        super().__init__(parent)
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)
        self._items = []

    def __del__(self):
        while self._items:
            self._items.pop()

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y = effective.x(), effective.y()
        line_height = 0
        spacing = self.spacing()

        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + spacing
            if next_x - spacing > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + spacing
                next_x = x + hint.width() + spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + m.bottom()


# ---------------------------------------------------------------------------
# Thumbnail loading — QThreadPool so the grid never blocks on image HTTP.
# An in-memory cache (touched only from the GUI thread) avoids refetching.
# ---------------------------------------------------------------------------
THUMB_CACHE = {}  # url -> QImage


class ThumbnailSignals(QObject):
    done = pyqtSignal(str, object)  # url, QImage or None


class ThumbnailRunnable(QRunnable):
    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self.signals = ThumbnailSignals()

    @pyqtSlot()
    def run(self):
        image = None
        try:
            resp = requests.get(self.url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            img = QImage()
            if img.loadFromData(resp.content):
                image = img
        except Exception:
            image = None  # thumbnail failure must never crash the app
        self.signals.done.emit(self.url, image)


# ---------------------------------------------------------------------------
# Manifest loader worker (runs on its own QThread).
# ---------------------------------------------------------------------------
class ManifestLoader(QObject):
    loaded = pyqtSignal(list, str)   # games, source label
    failed = pyqtSignal(str)
    unchanged = pyqtSignal()

    def __init__(self, manifest_url: str, mode: str = "url"):
        super().__init__()
        self.manifest_url = manifest_url
        self.mode = mode

    @pyqtSlot()
    def run(self):
        local_path = os.path.join(app_dir(), LOCAL_MANIFEST)
        bundled_path = os.path.join(getattr(sys, "_MEIPASS", app_dir()), LOCAL_MANIFEST)

        # Imported files and pasted JSON share a managed copy in AppData, so
        # the app does not depend on a removable file or security-scoped path.
        if self.mode in {"local", "paste"}:
            try:
                with open(user_catalog_path(), "r", encoding="utf-8") as fh:
                    games = validate_manifest(fh.read())
                self.loaded.emit(games, "local games.json" if self.mode == "local" else "pasted games.json")
                return
            except Exception as exc:
                self.failed.emit(f"Could not load the saved local games.json:\n{exc}")
                return

        # 1) Try the configured remote URL when this load is allowed to update.
        if self.mode == "url" and self.manifest_url:
            try:
                prefs = load_prefs()
                etag = prefs.get("catalog_etag", "")
                req = urllib.request.Request(
                    self.manifest_url,
                    headers={"User-Agent": "GameInstaller/1.0", "Cache-Control": "no-cache"},
                )
                if etag and prefs.get("catalog_etag_url") == self.manifest_url:
                    req.add_header("If-None-Match", etag)
                try:
                    response = urllib.request.urlopen(req, timeout=15)
                except urllib.error.HTTPError as exc:
                    if exc.code == 304:
                        self.unchanged.emit()
                        return
                    raise
                with response as resp:
                    raw = resp.read().decode("utf-8")
                    new_etag = getattr(resp, "headers", {}).get("ETag", "")
                games = validate_manifest(raw)
                # Persist to disk so the next offline launch uses the latest list.
                try:
                    with open(local_path, "w", encoding="utf-8") as fh:
                        fh.write(raw)
                    os.makedirs(os.path.dirname(user_catalog_path()), exist_ok=True)
                    with open(user_catalog_path(), "w", encoding="utf-8") as fh:
                        fh.write(raw)
                    if new_etag:
                        prefs["catalog_etag"] = new_etag
                        prefs["catalog_etag_url"] = self.manifest_url
                        save_prefs(prefs)
                except OSError:
                    pass  # write failure is non-fatal; we already have the data
                self.loaded.emit(games, "remote manifest")
                return
            except Exception:
                pass  # network, server rate limit or parse error — use local copy

        # 2) Fall back through the user's saved catalog, app-local copy, and
        # bundled copy. Restore the app-local copy from the bundle when absent.
        if not os.path.isfile(local_path) and os.path.isfile(bundled_path):
            try:
                shutil.copyfile(bundled_path, local_path)
            except OSError:
                pass

        # Imported catalogs take precedence over cached and bundled defaults.
        candidates = []
        for path in (user_catalog_path(), local_path, bundled_path):
            if os.path.isfile(path) and path not in candidates:
                candidates.append(path)

        last_error = None
        for path in candidates:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    games = validate_manifest(fh.read())
                label = "local games.json (online source unavailable)" if self.manifest_url else "local games.json"
                self.loaded.emit(games, label)
                return
            except Exception as exc:
                last_error = exc

        if last_error is not None:
            self.failed.emit(f"Failed to load games:\n{last_error}")
        else:
            self.failed.emit(
                "Could not reach the game list server and no valid local games.json was found.\n\n"
                "Check your internet connection or restore the bundled game list."
            )


# ---------------------------------------------------------------------------
# Defender exclusion worker — runs add_defender_exclusions off the GUI thread
# so UAC + PowerShell never freeze the window.
# ---------------------------------------------------------------------------
class DefenderWorker(QObject):
    finished = pyqtSignal(bool, str, list)  # success, message, paths actually added

    def __init__(self, folders):
        super().__init__()
        # Accept a single path string or a list of paths.
        self.folders = [folders] if isinstance(folders, str) else list(folders)

    @pyqtSlot()
    def run(self):
        success, message, added = add_defender_exclusions(self.folders)
        self.finished.emit(success, message, added)


# ---------------------------------------------------------------------------
# Self-update workers
# ---------------------------------------------------------------------------
class UpdateChecker(QObject):
    """Fetches the latest GitHub release and signals if a newer version exists."""
    result = pyqtSignal(object, str)  # UpdateRelease or None, error text
    finished = pyqtSignal()

    @pyqtSlot()
    def run(self):
        # Step 1 — app starts: log the current VERSION value.
        _ulog.info("[1] App started. Current VERSION = %r", VERSION)
        _log.debug("UpdateChecker: runtime VERSION = %r", VERSION)
        try:
            # Step 2 — public release manifest request.
            _ulog.info("[2] Release manifest request  URL = %s", RELEASES_API)
            resp = requests.get(
                RELEASES_API,
                timeout=(5, 10),
                headers={
                    "Accept": "application/json",
                    "User-Agent": "GameInstaller-updater",
                },
            )
            # Step 3 — response received: log raw tag_name and HTTP status.
            _log.debug("UpdateChecker: HTTP status = %s", resp.status_code)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError("GitHub returned an invalid releases list")
            data = next((release for release in data
                         if isinstance(release, dict)
                         and any(asset.get("name") in (
                             "GameInstaller-windows-x64.exe", "GameInstaller.exe")
                             for asset in (release.get("assets") or [])
                             if isinstance(asset, dict))), None)
            tag = data.get("tag_name", "") if data else ""
            _ulog.info(
                "[3] Release manifest response  status=%s  tag_name=%r",
                resp.status_code, tag,
            )
            _log.debug("UpdateChecker: tag_name from API = %r", tag)

            # GitHub Releases does not expose the custom commit-history fields
            # used by the former GitLab manifest. Compare release tags directly
            # so users see every commit since the version installed on Windows.
            if data and tag:
                history = release_change_history(VERSION, tag)
                if history:
                    data = {**data, **history}

            update = (compatible_release(data, VERSION, platform.system(), platform.machine(),
                                         packaged=bool(getattr(sys, "frozen", False)))
                      if data else None)
            self.result.emit(update, "")
        except Exception as exc:
            # Step 9 — exception anywhere in the update flow: log full traceback.
            _ulog.exception("[9] Exception in UpdateChecker.run — full traceback:")
            _log.exception("UpdateChecker: update check failed with an exception")
            self.result.emit(None, str(exc))
            # still fail silently to the UI — update check must never affect
            # normal operation — but now it's logged instead of invisible.
        finally:
            self.finished.emit()


class UpdateDownloadWorker(QObject):
    """Downloads the new release exe to a temp file."""
    progress = pyqtSignal(int)   # 0-100, or -1 for indeterminate
    success = pyqtSignal(str)    # path to downloaded temp file
    error = pyqtSignal(str) #test again

    def __init__(self, url: str, expected_sha256: str):
        super().__init__()
        self.url = url
        self.expected_sha256 = expected_sha256

    @pyqtSlot()
    def run(self):
        # Step 6 — download starts: log the download URL.
        _ulog.info("[6] Download started  url=%s", self.url)
        dest = None
        try:
            fd, dest = tempfile.mkstemp(suffix=".exe", prefix="GameInstaller_update_")
            os.close(fd)
            download_verified_file(self.url, dest, self.expected_sha256,
                                   progress=self.progress.emit)
            self.progress.emit(100)
            self.success.emit(dest)
        except Exception as exc:
            if dest and os.path.exists(dest):
                try:
                    os.remove(dest)
                except OSError:
                    pass
            # Step 7 — download failed: log the exception/error.
            _ulog.exception("[7] Download FAILED  url=%s  error=%s", self.url, exc)
            self.error.emit(str(exc))


# ---------------------------------------------------------------------------
# Google Drive large-file confirmation helper
# ---------------------------------------------------------------------------
_GDRIVE_UC_RE = re.compile(r"https?://drive\.google\.com/uc\b")

def _gdrive_confirm(session: "requests.Session", url: str) -> str:
    """If *url* is a Google Drive /uc download link, follow the virus-scan
    warning page and return the confirmed download URL with session cookies
    preserved.  Returns *url* unchanged for non-Drive URLs.

    Two strategies are tried in order:
      1. Modern Drive – extract the ``uuid`` hidden form field and redirect to
         ``drive.usercontent.google.com/download``.
      2. Legacy Drive – re-use the ``confirm=<token>`` found in the page, or
         append ``confirm=t`` as a last resort.

    The probe request uses ``stream=True`` so no file bytes are consumed when
    the server returns a direct download (no warning page).
    """
    if not _GDRIVE_UC_RE.match(url):
        return url
    try:
        probe = session.get(url, stream=True, timeout=30, allow_redirects=True)
        probe.raise_for_status()
        ct = probe.headers.get("Content-Type", "")
        if "text/html" not in ct:
            probe.close()
            _log.debug("_gdrive_confirm  direct file  ct=%r  url=%s", ct, url)
            return url
        html = probe.text   # warning page is small; read it fully
        probe.close()
    except requests.exceptions.RequestException:
        return url  # let _download surface the error

    _log.debug("_gdrive_confirm  warning page detected  url=%s", url)

    # Modern Drive: <input name="uuid" value="..."> in the confirmation form
    uuid_m = re.search(
        r'<input[^>]+name=["\']uuid["\'][^>]+value=["\']([^"\']+)["\']'
        r'|<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']uuid["\']',
        html, re.IGNORECASE,
    )
    if uuid_m:
        uuid_val = uuid_m.group(1) or uuid_m.group(2)
        from urllib.parse import parse_qs, urlparse
        file_id = parse_qs(urlparse(url).query).get("id", [""])[0]
        confirmed = (
            f"https://drive.usercontent.google.com/download"
            f"?id={file_id}&export=download&confirm=t&uuid={uuid_val}"
        )
        _log.debug("_gdrive_confirm  uuid=%s  -> %s", uuid_val, confirmed)
        return confirmed

    # Legacy Drive: a specific confirm token (not the generic "t") in the HTML
    token_m = re.search(r"confirm=([0-9A-Za-z_\-]+)", html)
    if token_m and token_m.group(1) != "t":
        sep = "&" if "?" in url else "?"
        confirmed = f"{url}{sep}confirm={token_m.group(1)}"
        _log.debug("_gdrive_confirm  legacy token=%s  -> %s",
                   token_m.group(1), confirmed)
        return confirmed

    # Last resort: append confirm=t if not already present
    if "confirm=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}confirm=t"
    _log.debug("_gdrive_confirm  fallback  -> %s", url)
    return url


# ---------------------------------------------------------------------------
# Google Drive download (replaces _gdrive_confirm in the download path).
# _gdrive_confirm above is kept intentionally for rollback but is no longer
# called anywhere.
# ---------------------------------------------------------------------------
def _is_gdrive_url(url: str) -> bool:
    return "drive.google.com" in url or "drive.usercontent.google.com" in url


def _gdrive_file_id(url: str):
    """Extract a Drive file id from any common link shape, or None.
    Handles /file/d/<id>/, ?id=<id>, /d/<id>, and usercontent ?id=<id>."""
    for pat in (r"/file/d/([^/?#]+)", r"[?&]id=([^&#]+)", r"/d/([^/?#]+)"):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


_HTML_SNIFF = re.compile(rb"^\s*(<!doctype html|<html|<head|<body|<!--)", re.IGNORECASE)


def _looks_like_html(head: bytes) -> bool:
    return bool(_HTML_SNIFF.match(head))


def _parse_gdrive_form(html: str):
    """Pull the confirmation <form> action URL and ALL its hidden inputs.
    Robust to attribute ordering (name-before-value or value-before-name)."""
    form = re.search(r'<form[^>]+action="([^"]+)"[^>]*>(.*?)</form>',
                     html, re.IGNORECASE | re.DOTALL)
    if not form:
        return None, {}
    action = form.group(1).replace("&amp;", "&")
    fields = {}
    for tag in re.finditer(r'<input\b[^>]*>', form.group(2), re.IGNORECASE):
        t = tag.group(0)
        nm = re.search(r'name="([^"]*)"', t, re.IGNORECASE)
        vm = re.search(r'value="([^"]*)"', t, re.IGNORECASE)
        if nm:
            fields[nm.group(1)] = vm.group(1) if vm else ""
    return action, fields


def _validate_not_html(path: str) -> bool:
    """Final guard: reject (and delete) the file if we saved the warning page."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
    except OSError:
        return False
    if not head or _looks_like_html(head):
        try:
            os.remove(path)
        except OSError:
            pass
        return False
    return True


def download_gdrive(file_id: str, dest_path: str, progress_callback=None, *,
                    api_key: str = None, session: "requests.Session" = None,
                    cancel=None, resume_offset: int = 0) -> bool:
    """Download a *public* Google Drive file by id, handling the large-file
    virus-scan interstitial automatically.

    Returns True on success, False on any failure — network error, per-file
    download quota, or the server handing us the HTML warning page instead of
    the real file (in which case no bad file is left on disk).

    progress_callback(bytes_downloaded, total_bytes): total_bytes is 0 when the
    server doesn't report a Content-Length.
    Optional: api_key enables the Drive-API fallback; cancel may be a
    threading.Event or a zero-arg callable returning True to abort.

    resume_offset > 0 asks the server (via a Range header) to continue an
    earlier partial download that already wrote that many bytes to dest_path;
    the bytes are appended. If the server ignores the range (responds 200
    instead of 206) the file is truncated and re-fetched from the start, which
    is always safe. The caller is responsible for validating/renaming dest_path
    afterwards.
    """
    api_key = api_key or None  # treat "" (unset key) as no API fallback
    owns_session = session is None
    if owns_session:
        session = requests.Session()
        session.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )

    def cancelled() -> bool:
        if cancel is None:
            return False
        return cancel.is_set() if hasattr(cancel, "is_set") else bool(cancel())

    def stream_to_disk(resp) -> bool:
        # A 206 means the server honoured our Range header: append to the
        # existing partial. Anything else (200) is a full body, so start over.
        resumed = resume_offset > 0 and resp.status_code == 206
        if resumed:
            cr = resp.headers.get("Content-Range", "")
            m = re.search(r"/\s*(\d+)\s*$", cr)
            total = int(m.group(1)) if m else 0
            done = resume_offset
            mode = "ab"
        else:
            total = resp.headers.get("Content-Length")
            total = int(total) if total and total.isdigit() else 0
            done = 0
            mode = "wb"
        first = True
        with open(dest_path, mode) as out:
            for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK):
                if cancelled():
                    return False
                if not chunk:
                    continue
                if first:
                    first = False
                    # Only the fresh (non-resumed) first chunk can be the HTML
                    # warning page; a resumed stream continues already-validated
                    # binary content mid-file.
                    if not resumed and _looks_like_html(chunk[:64]):
                        return False          # headers lied; it's the warning page
                out.write(chunk)
                done += len(chunk)
                if progress_callback is not None:
                    progress_callback(done, total)
        return done > (resume_offset if resumed else 0)

    def fetch(url, params=None):
        """-> ('file', ok) after streaming, ('html', text), or ('error', None)."""
        headers = {"Range": f"bytes={resume_offset}-"} if resume_offset > 0 else None
        try:
            with session.get(url, params=params, headers=headers, stream=True,
                             timeout=REQUEST_TIMEOUT, allow_redirects=True) as resp:
                # 416 = we already hold the whole file; treat as a finished download.
                if resp.status_code == 416:
                    return "file", True
                resp.raise_for_status()
                if "text/html" in resp.headers.get("Content-Type", "").lower():
                    return "html", resp.text
                return "file", stream_to_disk(resp)
        except requests.exceptions.RequestException:
            return "error", None

    def is_quota(html: str) -> bool:
        h = html.lower()
        return "too many users" in h or "exceeded" in h or "quota" in h

    # 1) usercontent with confirm=t — fast path; small files + some large ones
    kind, payload = fetch("https://drive.usercontent.google.com/download",
                          {"id": file_id, "export": "download", "confirm": "t"})
    if kind == "file" and payload:
        return True
    html = payload if kind == "html" else ""

    # 2) scrape the interstitial form and resubmit (carries the required uuid/at)
    if is_quota(html):
        return False                          # per-file quota: no method fixes this
    action, fields = _parse_gdrive_form(html)
    if not action:
        kind, payload = fetch("https://drive.google.com/uc",
                              {"export": "download", "id": file_id})
        if kind == "file" and payload:
            return True
        html = payload if kind == "html" else ""
        if is_quota(html):
            return False
        action, fields = _parse_gdrive_form(html)
    if action:
        kind, payload = fetch(action, fields)
        if kind == "file" and payload:
            return True

    # 3) Drive API alt=media — most stable transport, needs api_key + public file
    if api_key:
        kind, payload = fetch(f"https://www.googleapis.com/drive/v3/files/{file_id}",
                              {"alt": "media", "key": api_key})
        if kind == "file" and payload:
            return True

    return False


# ---------------------------------------------------------------------------
# Install worker (download + extract + optional fix merge) on its own QThread.
# ---------------------------------------------------------------------------
class InstallWorker(QObject):
    download_progress = pyqtSignal(int)       # main download   0-100 / -1
    extract_progress = pyqtSignal(int)        # main extraction 0-100
    fix_download_progress = pyqtSignal(int)   # fix download    0-100 / -1
    fix_apply_progress = pyqtSignal(int)      # fix merge       0-100
    status = pyqtSignal(str)                  # full status with bytes/speed/ETA
    speed_text = pyqtSignal(str)              # "8.4 MB/s"
    eta_text = pyqtSignal(str)                # "1m 34s remaining"
    elapsed_text = pyqtSignal(str)            # "2m 15s elapsed"
    overall_progress = pyqtSignal(int)        # 0-100 for entire install
    success = pyqtSignal(str)                 # final install path
    error = pyqtSignal(str)
    canceled = pyqtSignal()
    main_installed = pyqtSignal()             # main game fully installed to disk

    def __init__(self, game: Game, dest_path: str, fix_only: bool = False,
                 remove_temp_exclusion: bool = False):
        super().__init__()
        self.game = game
        self.dest_path = dest_path
        self._fix_only = fix_only
        # True only when THIS install is the one that added the %TEMP%
        # Defender exclusion, so it is also the one allowed to remove it again.
        self._remove_temp_exclusion = remove_temp_exclusion
        self._cancel = threading.Event()
        self._temp_files = []
        self._temp_dirs = []
        self._last_part_paths = []  # volumes from the most recent _download_parts
        self._cache_sets = []       # persistent cache dirs used by this install
        self._part_sizes = []
        self._part_cached = []
        self._part_index = 0
        self._size_probe_stop = threading.Event()
        # Progress tracking
        self._start_time = None  # time.monotonic()
        self._stage_start = None  # per-stage start time
        # Overall progress weights: (DL, EX, FIX_DL, FIX_AP)
        self._has_fix = game.has_fix
        if fix_only:
            self._weights = (0.0, 0.0, 0.5, 0.5)
        elif self._has_fix:
            self._weights = (0.35, 0.25, 0.20, 0.20)
        else:
            self._weights = (0.50, 0.50, 0.00, 0.00)
        self._stage_progress = [0, 0, 0, 0]  # DL, EX, FIX_DL, FIX_AP

    def cancel(self):
        """Thread-safe: request the worker to stop at the next checkpoint."""
        self._cancel.set()
        self._size_probe_stop.set()

    # -- progress helpers ---------------------------------------------------
    @staticmethod
    def _fmt_bytes(b: int) -> str:
        """Format bytes nicely: 412 MB, 1.2 GB, etc."""
        for unit, size in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
            if b >= size:
                return f"{b / size:.1f} {unit}"
        return f"{b} B"

    @staticmethod
    def _fmt_time(seconds: float) -> str:
        """Format seconds: 45s, 1m 34s, 1h 22m, etc."""
        if seconds < 60:
            return f"{int(seconds)}s"
        minutes = seconds / 60
        if minutes < 60:
            m, s = int(minutes), int(seconds % 60)
            return f"{m}m {s}s"
        hours = minutes / 60
        h, rem = int(hours), int(minutes % 60)
        return f"{h}h {rem}m"

    def _transfer_eta(self, done, total, speed):
        """Report the current part and whole-set estimate without guessing sizes."""
        part = self._fmt_time(max(0, total - done) / speed) if total and speed > 0 else None
        if not self._part_sizes:
            self.eta_text.emit(f"Part: {part} remaining" if part else "Part: calculating...")
            return
        if total and total > 0:
            self._part_sizes[self._part_index] = total
        future = self._part_sizes[self._part_index + 1:]
        remaining = max(0, (total or 0) - done)
        for index in range(self._part_index + 1, len(self._part_sizes)):
            size = self._part_sizes[index] or 0
            cached = self._part_cached[index] if index < len(self._part_cached) else 0
            remaining += max(0, size - cached)
        whole = self._fmt_time(remaining / speed) if total and all(future) and speed > 0 else None
        self.eta_text.emit(f"Part: {part or 'calculating'}  |  Archive set: {whole or 'unknown'}")

    def _update_overall_progress(self):
        """Emit overall_progress based on current stage progress."""
        overall = sum(s * w for s, w in zip(self._stage_progress, self._weights))
        self.overall_progress.emit(int(overall))

    def _emit_status(self, stage_name: str, step_pct: int, details: str = ""):
        """Emit status text and update elapsed time."""
        elapsed = time.monotonic() - self._start_time if self._start_time else 0
        elapsed_str = self._fmt_time(elapsed)
        self.elapsed_text.emit(elapsed_str)

        if step_pct < 0:
            step_str = stage_name
        else:
            step_str = f"{stage_name} {step_pct}%"
        if details:
            step_str += f"  {details}"
        self.status.emit(step_str)

    # -- temp bookkeeping ---------------------------------------------------
    def _new_temp_dir(self) -> str:
        path = tempfile.mkdtemp(prefix="gamefix_", dir=_cache_root(self.dest_path, self.game.id))
        self._temp_dirs.append(path)
        return path

    def _cleanup_temp(self):
        for path in self._temp_files:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        for path in self._temp_dirs:
            shutil.rmtree(path, ignore_errors=True)
        self._temp_files = []
        self._temp_dirs = []

    def _finish_cleanup(self):
        """Clean temp files then, if this install is the one that added the
        %TEMP% Defender exclusion, remove it again now that extraction is
        fully done. A %TEMP% exclusion that pre-existed is left untouched.
        Clears the flag so a later call (e.g. from the finally guard in
        run()) is a no-op."""
        self._cleanup_temp()
        if self._remove_temp_exclusion:
            _log.debug("_finish_cleanup  removing %%TEMP%% exclusion  path=%r",
                       tempfile.gettempdir())
            self._remove_temp_exclusion = False
            remove_defender_exclusion(tempfile.gettempdir())

    def _purge_cache(self):
        """Delete this install's persistent download-cache folders. Called only
        on full success — on failure the folders (and their .part files) are
        intentionally left behind so a retry can resume."""
        for path in self._cache_sets:
            shutil.rmtree(path, ignore_errors=True)
        self._cache_sets = []

    def _abort(self):
        self._cleanup_temp()
        self.canceled.emit()

    def _cancelled(self) -> bool:
        """If cancellation was requested, abort (cleanup + signal) and report."""
        if self._cancel.is_set():
            self._abort()
            return True
        return False

    def _remove_file(self, path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
        if path in self._temp_files:
            self._temp_files.remove(path)

    # -- main entry point ---------------------------------------------------
    @pyqtSlot()
    def run(self):
        try:
            self._start_time = time.monotonic()
            self.elapsed_text.emit("0s")
            _log.debug(
                "run START  id=%r  name=%r  zip_url=%r  fix_url=%r  dest=%r",
                self.game.id, self.game.name,
                self.game.zip_url, self.game.fix_url, self.dest_path,
            )

            # --- Fix-only fast path (skip main archive entirely) ---------------
            if self._fix_only:
                if not self.game.has_fix:
                    self.error.emit(f"{self.game.name} has no repair patch configured.")
                    return
                _log.debug(
                    "run FIX_ONLY  id=%r  name=%r  fix_url=%r  dest=%r",
                    self.game.id, self.game.name, self.game.fix_url, self.dest_path,
                )
                self._stage_start = time.monotonic()
                fix_archive = self._download_parts(
                    self.game.fix_parts, "Downloading fix",
                    self.fix_download_progress, stage_idx=2, kind="fix",
                    fallback_parts=self.game.fix_backup_parts)
                if fix_archive is None:
                    return
                if self._cancelled():
                    return
                self._stage_start = time.monotonic()
                if not self._apply_fix(fix_archive, self.dest_path,
                                       self.game.fix_url, stage_idx=3):
                    return
                self._remove_parts()
                if self._cancelled():
                    return
                self._finish_cleanup()
                self._purge_cache()
                elapsed = time.monotonic() - self._start_time
                self.elapsed_text.emit(self._fmt_time(elapsed))
                self.status.emit(f"Fix applied in {self._fmt_time(elapsed)}")
                self.overall_progress.emit(100)
                self.success.emit(self.dest_path)
                return

            if not self.game.zip_parts:
                self.error.emit("This game has no download URL configured.")
                return
            if not self._prepare_destination():
                return
            try:
                ensure_destination_volume(self.dest_path)
            except OSError as exc:
                self.error.emit(str(exc))
                return

            # --- 1) Main game: download (one or more volumes) then extract ---
            self._stage_start = time.monotonic()
            main_archive = self._download_parts(
                self.game.zip_parts, "Downloading game",
                self.download_progress, stage_idx=0, kind="main",
                fallback_parts=self.game.zip_backup_parts)
            if main_archive is None:
                return
            if self._cancelled():
                return
            try:
                ensure_destination_volume(self.dest_path)
            except OSError as exc:
                self.error.emit(str(exc))
                return
            self._stage_start = time.monotonic()
            # Extract to staging so wrapper-folder detection can run before any
            # files land in the install folder. For a multi-volume RAR set, the
            # sibling .partN.rar files sit next to this first volume, so 7-Zip
            # pulls them in automatically and reassembles the whole archive.
            main_staging = self._new_temp_dir()
            if not self._extract_archive(main_archive, main_staging,
                                         self.game.zip_url, self.extract_progress, stage_idx=1):
                return
            self._remove_parts()
            if self._cancelled():
                return
            try:
                ensure_destination_volume(self.dest_path)
            except OSError as exc:
                self.error.emit(str(exc))
                return
            # Detect and skip a single outer wrapper folder (e.g. archive
            # contains GangBeasts/ → install to dest_path, not dest_path/GangBeasts/).
            content_root = self._find_content_root(main_staging)
            _log.debug("run  content_root=%s  dest=%s", content_root, self.dest_path)
            try:
                if not self._merge_tree(content_root, self.dest_path,
                                        self.extract_progress, stage_idx=1,
                                        status_text="Installing"):
                    return
            except PermissionError:
                self._cleanup_temp()
                self.error.emit(
                    "Permission denied while installing game files.\n"
                    "Choose a different destination folder."
                )
                return
            except OSError as exc:
                self._cleanup_temp()
                self.error.emit(f"Installation failed:\n{exc}")
                return
            if self._cancelled():
                return

            # Apply an independent update archive before any repair. It is not
            # another volume of the base archive and must not be extracted with it.
            if self.game.update_parts:
                if len(self.game.update_parts) != 1:
                    self.error.emit("Multipart game updates are not supported yet.")
                    return
                update_archive = self._download_parts(
                    self.game.update_parts, "Downloading update",
                    self.fix_download_progress, stage_idx=2, kind="game-update",
                    fallback_parts=self.game.update_backup_parts)
                if update_archive is None or self._cancelled():
                    return
                if not self._apply_fix(update_archive, self.dest_path,
                                       self.game.update_parts[0]["url"], stage_idx=3):
                    return
                self._remove_parts()
                if self._cancelled():
                    return

            # The main game is now fully installed to dest_path. Signal this so a
            # later failure (e.g. in the fix stage) can be retried as fix-only —
            # the installed files persist, so only the fix needs redoing.
            self.main_installed.emit()

            # --- 2) Optional fix / repair patch -----------------------------
            if self.game.has_fix:
                _log.debug(
                    "run FIX BRANCH  id=%r  name=%r  fix_url=%r",
                    self.game.id, self.game.name, self.game.fix_url,
                )
                self._stage_start = time.monotonic()
                fix_archive = self._download_parts(
                    self.game.fix_parts, "Downloading fix",
                    self.fix_download_progress, stage_idx=2, kind="fix",
                    fallback_parts=self.game.fix_backup_parts)
                if fix_archive is None:
                    return
                _log.debug("run FIX ARCHIVE  path=%s", fix_archive)
                if self._cancelled():
                    return
                self._stage_start = time.monotonic()
                if not self._apply_fix(fix_archive, self.dest_path, self.game.fix_url, stage_idx=3):
                    return
                self._remove_parts()
                if self._cancelled():
                    return

            # --- Done -------------------------------------------------------
            self._finish_cleanup()
            self._purge_cache()
            elapsed = time.monotonic() - self._start_time
            self.elapsed_text.emit(self._fmt_time(elapsed))
            self.status.emit(f"Install completed successfully in {self._fmt_time(elapsed)}")
            self.overall_progress.emit(100)
            self.success.emit(self.dest_path)

        except Exception as exc:  # last-resort safety net — never crash the app
            traceback.print_exc()
            self._finish_cleanup()
            self.error.emit(f"Unexpected error:\n{exc}")

        finally:
            self._size_probe_stop.set()
            self._cleanup_temp()
            # Belt-and-suspenders: if a mid-install helper returned False and
            # called _cleanup_temp() directly (skipping _finish_cleanup), the
            # flag is still True and the exclusion hasn't been removed yet.
            if self._remove_temp_exclusion:
                _log.debug("run finally  removing %%TEMP%% exclusion (early-exit path)")
                self._remove_temp_exclusion = False
                remove_defender_exclusion(tempfile.gettempdir())

    # -- destination validation --------------------------------------------
    def _prepare_destination(self) -> bool:
        try:
            os.makedirs(self.dest_path, exist_ok=True)
        except PermissionError:
            self.error.emit(
                "Permission denied creating the destination folder.\n"
                "Try a different location or run with sufficient rights."
            )
            return False
        except OSError as exc:
            self.error.emit(f"Invalid destination folder:\n{exc}")
            return False
        if not os.access(self.dest_path, os.W_OK):
            self.error.emit("The destination folder is not writable.")
            return False
        return True

    # -- download -----------------------------------------------------------
    def _download(self, url, dest_file, status_text, progress_signal, stage_idx=0,
                  frac_base=0.0, frac_span=1.0, part_suffix=".part",
                  report_errors=True) -> bool:
        """Download ``url`` to ``dest_file`` (a persistent cache path), resuming
        an interrupted attempt when possible.

        Bytes stream into ``dest_file + '.part'`` and are promoted to the final
        name only once the whole file has arrived. On failure the ``.part`` is
        left in place so the next attempt continues from where this one stopped
        (via an HTTP Range request) instead of re-downloading from zero. A
        ``dest_file`` that already exists is treated as a completed download and
        skipped outright.
        """
        _log.debug("_download ENTER  stage=%d  url=%s", stage_idx, url)
        status_text = f"{status_text} via {download_host_name(url)}"

        # When this download is one volume of a multi-part set, frac_base /
        # frac_span map its local 0-100% onto the slice of the stage it owns
        # (e.g. part 2 of 3 → 33%..67%), so both the bar and the overall
        # progress advance smoothly across volumes instead of resetting.
        def _emit_pct(pct):
            if self._part_sizes and all(self._part_sizes):
                previous = sum(self._part_sizes[:self._part_index])
                current = self._part_sizes[self._part_index]
                scaled = int((previous + current * pct / 100) * 100 / sum(self._part_sizes))
            else:
                scaled = int(round(frac_base * 100 + pct * frac_span))
            scaled = max(0, min(100, scaled))
            progress_signal.emit(scaled)
            self._stage_progress[stage_idx] = scaled

        def _report_error(message):
            if report_errors:
                self.error.emit(message)

        _emit_pct(0)
        self._update_overall_progress()

        if provider_for(url):
            # Use the same browser-session and verified transfer engine as the
            # native Mac app. Never append one host's bytes to another's partial.
            target = dest_file + ".backup" if part_suffix == ".backup.part" else dest_file
            if os.path.isfile(target):
                self._emit_status(f"Verifying cached file — {status_text}", -1)
            else:
                self._emit_status(status_text, -1)
            transfer = {"start": None, "offset": 0}

            def hosted_progress(done, total):
                now = time.monotonic()
                if transfer["start"] is None:
                    transfer.update(start=now, offset=max(0, done - DOWNLOAD_CHUNK))
                elapsed = max(now - transfer["start"], 0.001)
                speed = (done - transfer["offset"]) / elapsed
                self.speed_text.emit(f"{speed / 1e6:.1f} MB/s")
                self._transfer_eta(done, total, speed)
                if total:
                    pct = min(100, int(done * 100 / total))
                    _emit_pct(pct)
                    self._emit_status(status_text, pct,
                                      f"{self._fmt_bytes(done)} / {self._fmt_bytes(total)}")
                else:
                    progress_signal.emit(-1)
                    self._emit_status(status_text, -1, self._fmt_bytes(done))
                self._update_overall_progress()

            try:
                HostedDownloader(
                    stop=self._cancel.is_set, progress=hosted_progress,
                    status=lambda message: self._emit_status(message, -1),
                ).fetch(url, target)
                if target != dest_file:
                    os.replace(target, dest_file)
                    if os.path.exists(target + ".json"):
                        os.replace(target + ".json", dest_file + ".json")
                _emit_pct(100)
                self._update_overall_progress()
                return True
            except DownloadStopped:
                self._abort()
                return False
            except Exception as exc:
                # Resolver output includes temporary credentials; never log it.
                _report_error(f"Download failed: {exc}")
                return False

        # Fully downloaded on a previous attempt → nothing to fetch.
        try:
            if os.path.exists(dest_file) and os.path.getsize(dest_file) > 0:
                _log.debug("_download SKIP cached-complete  stage=%d  %s",
                           stage_idx, dest_file)
                _emit_pct(100)
                self._update_overall_progress()
                return True
        except OSError:
            pass

        part_file = dest_file + part_suffix
        try:
            resume_offset = os.path.getsize(part_file) if os.path.exists(part_file) else 0
        except OSError:
            resume_offset = 0
        _log.debug("_download stage=%d  resume_offset=%d  part=%s",
                   stage_idx, resume_offset, part_file)
        if resume_offset > 0:
            self._emit_status(f"{status_text} (resuming)", -1)
        else:
            self._emit_status(status_text, -1)

        def _finalize():
            """Promote the completed .part to its final name."""
            try:
                os.replace(part_file, dest_file)
            except OSError as exc:
                _log.debug("_download finalize replace failed  %s", exc)

        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        if url.startswith("https://pixeldrain.com/u/"):
            url = canonical_source(url)

        # Google Drive links route through the dedicated handler, which deals
        # with the large-file virus-scan interstitial and validates that we got
        # the file (not the HTML warning page). All other URLs fall through to
        # the generic streaming path below.
        if _is_gdrive_url(url):
            file_id = _gdrive_file_id(url)
            _log.debug("_download GDRIVE route  file_id=%r  url=%s", file_id, url)
            if not file_id:
                _report_error("Could not read the Google Drive file ID from the link.")
                return False

            stage_start = time.monotonic()
            last_update = [stage_start]  # boxed so the closure can mutate it

            def _gd_progress(done, total):
                now = time.monotonic()
                if now - last_update[0] < 0.3 and (total <= 0 or done < total):
                    return  # throttle UI updates to ~3/sec
                last_update[0] = now
                elapsed = now - stage_start
                if total > 0:
                    pct = int(done * 100 / total)
                    _emit_pct(pct)
                    moved = max(done - resume_offset, 1)
                    speed = moved / elapsed if elapsed > 0 else 0
                    remaining = total - done
                    eta = remaining / speed if speed > 0 else 0
                    speed_str = f"{speed / 1e6:.1f} MB/s" if speed > 0 else "calculating..."
                    eta_str = self._fmt_time(eta) if eta > 0 else "calculating..."
                    bytes_str = f"{self._fmt_bytes(done)} / {self._fmt_bytes(total)}"
                    self.speed_text.emit(speed_str)
                    self._transfer_eta(done, total, speed)
                    self._emit_status(status_text, pct, bytes_str)
                    self._update_overall_progress()
                else:
                    progress_signal.emit(-1)  # indeterminate
                    self._emit_status(status_text, -1, f"{self._fmt_bytes(done)}")

            try:
                ok = download_gdrive(file_id, part_file, _gd_progress,
                                     session=session,
                                     cancel=self._cancel,
                                     api_key=GDRIVE_API_KEY,
                                     resume_offset=resume_offset)
            except OSError as exc:
                _log.debug("_download GDRIVE OSError  stage=%d  %s", stage_idx, exc)
                _report_error(f"Could not write the downloaded file:\n{exc}")
                return False

            if self._cancel.is_set():
                self._abort()
                return False
            # _validate_not_html deletes the .part if Google handed us the HTML
            # warning page, so a retry starts clean rather than resuming garbage.
            if not ok or not _validate_not_html(part_file):
                _report_error(
                    "Google Drive download failed.\n\n"
                    "The file may be private, the per-file download quota may be "
                    "exceeded, or Google returned its virus-scan warning page "
                    "instead of the file. Please try again later."
                )
                return False

            _finalize()
            _emit_pct(100)
            self._update_overall_progress()
            try:
                with open(dest_file, "rb") as _probe:
                    _first16 = _probe.read(16)
                _log.debug("_download GDRIVE DONE  stage=%d  first16=%s (%r)",
                           stage_idx, _first16.hex(), _first16[:16])
            except OSError:
                pass
            return True

        headers = {"Range": f"bytes={resume_offset}-"} if resume_offset > 0 else None
        try:
            with session.get(url, headers=headers, stream=True,
                             timeout=REQUEST_TIMEOUT) as resp:
                _log.debug(
                    "_download RESPONSE  status=%d  content-type=%r  "
                    "content-length=%r  content-range=%r  url-after-redirects=%s",
                    resp.status_code,
                    resp.headers.get("Content-Type"),
                    resp.headers.get("Content-Length"),
                    resp.headers.get("Content-Range"),
                    resp.url,
                )
                # 416 = the partial already covers the whole file → it's done.
                if resp.status_code == 416:
                    _finalize()
                    _emit_pct(100)
                    self._update_overall_progress()
                    return True
                resp.raise_for_status()

                # 206 means the server honoured Range: append to the partial.
                # Anything else is a full body, so overwrite from the start.
                resumed = resume_offset > 0 and resp.status_code == 206
                if resumed:
                    cr = resp.headers.get("Content-Range", "")
                    m = re.search(r"/\s*(\d+)\s*$", cr)
                    total = int(m.group(1)) if m else 0
                    done = resume_offset
                    mode = "ab"
                else:
                    total = resp.headers.get("Content-Length")
                    total = int(total) if total and total.isdigit() else 0
                    done = 0
                    mode = "wb"
                stage_start = time.monotonic()
                last_update = stage_start

                with open(part_file, mode) as out:
                    for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK):
                        if self._cancel.is_set():
                            self._abort()
                            return False
                        if not chunk:
                            continue
                        out.write(chunk)
                        done += len(chunk)

                        now = time.monotonic()
                        # Update at most every 0.3 seconds to avoid spam
                        if now - last_update < 0.3 and done < total:
                            continue
                        last_update = now

                        elapsed = now - stage_start
                        if total > 0:
                            pct = int(done * 100 / total)
                            _emit_pct(pct)
                            # Speed/ETA over bytes moved this session, not total.
                            moved = max(done - (resume_offset if resumed else 0), 1)
                            speed = moved / elapsed if elapsed > 0 else 0
                            remaining = total - done
                            eta = remaining / speed if speed > 0 else 0
                            speed_str = f"{speed / 1e6:.1f} MB/s" if speed > 0 else "calculating..."
                            eta_str = self._fmt_time(eta) if eta > 0 else "calculating..."
                            bytes_str = f"{self._fmt_bytes(done)} / {self._fmt_bytes(total)}"
                            self.speed_text.emit(speed_str)
                            self._transfer_eta(done, total, speed)
                            self._emit_status(status_text, pct, bytes_str)
                            self._update_overall_progress()
                        else:
                            progress_signal.emit(-1)  # indeterminate
                            self._emit_status(status_text, -1, f"{self._fmt_bytes(done)}")

            _finalize()
            # When a backup is configured, a host may respond with an HTML
            # error/download page and HTTP 200. Treat that as a failed source
            # so the matching backup is attempted instead of passing the page
            # to archive extraction.
            if not report_errors and not _validate_not_html(dest_file):
                _log.debug("_download rejected HTML response  stage=%d  url=%s", stage_idx, url)
                return False
            _emit_pct(100)
            self._update_overall_progress()
            try:
                with open(dest_file, "rb") as _probe:
                    _first16 = _probe.read(16)
                _log.debug(
                    "_download DONE  stage=%d  bytes=%d  first16=%s  (%r)",
                    stage_idx, done, _first16.hex(), _first16[:16],
                )
            except OSError:
                pass
            return True

        # On every failure below the .part file is deliberately left on disk so
        # the next attempt resumes instead of restarting.
        except requests.exceptions.MissingSchema:
            _log.debug("_download ERROR  stage=%d  MissingSchema  url=%s", stage_idx, url)
            _report_error("The download URL is invalid.")
            return False
        except requests.exceptions.InvalidURL:
            _log.debug("_download ERROR  stage=%d  InvalidURL  url=%s", stage_idx, url)
            _report_error("The download URL is invalid.")
            return False
        except requests.exceptions.ConnectionError as exc:
            _log.debug("_download ERROR  stage=%d  ConnectionError  %s", stage_idx, exc)
            _report_error(
                "Network error: could not reach the download server.\n"
                "Check your connection and try again."
            )
            return False
        except requests.exceptions.Timeout as exc:
            _log.debug("_download ERROR  stage=%d  Timeout  %s", stage_idx, exc)
            _report_error("The download timed out. Please try again.")
            return False
        except requests.exceptions.HTTPError as exc:
            _log.debug("_download ERROR  stage=%d  HTTPError  %s", stage_idx, exc)
            _report_error(f"Download failed (server returned an error):\n{exc}")
            return False
        except requests.exceptions.RequestException as exc:
            _log.debug("_download ERROR  stage=%d  RequestException  %s", stage_idx, exc)
            _report_error(f"Download failed:\n{exc}")
            return False
        except OSError as exc:
            _log.debug("_download ERROR  stage=%d  OSError  %s", stage_idx, exc)
            _report_error(f"Could not write the downloaded file:\n{exc}")
            return False

    # -- multi-volume download ---------------------------------------------
    def _download_parts(self, parts, status_text, progress_signal, stage_idx=0,
                        kind="main", fallback_parts=None):
        """Download one or more archive volumes into a single shared cache dir
        and return the path of the first volume (the one to hand to the
        extractor), or None on failure / cancellation.

        Volumes go to a *persistent* cache folder (keyed by game + stage + URLs)
        rather than a throwaway temp dir, so a partially-downloaded set survives
        a failure and is resumed on the next attempt instead of re-fetched. The
        folder is removed once the game finishes installing (see _purge_cache).

        Multi-volume RAR sets work by placing every ``.partN.rar`` file in the
        same folder; 7-Zip is then pointed at the first volume and pulls in the
        rest by name. A single-archive download is just a one-element list, so
        the common path behaves exactly as before.
        """
        fallback_parts = fallback_parts or []
        try:
            ensure_destination_volume(self.dest_path)
        except OSError as exc:
            self.error.emit(str(exc))
            return None
        if fallback_parts and len(fallback_parts) != len(parts):
            self.error.emit(
                "The backup links do not match the number of archive parts. "
                "Each primary part needs one matching backup link."
            )
            return None
        # Include both source sets in the cache identity so a changed backup
        # configuration cannot accidentally reuse another download set.
        part_dir = cache_set_dir(self.game.id, kind, parts + fallback_parts,
                                 self.dest_path)
        if part_dir not in self._cache_sets:
            self._cache_sets.append(part_dir)
        count = len(parts)
        # Cached receipts are local; network probes run in the background so
        # slow hosts cannot delay or block the actual download.
        from urllib.parse import urlparse
        self._size_probe_stop.set()
        self._size_probe_stop = threading.Event()
        probe_stop = self._size_probe_stop
        self._part_sizes = [None] * count
        self._part_cached = [0] * count
        sizes = self._part_sizes
        for index, part in enumerate(parts):
            if self._cancel.is_set():
                self._abort()
                return None
            filename = self._part_filename(part, index, count)
            cache_path = os.path.join(part_dir, filename)
            try:
                with open(cache_path + '.json', encoding='utf-8') as stream:
                    receipt = json.load(stream)
                source_set = {part['url']}
                if fallback_parts:
                    source_set.add(fallback_parts[index]['url'])
                size = receipt.get('expected_size') or receipt.get('total')
                if receipt.get('source') in source_set and isinstance(size, int) and not isinstance(size, bool) and size > 0:
                    self._part_sizes[index] = size
                    if receipt.get('complete') and os.path.isfile(cache_path) and os.path.getsize(cache_path) == size:
                        self._part_cached[index] = size
                    elif not fallback_parts and (receipt.get('etag') or receipt.get('modified')):
                        partial = cache_path + '.part'
                        if os.path.isfile(partial):
                            self._part_cached[index] = min(size, os.path.getsize(partial))
                    continue
            except (OSError, ValueError):
                pass
        def probe_sizes():
            for index, part in enumerate(parts):
                if probe_stop.is_set() or self._cancel.is_set():
                    break
                if sizes[index] is not None:
                    continue
                parsed = urlparse(part['url'])
                try:
                    if provider_for(part['url']) == 'gofile' and re.fullmatch(r'/d/[A-Za-z0-9]+/?', parsed.path):
                        deadline = time.monotonic() + 15
                        metadata = resolve_source(part['url'], stop=lambda: probe_stop.is_set() or self._cancel.is_set() or time.monotonic() >= deadline)
                        size = metadata.get('size')
                        if isinstance(size, int) and not isinstance(size, bool) and size > 0:
                            sizes[index] = size
                    elif (parsed.scheme == 'https' and not parsed.username and not parsed.password
                          and parsed.port in (None, 443) and (
                            (parsed.hostname in ('archive.org', 'www.archive.org') and parsed.path.startswith('/download/') and not parsed.query)
                            or (parsed.hostname == 'drive.usercontent.google.com' and parsed.path == '/download')
                            or (parsed.hostname == 'pixeldrain.com' and parsed.path.startswith('/api/file/'))
                            or (parsed.hostname == 'files.catbox.moe' and parsed.path.count('/') == 1))):
                        with requests.head(part['url'], allow_redirects=True, timeout=(3, 3)) as response:
                            length = response.headers.get('Content-Length', '')
                            content_type = response.headers.get('Content-Type', '').lower()
                            if response.status_code == 200 and length.isdigit() and int(length) > 0 and not any(x in content_type for x in ('text/html', 'application/json', 'text/plain')):
                                sizes[index] = int(length)
                    elif provider_for(part['url']) == 'filekeeper':
                        deadline = time.monotonic() + 15
                        metadata = resolve_source(part['url'], stop=lambda: probe_stop.is_set() or self._cancel.is_set() or time.monotonic() >= deadline)
                        if probe_stop.is_set() or self._cancel.is_set():
                            break
                        with requests.Session() as session:
                            for cookie in metadata['cookies']:
                                session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'], path=cookie.get('path', '/'))
                            with session.get(metadata['url'], headers={'User-Agent': metadata['user_agent'], 'Referer': part['url'], 'Range': 'bytes=0-0', 'Accept-Encoding': 'identity'}, stream=True, allow_redirects=False, timeout=(3, 5)) as response:
                                content_range = response.headers.get('Content-Range', '')
                                match = re.fullmatch(r'bytes 0-0/(\d+)', content_range)
                                content_type = response.headers.get('Content-Type', '').lower()
                                if response.status_code == 206 and match and int(match.group(1)) > 0 and not any(x in content_type for x in ('text/html', 'application/json')):
                                    sizes[index] = int(match.group(1))
                except Exception:
                    # Metadata is optional; downloads proceed with unknown ETA.
                    pass
        if count > 1:
            threading.Thread(target=probe_sizes, daemon=True, name='game-size-probe').start()
        local_paths = []
        for index, part in enumerate(parts):
            self._part_index = index
            self.eta_text.emit('Part: calculating  |  Archive set: unknown')
            filename = self._part_filename(part, index, count)
            dest = os.path.join(part_dir, filename)
            suffix = f"  (part {index + 1}/{count})" if count > 1 else ""
            self._stage_start = time.monotonic()
            backup = fallback_parts[index] if fallback_parts else None
            try:
                with open(dest + ".json", encoding="utf-8") as stream:
                    receipt = json.load(stream)
                cached_source = receipt.get("source")
                choices = {part["url"], backup["url"] if backup else None}
                if receipt.get("complete") and cached_source in choices and os.path.isfile(dest):
                    if self._download(cached_source, dest, status_text + suffix, progress_signal,
                                      stage_idx=stage_idx, frac_base=index / count,
                                      frac_span=1 / count, report_errors=False):
                        local_paths.append(dest)
                        continue
            except (OSError, ValueError):
                pass
            if backup:
                preferred_url, alternate_url = choose_faster_source(
                    part["url"], backup["url"], stop=self._cancel.is_set,
                    status=lambda message: self._emit_status(message, -1),
                )
                ok = self._download(
                    preferred_url, dest, status_text + suffix, progress_signal,
                    stage_idx=stage_idx,
                    frac_base=index / count, frac_span=1 / count,
                    part_suffix=".backup.part" if preferred_url == backup["url"] else ".part",
                    report_errors=False,
                )
                if not ok and not self._cancel.is_set():
                    _log.warning(
                        "Preferred download failed; trying alternate  game=%s kind=%s part=%d",
                        self.game.id, kind, index + 1,
                    )
                    self._emit_status(
                        f"{status_text}{suffix} failed; trying alternate via {download_host_name(alternate_url)}", -1
                    )
                    ok = self._download(
                        alternate_url, dest, status_text + suffix + " (alternate)",
                        progress_signal, stage_idx=stage_idx,
                        frac_base=index / count, frac_span=1 / count,
                        part_suffix=".backup.part" if alternate_url == backup["url"] else ".part",
                        report_errors=False,
                    )
                if not ok and not self._cancel.is_set():
                    self.error.emit(
                        "The main and backup download links both failed. "
                        "Check the links and try again."
                    )
            else:
                ok = self._download(
                    part["url"], dest, status_text + suffix, progress_signal,
                    stage_idx=stage_idx,
                    frac_base=index / count, frac_span=1 / count,
                )
            if not ok:
                return None
            if self._cancel.is_set():
                self._abort()
                return None
            local_paths.append(dest)

        self._last_part_paths = local_paths
        probe_stop.set()
        return self._first_volume(local_paths)

    @staticmethod
    def _part_filename(part, index, count):
        """Local filename for a downloaded volume. Prefer the explicit manifest
        ``name`` (required for 7-Zip to pair the volumes correctly), fall back
        to the filename in the URL, then to a generated name."""
        name = (part.get("name") or "").strip()
        if name:
            return os.path.basename(name.replace("\\", "/"))
        from urllib.parse import urlparse, unquote
        base = os.path.basename(unquote(urlparse(part["url"]).path))
        if base.lower().endswith((".rar", ".zip", ".7z")):
            return base
        return f"archive.part{index + 1}.rar" if count > 1 else "archive.zip"

    @staticmethod
    def _first_volume(paths):
        """Return the volume to start extraction from: the ``.part1.rar``
        (lowest part number) when names follow that scheme, else the first
        downloaded file."""
        if not paths:
            return None
        numbered = []
        for p in paths:
            m = re.search(r"\.part0*(\d+)\.rar$", p, re.IGNORECASE)
            if m:
                numbered.append((int(m.group(1)), p))
        if numbered:
            numbered.sort()
            return numbered[0][1]
        return paths[0]

    def _remove_parts(self):
        """Delete the volumes from the most recent _download_parts call (frees
        disk before the fix stage); any stragglers are caught by cleanup."""
        for path in self._last_part_paths:
            self._remove_file(path)
        self._last_part_paths = []

    # -- extraction (ZIP via zipfile, RAR via 7-Zip) ------------------------
    @staticmethod
    def _archive_kind(path, url):
        """Return 'zip', 'rar', '7z', or None — signature first, extension fallback."""
        try:
            with open(path, "rb") as fh:
                sig = fh.read(8)
        except OSError:
            sig = b""

        if sig.startswith(b"PK"):
            kind = "zip"                       # PK\x03\x04 / \x05\x06 / \x07\x08
        elif sig.startswith(b"Rar!"):
            kind = "rar"                       # RAR4 and RAR5
        elif sig.startswith(b"7z\xbc\xaf\x27\x1c"):
            kind = "7z"                        # 7-Zip
        else:
            low = url.lower().split("?")[0]
            if low.endswith(".zip"):
                kind = "zip"
            elif low.endswith(".rar"):
                kind = "rar"
            elif low.endswith(".7z"):
                kind = "7z"
            else:
                kind = None

        _log.debug(
            "_archive_kind  sig=%s (%r)  url=%s  →  %s",
            sig.hex(), sig[:8], url, kind,
        )
        return kind

    # -- extraction completeness -------------------------------------------
    @staticmethod
    def _missing_files_message(missing, dest_dir):
        """Build the explicit incomplete-extraction error text."""
        preview = "\n".join(f"  •  {m}" for m in missing[:8])
        if len(missing) > 8:
            preview += f"\n  …  and {len(missing) - 8} more"
        return (
            f"Extraction finished, but {len(missing)} file(s) never landed in "
            f"the install folder.\n\n"
            f"Microsoft Defender most likely quarantined them the moment they "
            f"were written — crack/fix DLLs such as winmm.dll and "
            f"OnlineFix64.dll are routinely flagged as Trojan:Win32/Malgent "
            f"or similar even though they are harmless.\n\n"
            f"Missing:\n{preview}\n\n"
            f"To fix it, either:\n"
            f"  •  tick \"Add install folder to Microsoft Defender exclusions\" "
            f"in the install dialog and run it again, or\n"
            f"  •  open Windows Security → Protection history, restore the "
            f"quarantined files, then re-run the install.\n\n"
            f"Install folder:\n{dest_dir}"
        )

    def _check_extracted(self, expected, dest_dir) -> bool:
        """Emit an explicit incomplete-extraction error if any expected file is
        absent from dest_dir.

        Returns True when everything is present (or when the caller had no
        member list to compare against). This is what turns a Defender
        quarantine during extraction into a visible failure instead of a
        green 'installed successfully' over a broken game.
        """
        if not expected:
            return True
        missing = []
        for rel in expected:
            target = os.path.join(
                dest_dir, rel.replace("\\", "/").replace("/", os.sep)
            )
            if not os.path.isfile(target):
                missing.append(rel)
        if not missing:
            return True
        _log.debug("_check_extracted  MISSING %d/%d  first=%r",
                   len(missing), len(expected), missing[:5])
        self._cleanup_temp()
        self.error.emit(self._missing_files_message(missing, dest_dir))
        return False

    @staticmethod
    def _list_7zip_files(seven, archive_path):
        """Relative member paths inside an archive, or None if unreadable.

        Uses 7-Zip's technical listing, whose blocks are separated by
        '----------' (the first block being the archive header). Directory
        entries are skipped so only real files are returned.
        """
        try:
            proc = subprocess.run(
                [seven, "l", "-slt",
                 f"-p{ZIP_PASSWORD.decode('utf-8', 'ignore')}", archive_path],
                capture_output=True, text=True, timeout=120,
                creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode not in (0, 1):
            return None

        paths = []
        for block in proc.stdout.split("----------")[1:]:
            rel, is_dir = "", False
            for line in block.splitlines():
                if line.startswith("Path = "):
                    rel = line[len("Path = "):].strip()
                elif line.startswith("Folder = "):
                    is_dir = line[len("Folder = "):].strip() == "+"
                elif line.startswith("Attributes = "):
                    if "D" in line[len("Attributes = "):].split():
                        is_dir = True
            if rel and not is_dir:
                paths.append(rel)
        return paths or None

    def _extract_archive(self, archive_path, dest_dir, url, progress_signal, stage_idx=1) -> bool:
        """Dispatch extraction by archive type. Caller emits the status text."""
        progress_signal.emit(0)
        self._stage_progress[stage_idx] = 0
        self._update_overall_progress()
        kind = self._archive_kind(archive_path, url)
        if kind == "zip":
            return self._extract_zip(archive_path, dest_dir, progress_signal, stage_idx=stage_idx)
        if kind in ("rar", "7z"):
            return self._extract_rar(archive_path, dest_dir, progress_signal, stage_idx=stage_idx)
        self._cleanup_temp()
        self.error.emit(
            "Unsupported archive format.\n"
            "Only .zip, .rar, and .7z downloads are supported."
        )
        return False

    def _extract_zip(self, zip_path, dest_dir, progress_signal, stage_idx=1) -> bool:
        # Pre-open diagnostics
        try:
            _zsize = os.path.getsize(zip_path)
        except OSError:
            _zsize = -1
        _zexists = os.path.isfile(zip_path)
        _zvalid = zipfile.is_zipfile(zip_path) if _zexists else False
        try:
            with open(zip_path, "rb") as _f:
                _zmagic = _f.read(8)
        except OSError:
            _zmagic = b""
        _log.debug(
            "_extract_zip PRE  path=%s  exists=%s  size=%d  "
            "is_zipfile=%s  magic=%s (%r)",
            zip_path, _zexists, _zsize, _zvalid, _zmagic.hex(), _zmagic,
        )

        try:
            with zipfile.ZipFile(zip_path) as zf:
                members = zf.infolist()
                count = len(members)
                _log.debug(
                    "_extract_zip OPEN  members=%d  dest_dir=%s",
                    count, dest_dir,
                )
                if count == 0:
                    self._cleanup_temp()
                    self.error.emit("The downloaded archive is empty.")
                    return False

                # Determine whether the zip is password-protected by probing the
                # first file member.  Try ZIP_PASSWORD first; if it raises
                # RuntimeError with "password required" we know we need it.  If
                # it raises RuntimeError with "bad password" the password is
                # wrong (shouldn't happen for online-fix zips).  If it succeeds
                # or raises anything else (e.g. the zip is unencrypted) we set
                # pwd=None and let ZipFile handle it normally.
                _pwd: bytes | None = None
                for _probe in members:
                    if not _probe.filename.endswith("/"):
                        try:
                            with zf.open(_probe, pwd=ZIP_PASSWORD) as _fh:
                                _fh.read(1)
                            _pwd = ZIP_PASSWORD
                            _log.debug("_extract_zip  password accepted")
                        except RuntimeError as _e:
                            _msg = str(_e).lower()
                            if "password required" in _msg or "bad password" in _msg:
                                _pwd = None
                                _log.debug("_extract_zip  no/wrong password, extracting without")
                        except Exception:
                            _pwd = None
                        break

                # Every regular member we expect to find on disk afterwards.
                expected = []
                _dest_real = os.path.realpath(dest_dir)
                for index, member in enumerate(members, start=1):
                    if self._cancel.is_set():
                        self._abort()
                        return False

                    # Guard against zip-slip: Python < 3.12 does not prevent
                    # '..' components from escaping the staging directory.
                    # Strip every '.' and '..' segment; skip the member if
                    # nothing useful remains.
                    _orig_fn = member.filename
                    _parts = [
                        p for p in _orig_fn.replace("\\", "/").split("/")
                        if p not in ("..", ".", "")
                    ]
                    if not _parts:
                        _log.debug(
                            "_extract_zip  SKIP all-traversal  %r", _orig_fn
                        )
                        continue
                    member.filename = "/".join(_parts) + (
                        "/" if _orig_fn.endswith("/") else ""
                    )
                    if member.filename != _orig_fn:
                        _log.debug(
                            "_extract_zip  sanitized  %r -> %r",
                            _orig_fn, member.filename,
                        )

                    _target = os.path.normpath(
                        os.path.join(dest_dir, member.filename.replace("/", os.sep))
                    )
                    _log.debug(
                        "_extract_zip  [%d/%d]  %r  ->  %s",
                        index, count, member.filename, _target,
                    )
                    try:
                        zf.extract(member, dest_dir, pwd=_pwd)
                    except OSError as _mexc:
                        _log.debug(
                            "_extract_zip  MEMBER FAIL  %r  target=%s  exc=%s",
                            member.filename, _target, _mexc,
                        )
                        raise
                    if not member.is_dir():
                        expected.append(member.filename)

                    pct = int(index * 100 / count)
                    progress_signal.emit(pct)
                    self._stage_progress[stage_idx] = pct
                    if index % max(1, count // 20) == 0:  # Update 20 times
                        self._emit_status("Extracting", pct, f"{index} / {count} files")
                        self._update_overall_progress()

            # A quarantining AV deletes files right after they are written, so
            # zipfile reports success while the folder is short of content.
            if not self._check_extracted(expected, dest_dir):
                return False

            progress_signal.emit(100)
            self._stage_progress[stage_idx] = 100
            self._update_overall_progress()
            return True

        except zipfile.BadZipFile:
            self._cleanup_temp()
            self.error.emit(
                "The downloaded file is not a valid zip archive "
                "(it may be corrupt). Please try again."
            )
            return False
        except PermissionError:
            self._cleanup_temp()
            self.error.emit(
                "Permission denied while extracting files.\n"
                "Choose a different destination folder."
            )
            return False
        except OSError as exc:
            self._cleanup_temp()
            self.error.emit(f"Extraction failed:\n{exc}")
            return False

    def _extract_rar(self, rar_path, dest_dir, progress_signal, stage_idx=1) -> bool:
        seven = find_7zip()
        if not seven:
            self._cleanup_temp()
            self.error.emit(
                "The bundled extraction tool (7-Zip) was not found.\n\n"
                "Try reinstalling the app. If the problem persists, install "
                "7-Zip from https://www.7-zip.org and try again."
            )
            return False

        # Read the member list up front so the post-extraction completeness
        # check knows what should have landed (None = list unreadable, skip).
        expected = self._list_7zip_files(seven, rar_path)

        try:
            os.makedirs(dest_dir, exist_ok=True)
            # x = extract with paths, -o = output dir, -y = assume yes,
            # -p = archive password, -bsp1 = progress to stdout,
            # -bb1 = log names so output flows.
            #
            # online-fix RARs are password-protected (sometimes with encrypted
            # headers, which is why a missing password fails outright instead of
            # just blocking the data). 7-Zip ignores -p for archives that aren't
            # encrypted, so passing it unconditionally is safe for plain RARs.
            # stdin=DEVNULL ensures 7-Zip can never block waiting on a prompt.
            _pwd = ZIP_PASSWORD.decode("utf-8", "ignore")
            proc = subprocess.Popen(
                [seven, "x", rar_path, f"-o{dest_dir}", "-y",
                 f"-p{_pwd}", "-bsp1", "-bb1"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
        except (OSError, ValueError) as exc:
            self._cleanup_temp()
            self.error.emit(f"Could not start 7-Zip for RAR extraction:\n{exc}")
            return False

        # Drain 7-Zip's output on a dedicated thread so the pipe can never fill
        # and deadlock; it parses progress and accumulates text for error diagnosis.
        latest = {"pct": None}
        all_chunks = []

        def _drain():
            buf = b""
            try:
                while True:
                    chunk = proc.stdout.read(256)
                    if not chunk:
                        break
                    all_chunks.append(chunk)
                    buf += chunk
                    found = re.findall(rb"(\d{1,3})%", buf)
                    if found:
                        latest["pct"] = int(found[-1])
                    buf = buf[-256:]  # keep a short tail for split tokens
            except Exception:
                pass

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()

        # Poll the worker thread for completion / cancellation (no blocking I/O
        # here, so cancel stays responsive and QThread.wait() never hangs).
        indeterminate = False
        try:
            while proc.poll() is None:
                if self._cancel.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    reader.join(timeout=2)
                    self._abort()
                    return False
                pct = latest["pct"]
                if pct is not None:
                    progress_signal.emit(max(0, min(100, pct)))
                    self._stage_progress[stage_idx] = max(0, min(100, pct))
                    if pct % 10 == 0:  # Update every 10%
                        self._emit_status("Extracting", pct)
                        self._update_overall_progress()
                elif not indeterminate:
                    progress_signal.emit(-1)  # show a busy bar until % appears
                    indeterminate = True
                time.sleep(0.08)
            reader.join(timeout=2)
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass

        # 0 = OK, 1 = non-fatal warning; anything else is a real failure.
        if proc.returncode not in (0, 1):
            full_output = b"".join(all_chunks).decode("utf-8", errors="replace").lower()
            self._cleanup_temp()
            if "wrong password" in full_output or "encrypted" in full_output:
                self.error.emit(
                    "Extraction failed: wrong or missing password.\n\n"
                    "The archive is password-protected and the built-in "
                    "password did not match."
                )
            elif "crc failed" in full_output or "data error" in full_output or "unexpected end" in full_output:
                self.error.emit(
                    "Extraction failed: the archive appears to be corrupt.\n\n"
                    "Try downloading the game again."
                )
            else:
                self.error.emit(
                    f"Extraction failed — 7-Zip could not read the archive.\n\n"
                    f"Exit code: {proc.returncode}"
                )
            return False

        # Same completeness guard as the zip path: 7-Zip can exit 0 while an AV
        # quietly deletes freshly-written crack DLLs.
        if not self._check_extracted(expected, dest_dir):
            return False

        progress_signal.emit(100)
        self._stage_progress[stage_idx] = 100
        self._update_overall_progress()
        return True

    # -- fix / repair patch -------------------------------------------------
    def _apply_fix(self, archive_path, dest_dir, url, stage_idx=3) -> bool:
        """Extract the fix archive (zip or rar) to a staging dir, resolve the
        correct patch root (smart wrapper-folder detection), then recursively
        merge it into the installed game, overwriting matching files."""
        try:
            _archive_size = os.path.getsize(archive_path)
        except OSError:
            _archive_size = -1
        _log.debug(
            "_apply_fix ENTER  archive_path=%s  size=%d  url=%s  dest_dir=%s",
            archive_path, _archive_size, url, dest_dir,
        )
        # 1) Extract the fix archive into an isolated staging directory. The
        #    same zip/rar dispatch is used as for the main game.
        staging = self._new_temp_dir()
        if not self._extract_archive(archive_path, staging, url,
                                     self.fix_apply_progress, stage_idx=3):
            return False
        if self._cancel.is_set():
            self._abort()
            return False
        try:
            ensure_destination_volume(self.dest_path)
        except OSError as exc:
            self.error.emit(str(exc))
            return False

        # 2) Resolve the real patch root.
        source_root = self._resolve_patch_root(staging, dest_dir)
        _log.debug("_apply_fix  staging=%s  source_root=%s", staging, source_root)

        # 3) Recursively merge into the installed game (overwrite, never delete).
        try:
            return self._merge_tree(source_root, dest_dir)
        except PermissionError:
            self._cleanup_temp()
            self.error.emit(
                "Permission denied while applying the fix.\n"
                "Choose a different destination folder."
            )
            return False
        except OSError as exc:
            self._cleanup_temp()
            self.error.emit(f"Applying the fix failed:\n{exc}")
            return False

    @staticmethod
    def _resolve_patch_root(staging, dest_dir):
        """Depth-first search for the correct merge root.

        Descends through pure wrapper folders (a level that has exactly one
        child and that child is a directory) until a level is reached where
        at least one entry also exists in dest_dir.  That matching level is
        the correct source root for _merge_tree.

        This handles arbitrarily nested wrappers such as:
            gamble_difference/gamble_difference/GameName_Data/...
        where the old one-shot logic would stop one level too early.

        A pure wrapper is defined as: exactly one child, and that child is a
        directory (no loose files at this level).  If the current level has
        multiple entries or any files, the loop stops — either a match was
        already found, or we fall back to the deepest level we reached.
        """
        current = staging
        visited: set = set()
        while True:
            real = os.path.realpath(current)
            if real in visited:            # symlink cycle guard
                break
            visited.add(real)

            try:
                entries = os.listdir(current)
            except OSError:
                break

            if not entries:
                break

            # Found the right level: at least one entry exists in dest_dir.
            if any(os.path.exists(os.path.join(dest_dir, e)) for e in entries):
                _log.debug("_resolve_patch_root  match at %s", current)
                return current

            # Not a match yet. Only descend if this is an unambiguous wrapper:
            # exactly one child, and it is a directory (no loose files here).
            if len(entries) == 1 and os.path.isdir(os.path.join(current, entries[0])):
                _log.debug("_resolve_patch_root  step into %r", entries[0])
                current = os.path.join(current, entries[0])
            else:
                break

        _log.debug("_resolve_patch_root  fallback at %s", current)
        return current

    @staticmethod
    def _find_content_root(staging: str) -> str:
        """Detect and skip a single-folder wrapper in a freshly extracted
        main-game archive.

        If the staging directory contains exactly one entry and that entry is
        a directory, the archive was packed with a wrapper folder (e.g.
        ``GangBeasts/...`` inside ``Gang.Beasts.zip``).  Return that inner
        directory so the caller can merge its contents directly into the
        install folder, avoiding a ``GangBeasts/GangBeasts/...`` result.

        If the staging directory has multiple entries (files or folders at the
        root level) they already belong directly in the install folder, so
        staging itself is returned unchanged.
        """
        try:
            entries = os.listdir(staging)
        except OSError:
            return staging
        if len(entries) == 1 and os.path.isdir(os.path.join(staging, entries[0])):
            _log.debug("_find_content_root  unwrap wrapper %r", entries[0])
            return os.path.join(staging, entries[0])
        _log.debug("_find_content_root  direct root  entries=%d", len(entries))
        return staging

    @staticmethod
    def _copy_bytes(src, target, progress_callback=None):
        """Raw byte copy — no shutil, no chmod, no timestamps, ever."""
        copied = 0
        with open(src, "rb") as fin, open(target, "wb") as fout:
            while True:
                buf = fin.read(1024 * 1024)
                if not buf:
                    break
                fout.write(buf)
                copied += len(buf)
                if progress_callback:
                    progress_callback(copied)

    def _copy_file_tolerant(self, src=None, target=None, progress_callback=None) -> bool:
        r"""Copy src -> target, defeating locks, mappings, and AV interference.

        Three strategies per attempt, in order:
          1. direct byte copy (metadata never touched)
          2. rename the existing target aside, then move the new file in —
             Windows blocks overwriting a file mapped into a running process
             but allows renaming it, so this is what makes a live game folder
             writable
          3. the same direct copy through a \\?\ long-path prefix
        """
        if src is not None and target is None and not isinstance(self, InstallWorker):
            # Called as function/staticmethod: InstallWorker._copy_file_tolerant(src, target, ...)
            src, target = self, src

        def _clear_attr():
            try:
                if os.path.exists(target):
                    os.chmod(target, 0o666)
            except OSError:
                pass

        def _rename_swap():
            tmp = target + ".incoming"
            InstallWorker._copy_bytes(src, tmp, progress_callback=progress_callback)
            old = target + ".old"
            try:
                if os.path.exists(old):
                    os.remove(old)
            except OSError:
                pass
            if os.path.exists(target):
                try:
                    os.replace(target, old)
                except OSError:
                    pass
            os.replace(tmp, target)
            try:
                os.remove(old)
            except OSError:
                pass

        last_exc = None
        for attempt in range(10):
            try:
                _clear_attr()
                InstallWorker._copy_bytes(src, target, progress_callback=progress_callback)
                return True
            except FileNotFoundError:
                raise
            except OSError as exc:
                last_exc = exc
                _log.debug("_copy_file_tolerant  attempt=%d  errno=%s  %s -> %s",
                           attempt + 1, exc.errno, src, target)
                try:
                    _rename_swap()
                    return True
                except OSError as exc2:
                    last_exc = exc2
                    _log.debug("  rename-swap failed  errno=%s", exc2.errno)
                try:
                    InstallWorker._copy_bytes("\\\\?\\" + os.path.abspath(src),
                                              "\\\\?\\" + os.path.abspath(target),
                                              progress_callback=progress_callback)
                    return True
                except OSError:
                    pass
                time.sleep(0.2 * (attempt + 1))
        _log.debug("_copy_file_tolerant FAILED  %s -> %s  %r", src, target, last_exc)
        return False

    def _merge_tree(self, source_root, dest_dir,
                    progress_signal=None, stage_idx=3,
                    status_text="Applying fix") -> bool:
        """Recursively merge source_root into dest_dir: overwrite matching
        files, merge folders, copy new content, never delete existing files.

        progress_signal / stage_idx / status_text default to the fix-apply
        values so existing _apply_fix callers need no change.  Pass
        self.extract_progress / 1 / "Installing" for the main archive path.
        """
        _sig = progress_signal if progress_signal is not None else self.fix_apply_progress

        file_list = []
        failed = []
        for root, _dirs, files in os.walk(source_root):
            for name in files:
                file_list.append(os.path.join(root, name))

        total = len(file_list)
        if total == 0:
            _sig.emit(100)
            self._stage_progress[stage_idx] = 100
            self._update_overall_progress()
            return True

        for index, src in enumerate(file_list, start=1):
            if self._cancel.is_set():
                self._abort()
                return False
            rel = os.path.relpath(src, source_root)
            target = os.path.join(dest_dir, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if not self._copy_file_tolerant(src, target):
                failed.append(rel)
            pct = int(index * 100 / total)
            _sig.emit(pct)
            self._stage_progress[stage_idx] = pct
            if index % max(1, total // 20) == 0:  # Update 20 times
                self._emit_status(status_text, pct, f"{index} / {total} files")
                self._update_overall_progress()

        if failed:
            time.sleep(1.5)  # let AV release its holds
            still_failed = []
            for rel in failed:
                src = os.path.join(source_root, rel)
                target = os.path.join(dest_dir, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                ok = self._copy_file_tolerant(src, target)
                if ok and not os.path.isfile(target):
                    ok = False  # written, then removed — Defender quarantine
                if not ok:
                    still_failed.append(rel)
            failed = still_failed

        if failed:
            self._cleanup_temp()
            preview = "\n".join(f"  •  {m}" for m in failed[:8])
            if len(failed) > 8:
                preview += f"\n  …  and {len(failed) - 8} more"
            self.error.emit(
                f"{len(failed)} file(s) could not be written into the install "
                f"folder.\n\n"
                f"Two causes, both fixable:\n"
                f"  •  Defender quarantined the file — tick the exclusion and "
                f"reinstall, or restore it from Protection history\n"
                f"  •  the game is still running and holds the DLL open — close "
                f"it and retry\n\n"
                f"Failed:\n{preview}\n\n"
                f"Install folder:\n{dest_dir}"
            )
            return False

        _sig.emit(100)
        self._stage_progress[stage_idx] = 100
        self._update_overall_progress()
        return True


# ---------------------------------------------------------------------------
# macCompatibility badge helper  (publish-time data — app only reads)
# ---------------------------------------------------------------------------
_COMPAT_TIER_META = {
    # tier string -> (emoji, object-name-suffix)
    "Native":               ("\U0001f34f", "Native"),    # 🍏 green apple
    "Runs great":           ("\U0001f7e2", "Great"),     # 🟢
    "Runs with a recipe":   ("\U0001f7e1", "Recipe"),    # 🟡
    "Partial":              ("\U0001f7e0", "Partial"),   # 🟠
    "Not yet":              ("\U0001f534", "NotYet"),    # 🔴
}


def _build_compat_badge(game: "Game") -> "QLabel | None":
    """
    Build a compat-tier QLabel for *game*, or return None if no data.

    Badge object names (for QSS styling):
      CompatBadge_Native    — native macOS build confirmed
      CompatBadge_Great     — Runs great
      CompatBadge_Recipe    — Runs with a recipe
      CompatBadge_Partial   — Partial (standard risk)
      CompatBadge_PartialHigh — Partial, riskLevel = High
      CompatBadge_NotYet    — Not yet
      CompatBadge_Unknown   — fallback

    All badges receive an additional data property "confidence" so CSS
    pseudo-class variants can be added without extra widget types.
    Low-confidence entries append "(estimated)" to the badge text.
    """
    if game.mac_compat is None:
        return None

    tier       = game.mac_compat.get("tier", "")
    risk_level = game.mac_compat.get("riskLevel", "Standard")
    confidence = game.mac_compat.get("confidence", "")
    reasoning  = game.mac_compat.get("reasoning", "")

    emoji, suffix = _COMPAT_TIER_META.get(tier, ("\u26aa", "Unknown"))  # ⚪ fallback

    # High-risk Partial gets its own object name for distinct QSS coloring
    if tier == "Partial" and risk_level == "High":
        suffix = "PartialHigh"

    # Low-confidence entries append an "estimated" caveat to the badge text
    estimated_tag = " · estimated" if confidence == "Low" else ""
    text = f"{emoji} {tier}{estimated_tag}"

    lbl = QLabel(text)
    lbl.setObjectName(f"CompatBadge_{suffix}")
    lbl.setProperty("confidence", confidence)   # allows CSS :hover etc.
    if reasoning:
        lbl.setToolTip(reasoning)
    lbl.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return lbl


def _build_compat_reasoning(game: "Game") -> "QLabel | None":
    """
    Build a one-line reasoning QLabel for the install dialog.
    Low-confidence entries append '(unconfirmed — estimated)' to the text.
    Returns None if no data or no reasoning.
    """
    if game.mac_compat is None:
        return None

    reasoning  = game.mac_compat.get("reasoning", "").strip()
    confidence = game.mac_compat.get("confidence", "")

    if not reasoning:
        return None

    if confidence == "Low":
        reasoning = f"{reasoning} (unconfirmed \u2014 estimated)"

    lbl = QLabel(f"\U0001f4bb  {reasoning}")
    obj_name = "CompatReasoning_Low" if confidence == "Low" else "CompatReasoning"
    lbl.setObjectName(obj_name)
    lbl.setWordWrap(True)
    return lbl



# ---------------------------------------------------------------------------
# Game card widget
# ---------------------------------------------------------------------------
def artwork_accent(image):
    """Select a prominent chromatic hue from a small image sample."""
    fallback = QColor('#2dd4bf')
    if image is None or image.isNull():
        return fallback
    sample = image.scaled(32, 32, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    bins = [0.0] * 24
    for y in range(sample.height()):
        for x in range(sample.width()):
            color = sample.pixelColor(x, y)
            hue, saturation, value, alpha = color.getHsv()
            if alpha < 128 or hue < 0 or saturation < 60 or value < 45 or value > 245:
                continue
            bins[hue // 15] += (saturation / 255.0) * (value / 255.0)
    best = max(range(len(bins)), key=bins.__getitem__)
    return QColor.fromHsv(best * 15 + 7, 150, 190) if bins[best] >= 3 else fallback


def ui_motion_enabled():
    try:
        enabled = ctypes.c_int()
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0):
            return bool(enabled.value)
    except (AttributeError, OSError):
        pass
    return True


def game_detail_art_urls(game):
    app_id = str(game._raw.get('steamAppId') or '')
    urls = []
    if app_id.isdigit():
        base = f'https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{app_id}'
        urls.extend([f'{base}/library_hero.jpg', f'{base}/header.jpg'])
    if game.thumbnail and game.thumbnail not in urls:
        urls.append(game.thumbnail)
    return urls


class CardHoverEffect(QGraphicsEffect):
    def __init__(self, card):
        super().__init__(card)
        self.card = card
        self.progress = 0.0
        self.flash = 0.0

    def boundingRectFor(self, rect):
        return rect.adjusted(-22, -26, 22, 22)

    def draw(self, painter):
        if self.progress <= 0 and self.flash <= 0:
            self.drawSource(painter)
            return
        pixmap, offset = self.sourcePixmap(Qt.CoordinateSystem.LogicalCoordinates,
                                           QGraphicsEffect.PixmapPadMode.NoPad)
        if pixmap.isNull():
            return
        p = self.progress
        target = QRectF(offset.x(), offset.y(), pixmap.width() / pixmap.devicePixelRatio(),
                        pixmap.height() / pixmap.devicePixelRatio())
        if ui_motion_enabled():
            expansion = target.width() * 0.017 * p
            target = target.adjusted(-expansion, -expansion - 5*p, expansion, expansion - 5*p)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if p > 0:
            for width in range(22, 1, -2):
                color = QColor(self.card._accent)
                color.setAlpha(round(p * (23 - width) * 1.3))
                painter.setPen(QPen(color, width))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(target.adjusted(1, 1, -1, -1), 14, 14)
        painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
        if self.flash:
            color = QColor(self.card._accent)
            color.setAlpha(round(self.flash * 90))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(target, 14, 14)
        painter.restore()


class _PageTransition(QWidget):
    def __init__(self, parent, outgoing, incoming, direction):
        super().__init__(parent)
        self.outgoing = outgoing
        self.incoming = incoming
        self.direction = direction
        self.progress = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setGeometry(parent.rect())
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(280)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self._advance)
        self.animation.finished.connect(self.deleteLater)
        self.show()
        self.raise_()
        self.animation.start()

    def _advance(self, value):
        self.progress = value
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        distance = min(self.width() * 0.045, 64)
        painter.setOpacity(1.0 - self.progress)
        painter.drawPixmap(
            int(-self.direction * distance * self.progress), 0, self.outgoing)
        painter.setOpacity(self.progress)
        painter.drawPixmap(
            int(self.direction * distance * (1.0 - self.progress)), 0, self.incoming)


class AnimatedStack(QStackedWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._transition = None

    def capture_page(self):
        image = self.grab() if ui_motion_enabled() and self.isVisible() else None
        if self._transition is not None:
            self._transition.animation.stop()
            self._transition.hide()
            self._transition.deleteLater()
            self._transition = None
        return image

    def animate_from(self, image, direction=1):
        if image is not None and not image.isNull():
            incoming = self.grab()
            self._transition = _PageTransition(self, image, incoming, direction)
            transition = self._transition
            transition.animation.finished.connect(
                lambda: setattr(self, '_transition', None) if self._transition is transition else None)

    def setCurrentWidget(self, widget):
        if widget is self.currentWidget():
            return
        old_index = self.currentIndex()
        image = self.capture_page()
        super().setCurrentWidget(widget)
        self.animate_from(image, 1 if self.currentIndex() >= old_index else -1)


class GameCard(QFrame):
    CARD_W = 280
    THUMB_H = 130

    def __init__(self, game: Game, on_click, *, installed=False, on_choose=None,
                 on_favorite=None, is_favorite=False):
        super().__init__()
        self.game = game
        self._on_click = on_click
        self._on_choose = on_choose
        self._installed = installed
        self._accent = QColor(game.card_color or '#2dd4bf')
        self._card_hovered = False
        self._opening = False
        self._card_effect = CardHoverEffect(self)
        self.setGraphicsEffect(self._card_effect)
        self._card_animation = QVariantAnimation(self)
        self._card_animation.setDuration(200 if ui_motion_enabled() else 0)
        self._card_animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._card_animation.valueChanged.connect(self._animate_card)
        self.setObjectName("GameCard")
        self.setFixedWidth(self.CARD_W)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 12)
        root.setSpacing(10)

        self.thumb_container = QWidget(self)
        self.thumb_container.setFixedSize(self.CARD_W, self.THUMB_H)
        self.thumb = QLabel(self.thumb_container)
        self.thumb.setFixedSize(self.CARD_W, self.THUMB_H)
        self.thumb.setObjectName("Thumb")
        self.thumb.setFixedHeight(self.THUMB_H)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setText("Loading image…")
        self.favorite_btn = QPushButton("★" if is_favorite else "☆", self.thumb_container)
        self.favorite_btn.setObjectName("FavoriteButton")
        self.favorite_btn.setFixedSize(36, 36)
        self.favorite_btn.move(self.CARD_W - 46, 10)
        self.favorite_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.favorite_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.favorite_btn.setToolTip("Remove from favorites" if is_favorite else "Add to favorites")
        self.favorite_btn.setAccessibleName(self.favorite_btn.toolTip())
        self.favorite_btn.setStyleSheet(
            "QPushButton#FavoriteButton { background: rgba(10, 16, 28, 205); color: #facc15; "
            "border: 1px solid rgba(255,255,255,75); border-radius: 18px; font-size: 22px; }"
            "QPushButton#FavoriteButton:hover { background: rgba(10, 16, 28, 245); }"
        )
        self.favorite_btn.clicked.connect(lambda: on_favorite(game) if on_favorite else None)
        self.favorite_btn.raise_()
        root.addWidget(self.thumb_container)
        self._normal_art = None
        self._hovered = False
        self._hover_progress = 0.0
        self._hover_fade = QVariantAnimation(self)
        self._hover_fade.setDuration(180 if ui_motion_enabled() else 0)
        self._hover_fade.valueChanged.connect(self._on_hover_progress)

        body = QVBoxLayout()
        body.setContentsMargins(14, 0, 14, 0)
        body.setSpacing(6)

        title = QLabel(game.name)
        title.setObjectName("CardTitle")
        title.setWordWrap(True)
        body.addWidget(title)

        if not installed:
            desc = QLabel(game.description)
            desc.setObjectName("CardDesc")
            desc.setWordWrap(True)
            body.addWidget(desc)

        meta_bits = []
        if game.version and not installed:
            meta_bits.append(f"v{game.version}")
        meta_bits.append(game.size if game.size else "Size unknown")
        if game.has_fix and not installed:
            meta_bits.append("+ patch")
        meta = QLabel("  •  ".join(meta_bits))
        meta.setObjectName("CardMeta")
        body.addWidget(meta)


        if game.players and not installed:
            players_lbl = QLabel(f"👥 {game.players}")
            players_lbl.setObjectName("CardPlayers")
            players_lbl.setWordWrap(True)
            body.addWidget(players_lbl)

        if installed:
            badge = QLabel("● Click to play · right-click for options")
            badge.setObjectName("BadgeAvailable")
        elif game.is_patch:
            badge = QLabel("⚙ Fix / Patch")
            badge.setObjectName("BadgeFix")
        elif game.is_available:
            badge = QLabel("● Available")
            badge.setObjectName("BadgeAvailable")
        else:
            badge = QLabel("Coming Soon")
            badge.setObjectName("BadgeSoon")
        badge.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        body.addWidget(badge)

        root.addLayout(body)

        self._load_thumbnail()

    # -- thumbnail (cached + async) ----------------------------------------
    def _load_thumbnail(self):
        if not self.game.thumbnail:
            self._show_placeholder()
            return
        cached = THUMB_CACHE.get(self.game.thumbnail)
        if cached is not None:
            self._apply_image(cached)
            return
        runnable = ThumbnailRunnable(self.game.thumbnail)
        runnable.signals.done.connect(self._on_thumb_done)
        QThreadPool.globalInstance().start(runnable)

    @pyqtSlot(str, object)
    def _on_thumb_done(self, url, image):
        if image is None:
            self._show_placeholder()
            return
        THUMB_CACHE[url] = image  # cache on the GUI thread (safe)
        self._apply_image(image)

    def _apply_image(self, image):
        self._accent = QColor(self.game.card_color) if self.game.card_color else artwork_accent(image)
        self._update_card_color()
        pix = QPixmap.fromImage(image).scaled(
            self.CARD_W, self.THUMB_H,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = max(0, (pix.width() - self.CARD_W) // 2)
        y = max(0, (pix.height() - self.THUMB_H) // 2)
        cropped = pix.copy(x, y, self.CARD_W, self.THUMB_H)
        rounded = QPixmap(self.CARD_W, self.THUMB_H)
        rounded.fill(Qt.GlobalColor.transparent)
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(0, 0, self.CARD_W, self.THUMB_H, 12, 12)
        painter.setClipPath(clip)
        painter.drawPixmap(0, 0, cropped)
        painter.end()
        self._normal_art = rounded
        self._render_hover()

    def _show_placeholder(self):
        self._accent = QColor(self.game.card_color or '#2dd4bf')
        self._update_card_color()
        self._normal_art = None
        self.thumb.setPixmap(QPixmap())
        self.thumb.setText(self.game.name)
        self.thumb.setProperty("placeholder", True)
        self.thumb.style().unpolish(self.thumb)
        self.thumb.style().polish(self.thumb)
        self._render_hover()

    def _update_card_color(self):
        accent = self._accent
        background = accent.darker(145)
        if 0.2126 * background.red() + 0.7152 * background.green() + 0.0722 * background.blue() > 130:
            background = accent.darker(185)
        self.setStyleSheet(f"#GameCard {{ background-color: {background.name()}; border: 1px solid {background.lighter(120).name()}; border-radius: 14px; }} #GameCard:hover {{ border: 1px solid {accent.lighter(125).name()}; }}")
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)

    def _animate_card(self, value):
        self._card_effect.progress = float(value)
        self._card_effect.update()

    def _set_hovered(self, hovered):
        if self._card_hovered != hovered:
            self._card_hovered = hovered
            if hovered:
                self.raise_()
            self._card_animation.stop()
            self._card_animation.setStartValue(self._card_effect.progress)
            self._card_animation.setEndValue(1.0 if hovered else 0.0)
            self._card_animation.start()
            self.update()
        if self._installed and self._hovered != hovered:
            self._hovered = hovered
            self._hover_fade.stop()
            self._hover_fade.setStartValue(self._hover_progress)
            self._hover_fade.setEndValue(1.0 if hovered else 0.0)
            self._hover_fade.start()

    def _on_hover_progress(self, value):
        self._hover_progress = float(value)
        self._render_hover()

    def _render_hover(self):
        if self._hover_progress <= 0:
            if self._normal_art is not None:
                self.thumb.setPixmap(self._normal_art)
            else:
                self.thumb.setPixmap(QPixmap())
                self.thumb.setText(self.game.name)
            return
        if self._normal_art is not None:
            art = self._normal_art.copy()
            blurred = self._normal_art.scaled(35, 16, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation).scaled(self.CARD_W, self.THUMB_H, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        else:
            art = QPixmap(self.CARD_W, self.THUMB_H)
            art.fill(QColor('#0b0f1a'))
        painter = QPainter(art)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(0, 0, self.CARD_W, self.THUMB_H, 12, 12)
        painter.setClipPath(clip)
        painter.setOpacity(self._hover_progress)
        if self._normal_art is not None:
            painter.drawPixmap(0, 0, blurred)
        painter.fillRect(art.rect(), QColor(5, 10, 20, 150))
        painter.setPen(QColor('white'))
        painter.setFont(QFont('Arial', 16, QFont.Weight.Bold))
        painter.drawText(art.rect(), Qt.AlignmentFlag.AlignCenter, '▶  PLAY')
        painter.end()
        self.thumb.setPixmap(art)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self._opening:
            self._opening = True
            self._card_effect.flash = 1.0 if not self._installed and ui_motion_enabled() else 0.0
            self._card_effect.update()
            def activate():
                self._card_effect.flash = 0.0
                self._card_effect.update()
                try:
                    self._on_click(self.game)
                finally:
                    self._opening = False
            QTimer.singleShot(90 if self._card_effect.flash else 0, activate)
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        self._set_hovered(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._set_hovered(False)
        super().leaveEvent(event)

    def contextMenuEvent(self, event):
        if self._installed and self._on_choose:
            menu = QMenu(self)
            choose = menu.addAction("Choose game executable…")
            if menu.exec(event.globalPos()) == choose:
                self._on_choose(self.game)
        else:
            super().contextMenuEvent(event)


# ---------------------------------------------------------------------------
# Install dialog — owns the worker thread for one install.
# ---------------------------------------------------------------------------
class ItchLoginSignals(QObject):
    finished = pyqtSignal(str, bool)
    authorization_url = pyqtSignal(str)


class ItchLoginTask(QRunnable):
    def __init__(self):
        super().__init__()
        self.signals = ItchLoginSignals()

    def run(self):
        try:
            from pathlib import Path
            destination = Path(desktop_profile()) / "AppData/LocalLow/Innersloth/Among Us/itch"
            itch_sign_in("d9148c070f56eba6b00ae58b3cf9621a", destination,
                         authorization_url=self.signals.authorization_url.emit)
        except Exception as exc:
            self.signals.finished.emit(str(exc), False)
        else:
            self.signals.finished.emit("Login saved. Launch Among Us to verify sign-in.", True)


def default_game_directory():
    saved = load_prefs().get("install_path", "").strip()
    if saved:
        return saved
    try:
        return os.path.join(desktop_profile(), "Games")
    except (OSError, RuntimeError):
        _log.exception("Could not identify the desktop user; require an install folder")
        return ""


class DetailArtwork(QLabel):
    def __init__(self):
        super().__init__()
        self.image = None
        self.setFixedHeight(180)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def set_image(self, image):
        self.image = image
        self.update()

    def paintEvent(self, event):
        if self.image is None or self.image.isNull():
            return super().paintEvent(event)
        target = QRectF(self.rect())
        if target.isEmpty():
            return
        source = QRectF(self.image.rect())
        ratio = max(target.width() / source.width(), target.height() / source.height())
        width, height = target.width() / ratio, target.height() / ratio
        source = QRectF((source.width() - width) / 2, (source.height() - height) / 2, width, height)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        clip = QPainterPath()
        clip.addRoundedRect(target, 12, 12)
        painter.setClipPath(clip)
        painter.drawImage(target, self.image, source)


class InstallDialog(QDialog):
    def __init__(self, game: Game, parent=None, *, patch_target: str | None = None,
                 manager=None):
        super().__init__(parent)
        self.game = game
        self._catalog_game = game
        self._patch_target = patch_target
        self._manager = manager  # DownloadManager; dialog enqueues then closes
        self.thread = None
        self.worker = None
        self._installing = False
        self.defender_thread = None
        self.defender_worker = None
        self._installed_path = None
        self._install_target = None

        title_text = f"Apply Fix — {game.name}" if patch_target else f"Install — {game.name}"
        self.setWindowTitle(title_text)
        self.setMinimumWidth(540)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(13)

        self._detail_header = QWidget()
        detail_layout = QVBoxLayout(self._detail_header)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        self._detail_art = DetailArtwork()
        detail_layout.addWidget(self._detail_art)
        title_row = QHBoxLayout()
        title = QLabel(game.name)
        title.setObjectName("DialogTitle")
        title_row.addWidget(title)
        title_row.addStretch()
        self.fix_only_btn = QPushButton("Download fix only")
        self.fix_only_btn.setObjectName("FixOnlyButton")
        self.fix_only_btn.setVisible(game.has_fix)
        self.fix_only_btn.clicked.connect(self._start_fix_only)
        _log.debug(
            "InstallDialog.__init__  id=%r  name=%r  has_fix=%s  fix_url=%r",
            game.id, game.name, game.has_fix, game.fix_url,
        )
        title_row.addWidget(self.fix_only_btn)
        detail_layout.addLayout(title_row)
        root.addWidget(self._detail_header)
        self._detail_header.hide()
        self._detail_urls = game_detail_art_urls(game)
        self._detail_placeholder = QLabel('Loading artwork…')
        root.insertWidget(0, self._detail_placeholder)
        QTimer.singleShot(0, self._load_detail_art)

        if game.id == "among-us":
            login_button = QPushButton("Set up itch.io login")
            login_button.clicked.connect(self._start_itch_login)
            root.addWidget(login_button)

        desc = QLabel(game.description)
        desc.setObjectName("CardDesc")
        desc.setWordWrap(True)
        root.addWidget(desc)

        self.edition_combo = None
        if game.more_players and not patch_target:
            root.addWidget(self._label("Edition", "SectionLabel"))
            self.edition_combo = QComboBox()
            self.edition_combo.addItem("Normal", "normal")
            self.edition_combo.addItem("More Players", "more-players")
            root.addWidget(self.edition_combo)
            self.edition_note = QLabel()
            self.edition_note.setObjectName("CardMeta")
            self.edition_note.setWordWrap(True)
            root.addWidget(self.edition_note)

        # Keep compatibility metadata in the catalog without displaying it here.
        self._compat_badge = None
        self._compat_reasoning = None

        meta_bits = []
        if game.version:
            meta_bits.append(f"Version {game.version}")
        if game.size:
            meta_bits.append(f"Size {game.size}")
        if game.players:
            meta_bits.append(f"👥 {game.players}")
        if game.has_fix:
            meta_bits.append("Includes repair patch")
        if meta_bits:
            meta = QLabel("   •   ".join(meta_bits))
            meta.setObjectName("CardMeta")
            root.addWidget(meta)
            self._edition_meta = meta
        else:
            self._edition_meta = None


        # "Will it run on your PC?" — compared against this game's requirements.
        self._build_system_check(root)

        # Destination row — hidden for patch installs (folder already chosen).
        self._dest_label = self._label("Install location", "SectionLabel")
        root.addWidget(self._dest_label)
        dest_row = QHBoxLayout()
        self.dest_edit = QLineEdit()
        self.dest_edit.setPlaceholderText("Choose a folder to install into…")
        self.dest_edit.setText(default_game_directory())
        dest_row.addWidget(self.dest_edit)
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.clicked.connect(self._browse)
        dest_row.addWidget(self.browse_btn)
        self._dest_row_widget = QWidget()
        self._dest_row_widget.setLayout(dest_row)
        root.addWidget(self._dest_row_widget)

        self.final_hint = QLabel()
        self.final_hint.setObjectName("CardMeta")
        self.final_hint.setWordWrap(True)
        root.addWidget(self.final_hint)
        self.dest_edit.textChanged.connect(self._update_hint)
        self._update_hint()

        if patch_target:
            self._dest_label.setVisible(False)
            self._dest_row_widget.setVisible(False)
            self.final_hint.setText(f"Will apply fix into:  {patch_target}")

        # Status + progress bars. With a download manager these live on the
        # Downloads page instead, so the whole section is hidden here — the
        # dialog is purely a configure-and-queue step.
        self._progress_box = QWidget()
        pbox = QVBoxLayout(self._progress_box)
        pbox.setContentsMargins(0, 0, 0, 0)
        pbox.setSpacing(13)
        root.addWidget(self._progress_box)

        self.status_label = QLabel("Ready.")
        self.status_label.setObjectName("StatusLabel")
        pbox.addWidget(self.status_label)

        # Detailed metrics row
        metrics_row = QHBoxLayout()
        self.speed_label = QLabel("")
        self.speed_label.setObjectName("MetricLabel")
        metrics_row.addWidget(self.speed_label)
        metrics_row.addStretch()
        self.elapsed_label = QLabel("0s")
        self.elapsed_label.setObjectName("MetricLabel")
        metrics_row.addWidget(self.elapsed_label)
        metrics_row.addSpacing(10)
        self.eta_label = QLabel("")
        self.eta_label.setObjectName("MetricLabel")
        metrics_row.addWidget(self.eta_label)
        pbox.addLayout(metrics_row)

        pbox.addWidget(self._label("Overall progress", "SmallLabel"))
        self.overall_bar = QProgressBar()
        self.overall_bar.setRange(0, 100)
        self.overall_bar.setValue(0)
        pbox.addWidget(self.overall_bar)

        self.dl_bar = self._add_bar(pbox, "Download")
        self.ex_bar = self._add_bar(pbox, "Extraction")
        # Fix bars only shown when this game ships a patch.
        self.fix_dl_label, self.fix_dl_bar = self._add_bar(pbox, "Fix download", ret_label=True)
        self.fix_ap_label, self.fix_ap_bar = self._add_bar(pbox, "Applying fix", ret_label=True)
        if not game.has_fix:
            for w in (self.fix_dl_label, self.fix_dl_bar,
                      self.fix_ap_label, self.fix_ap_bar):
                w.setVisible(False)

        if manager is not None:
            self._progress_box.setVisible(False)

        # Buttons.
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._on_cancel_clicked)
        btn_row.addWidget(self.cancel_btn)
        install_label = "Apply fix" if patch_target else "Install game"
        self.install_btn = QPushButton(install_label)
        self.install_btn.setObjectName("PrimaryButton")
        self.install_btn.clicked.connect(self._start_install)
        btn_row.addWidget(self.install_btn)
        root.addLayout(btn_row)

        if not game.is_available:
            self.install_btn.setEnabled(False)
            self.install_btn.setText("Coming Soon")
            self.status_label.setText("This game isn't available to download yet.")

        if self.edition_combo is not None:
            self.edition_combo.currentIndexChanged.connect(self._change_edition)
            self._change_edition()

    # -- ui helpers ---------------------------------------------------------
    def _start_itch_login(self):
        app = QApplication.instance()
        if getattr(app, "_itch_login_task", None) is not None:
            QMessageBox.information(self, "itch.io login", "A sign-in is already in progress.")
            return
        task = ItchLoginTask()
        app._itch_login_task = task
        def show_link(url):
            dialog = QMessageBox(None)
            dialog.setWindowTitle("itch.io login")
            dialog.setText("Complete sign-in in your browser. If it did not open, copy the link below and open it on this computer.")
            dialog.setDetailedText(url)
            copy_button = dialog.addButton("Copy login link", QMessageBox.ButtonRole.ActionRole)
            dialog.addButton("Close", QMessageBox.ButtonRole.RejectRole)
            copy_button.clicked.connect(lambda: QApplication.clipboard().setText(url))
            app._itch_login_dialog = dialog
            dialog.show()
        def finished(message, success):
            app._itch_login_task = None
            dialog = getattr(app, "_itch_login_dialog", None)
            if dialog is not None:
                dialog.close()
                app._itch_login_dialog = None
            (QMessageBox.information if success else QMessageBox.warning)(None, "itch.io login", message)
        task.signals.authorization_url.connect(show_link)
        task.signals.finished.connect(finished)
        QThreadPool.globalInstance().start(task)

    def _load_detail_art(self):
        if not self._detail_urls:
            self._detail_art.hide()
            self._detail_placeholder.hide()
            self._detail_header.show()
            return
        url = self._detail_urls.pop(0)
        cached = THUMB_CACHE.get(url)
        if cached is not None:
            self._detail_art_done(url, cached)
            return
        task = ThumbnailRunnable(url)
        task.signals.done.connect(self._detail_art_done)
        QThreadPool.globalInstance().start(task)

    @pyqtSlot(str, object)
    def _detail_art_done(self, url, image):
        if image is None:
            self._load_detail_art()
            return
        THUMB_CACHE[url] = image
        self._detail_art.set_image(image)
        self._detail_placeholder.hide()
        self._detail_header.show()

    def _label(self, text, obj):
        lbl = QLabel(text)
        lbl.setObjectName(obj)
        return lbl

    def _add_bar(self, layout, caption, ret_label=False):
        lbl = self._label(caption, "SmallLabel")
        layout.addWidget(lbl)
        bar = QProgressBar()
        bar.setValue(0)
        layout.addWidget(bar)
        if ret_label:
            return lbl, bar
        return bar

    def _build_system_check(self, root):
        """Render the 'will it run?' box from this game's requirements. No-op
        when the game has no requirements listed."""
        if not self.game.requirements:
            return

        # Storage is checked against the drive we'll install to. dest_edit
        # doesn't exist yet, so resolve the same default path it will show.
        default_dest = default_game_directory()
        path_for_storage = self._patch_target or default_dest
        result = evaluate_requirements(self.game.requirements, path_for_storage)

        frame = QFrame()
        frame.setObjectName("SysCheckBox")
        box = QVBoxLayout(frame)
        box.setContentsMargins(14, 10, 14, 12)
        box.setSpacing(6)

        platform_note = QLabel("Windows minimum requirements · Mac compatibility is reported separately.")
        platform_note.setObjectName("SmallLabel")
        platform_note.setWordWrap(True)
        box.addWidget(platform_note)

        verdict = QLabel(result["summary"])
        verdict.setObjectName({
            "pass": "SysVerdictPass", "warn": "SysVerdictWarn",
            "fail": "SysVerdictFail",
        }.get(result["verdict"], "SysVerdictUnknown"))
        verdict.setWordWrap(True)
        box.addWidget(verdict)

        def esc(s):
            return (str(s).replace("&", "&amp;")
                    .replace("<", "&lt;").replace(">", "&gt;"))

        icons = {"pass": ("✓", "#22c55e"), "fail": ("✗", "#f87171"),
                 "warn": ("⚠", "#fbbf24"), "info": ("ℹ", "#8b93a7"),
                 "unknown": ("?", "#8b93a7")}
        rows = []
        for c in result["checks"]:
            icon, color = icons.get(c["status"], ("•", "#8b93a7"))
            line = (f'<span style="color:{color};">{icon}</span> '
                    f'<b>{esc(c["label"])}:</b> needs {esc(c["required"])}'
                    f' &nbsp;·&nbsp; you: {esc(c["yours"])}')
            if c["note"]:
                line += f' <span style="color:#8b93a7;">— {esc(c["note"])}</span>'
            rows.append(line)
        if rows:
            detail = QLabel("<br>".join(rows))
            detail.setObjectName("SysCheckDetail")
            detail.setTextFormat(Qt.TextFormat.RichText)
            detail.setWordWrap(True)
            box.addWidget(detail)

        if any(c["status"] == "info" for c in result["checks"]):
            tip = QLabel("ℹ CPU and GPU are shown for you to compare — they "
                         "can't be auto-verified reliably.")
            tip.setObjectName("SysCheckTip")
            tip.setWordWrap(True)
            box.addWidget(tip)

        root.addWidget(frame)

    def _target_path(self) -> str:
        if self._patch_target:
            return self._patch_target
        base = self.dest_edit.text().strip()
        if CONFIG["default_install_subfolder"] or self._catalog_game.more_players:
            return os.path.join(base, self.game.safe_folder_name)
        return base

    def _change_edition(self):
        modified = self.edition_combo.currentData() == "more-players"
        self.game = self._catalog_game.more_players_game() if modified else self._catalog_game
        self.edition_note.setText(self.game.description if modified else "Original game download and its configured repair, if available.")
        for widget in (self._compat_badge, self._compat_reasoning):
            if widget is not None:
                widget.setVisible(not modified)
        if self._edition_meta is not None:
            details = ([f"Version {self.game.version}"] if self.game.version else [])
            if not modified and self.game.size:
                details.append(f"Size {self.game.size}")
            if not modified and self.game.players:
                details.append(f"👥 {self.game.players}")
            if self.game.has_fix:
                details.append("Includes repair patch")
            self._edition_meta.setText("   •   ".join(details))
        self.fix_only_btn.setVisible(self.game.has_fix)
        self.install_btn.setEnabled(self.game.is_available)
        self.install_btn.setText("Install game" if self.game.is_available else "Coming Soon")
        self._update_hint()

    def _update_hint(self):
        if self.dest_edit.text().strip():
            self.final_hint.setText(f"Will install to:  {self._target_path()}")
        else:
            self.final_hint.setText("")

    # -- actions ------------------------------------------------------------
    def _browse(self):
        start = self.dest_edit.text().strip() or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "Select install folder", start)
        if folder:
            self.dest_edit.setText(folder)

    def _start_install(self):
        if not self._patch_target:
            base = self.dest_edit.text().strip()
            if not base:
                QMessageBox.warning(self, "No destination",
                                    "Please choose a folder to install the game into.")
                return

        target = self._target_path()

        if self._catalog_game.more_players and self._manager is not None:
            target_key = os.path.normcase(os.path.abspath(target))
            for card in self._manager.cards:
                if card.state in ("queued", "running") and os.path.normcase(os.path.abspath(card.target)) == target_key:
                    QMessageBox.warning(self, "Already queued", "An installation is already using this folder.")
                    return
        if self._catalog_game.more_players and os.path.exists(target):
            try:
                occupied = not os.path.isdir(target) or bool(os.listdir(target))
            except OSError as exc:
                QMessageBox.warning(self, "Cannot inspect folder", str(exc))
                return
            if occupied:
                QMessageBox.warning(self, "Folder already exists",
                                    "This edition's install folder is not empty. Choose another install location to avoid overwriting existing files.")
                return

        # Hand the job to the download manager and close — it runs in the
        # background on the Downloads page (and applies the Defender exclusion
        # there if requested, just like the old inline pre-step did).
        wants_exclusion = bool(load_prefs().get("defender_exclusion_default", True))
        defender_folders = None
        if wants_exclusion:
            try:
                os.makedirs(target, exist_ok=True)
            except OSError as exc:
                QMessageBox.critical(
                    self, "Cannot create folder",
                    f"Could not create the install folder:\n{exc}",
                )
                return
            defender_folders = [target, tempfile.gettempdir()]

        # Remember the install location; exclusion preference is managed in Settings.
        prefs = load_prefs()
        if not self._patch_target:
            prefs["install_path"] = self.dest_edit.text().strip()
        save_prefs(prefs)

        self._install_target = target

        # When applying into an existing (already-owned) game folder, the base
        # game is already installed — only the fix needs to be downloaded and
        # applied. Downloading the main zipUrl here would fail for Steam-based
        # entries whose zipUrl is a steam:// link (not an HTTP archive).
        self._manager.enqueue(self.game, target,
                              fix_only=bool(self._patch_target),
                              defender_folders=defender_folders)
        self.accept()

    def _launch_install_worker(self, target: str, remove_temp_exclusion: bool = False):
        self.thread = QThread()
        self.worker = InstallWorker(self.game, target,
                                    remove_temp_exclusion=remove_temp_exclusion)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.download_progress.connect(
            lambda v: self._set_bar(self.dl_bar, v))
        self.worker.extract_progress.connect(
            lambda v: self._set_bar(self.ex_bar, v))
        self.worker.fix_download_progress.connect(
            lambda v: self._set_bar(self.fix_dl_bar, v))
        self.worker.fix_apply_progress.connect(
            lambda v: self._set_bar(self.fix_ap_bar, v))
        self.worker.status.connect(self.status_label.setText)
        self.worker.speed_text.connect(self.speed_label.setText)
        self.worker.eta_text.connect(self.eta_label.setText)
        self.worker.elapsed_text.connect(self.elapsed_label.setText)
        self.worker.overall_progress.connect(
            lambda v: self._set_bar(self.overall_bar, v))
        self.worker.success.connect(self._on_success)
        self.worker.error.connect(self._on_error)
        self.worker.canceled.connect(self._on_canceled)
        self.thread.start()

    # -- fix-only flow -------------------------------------------------------
    def _start_fix_only(self):
        base = self.dest_edit.text().strip()
        if not base:
            QMessageBox.warning(self, "No destination",
                                "Please enter the folder where the game is already installed.")
            return

        target = self._target_path()
        _log.debug(
            "_start_fix_only  id=%r  name=%r  fix_url=%r  target=%r",
            self.game.id, self.game.name, self.game.fix_url, target,
        )

        self._manager.enqueue(self.game, target, fix_only=True,
                              defender_folders=None)
        self.accept()

    def _launch_fix_only_worker(self, target: str):
        self.thread = QThread()
        self.worker = InstallWorker(self.game, target, fix_only=True)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.fix_download_progress.connect(
            lambda v: self._set_bar(self.fix_dl_bar, v))
        self.worker.fix_apply_progress.connect(
            lambda v: self._set_bar(self.fix_ap_bar, v))
        self.worker.status.connect(self.status_label.setText)
        self.worker.speed_text.connect(self.speed_label.setText)
        self.worker.eta_text.connect(self.eta_label.setText)
        self.worker.elapsed_text.connect(self.elapsed_label.setText)
        self.worker.overall_progress.connect(
            lambda v: self._set_bar(self.overall_bar, v))
        self.worker.success.connect(self._on_fix_only_success)
        self.worker.error.connect(self._on_error)
        self.worker.canceled.connect(self._on_canceled)
        self.thread.start()

    @pyqtSlot(str)
    def _on_fix_only_success(self, path):
        for bar in (self.fix_dl_bar, self.fix_ap_bar):
            bar.setRange(0, 100)
            bar.setValue(100)
        self._teardown_thread()
        self._reset_controls()
        self.status_label.setText("Fix applied successfully.")
        QMessageBox.information(
            self, "Fix applied",
            f"The repair patch for {self.game.name} was applied successfully.\n\n"
            f"Location:\n{path}",
        )

    def _on_cancel_clicked(self):
        if self._installing and self.worker is not None:
            self.status_label.setText("Cancelling…")
            self.cancel_btn.setEnabled(False)
            self.worker.cancel()
        else:
            self.reject()

    # -- worker slots (GUI thread) -----------------------------------------
    def _set_bar(self, bar, value):
        if value < 0:
            bar.setRange(0, 0)  # indeterminate / busy
        else:
            bar.setRange(0, 100)
            bar.setValue(value)

    def _teardown_thread(self):
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait()
            self.worker.deleteLater()
            self.thread.deleteLater()
            self.worker = None
            self.thread = None
        self._installing = False

    @pyqtSlot(str)
    def _on_success(self, path):
        for bar in (self.dl_bar, self.ex_bar):
            bar.setRange(0, 100)
            bar.setValue(100)
        if self.game.has_fix:
            for bar in (self.fix_dl_bar, self.fix_ap_bar):
                bar.setRange(0, 100)
                bar.setValue(100)
        self._teardown_thread()
        self._finish(path, None)

    # -- Defender exclusion (pre-install) -------------------------------------
    def _start_pre_defender(self, path: str):
        self.defender_thread = QThread()
        self.defender_worker = DefenderWorker([path, tempfile.gettempdir()])
        self.defender_worker.moveToThread(self.defender_thread)
        self.defender_thread.started.connect(self.defender_worker.run)
        self.defender_worker.finished.connect(self._on_pre_install_defender_done)
        self.defender_thread.start()

    def _teardown_defender_thread(self):
        if self.defender_thread is not None:
            self.defender_thread.quit()
            self.defender_thread.wait()
            self.defender_worker.deleteLater()
            self.defender_thread.deleteLater()
            self.defender_worker = None
            self.defender_thread = None

    @staticmethod
    def _temp_was_added(added: list) -> bool:
        """True when %TEMP% is among the exclusions this run created, i.e. when
        this install may remove it again on the way out."""
        temp = os.path.normcase(tempfile.gettempdir())
        return any(os.path.normcase(p) == temp for p in added)

    @pyqtSlot(bool, str, list)
    def _on_pre_install_defender_done(self, success: bool, message: str, added: list):
        self._teardown_defender_thread()
        if success:
            self.status_label.setText("Exclusion added. Starting download…")
            self._launch_install_worker(
                self._install_target,
                remove_temp_exclusion=self._temp_was_added(added),
            )
        else:
            self._installing = False
            self._reset_controls()
            QMessageBox.critical(
                self, "Defender exclusion failed",
                f"Could not add the Defender exclusion:\n{message}\n\n"
                "The installation was not started.",
            )

    def _finish(self, path: str, defender_result):
        """Show the final completion dialog and close the install dialog."""
        if not self._patch_target:
            prefs = load_prefs()
            prefs["install_path"] = self.dest_edit.text().strip()
            save_prefs(prefs)
        if self._patch_target:
            body = f"{self.game.name} fix was applied successfully.\n\nLocation:\n{path}"
        else:
            body = f"{self.game.name} was installed successfully.\n\nLocation:\n{path}"
        if defender_result is not None:
            ok, msg = defender_result
            icon = "✓" if ok else "⚠"
            body += f"\n\n{icon} Defender exclusion: {msg}"
        QMessageBox.information(self, "Installation complete", body)
        self.accept()

    # -------------------------------------------------------------------------
    @pyqtSlot(str)
    def _on_error(self, message):
        self._teardown_thread()
        self._reset_controls()
        self.status_label.setText("Installation failed.")
        QMessageBox.critical(self, "Installation failed", message)

    @pyqtSlot()
    def _on_canceled(self):
        self._teardown_thread()
        self._reset_controls()
        self.status_label.setText("Installation cancelled.")
        for bar in (self.dl_bar, self.ex_bar, self.fix_dl_bar, self.fix_ap_bar):
            bar.setRange(0, 100)
            bar.setValue(0)

    def _reset_controls(self):
        self.install_btn.setEnabled(self.game.is_available)
        self.fix_only_btn.setEnabled(True)  # visibility already gates usage
        self.browse_btn.setEnabled(True)
        self.dest_edit.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("Close")

    def closeEvent(self, event):
        if self._installing and self.worker is not None:
            reply = QMessageBox.question(
                self, "Cancel installation?",
                "An installation is in progress. Cancel it and close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.worker.cancel()
                self.thread.quit()
                self.thread.wait()
                event.accept()
            else:
                event.ignore()
        elif self.defender_thread is not None:
            # Defender elevated script is in progress — wait for it rather
            # than orphaning the elevated process mid-write.
            QMessageBox.information(
                self, "Please wait",
                "Adding the Defender exclusion, please wait a moment…",
            )
            event.ignore()
        else:
            event.accept()


# ---------------------------------------------------------------------------
# Fix Ownership dialog — shown before any "type": "fix" entry is installed.
# ---------------------------------------------------------------------------
class FixOwnershipDialog(QDialog):
    """Ask whether the user already has the base game installed.

    Results:
        RESULT_YES — user has the game → continue with the fix-apply flow
        RESULT_NO  — user doesn't    → launch the steam:// URL and return home
        exec() == 0 (dismissed)      → do nothing
    """

    RESULT_YES = 2
    RESULT_NO = 3

    def __init__(self, game: Game, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Install — {game.name}")
        self.setMinimumWidth(420)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        title = QLabel(game.name)
        title.setObjectName("DialogTitle")
        root.addWidget(title)

        msg = QLabel(f"Do you already have {game.name} installed?")
        msg.setObjectName("StatusLabel")
        msg.setWordWrap(True)
        root.addWidget(msg)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        no_btn = QPushButton("No")
        no_btn.clicked.connect(lambda: self.done(FixOwnershipDialog.RESULT_NO))
        btn_row.addWidget(no_btn)

        yes_btn = QPushButton("Yes")
        yes_btn.setObjectName("PrimaryButton")
        yes_btn.clicked.connect(lambda: self.done(FixOwnershipDialog.RESULT_YES))
        btn_row.addWidget(yes_btn)

        root.addLayout(btn_row)


# ---------------------------------------------------------------------------
# Concurrent downloads — manager + per-download status card + Downloads page.
# Each running card owns its own InstallWorker on its own QThread (same proven
# logic the install dialog used to run inline); the manager just limits how
# many run at once and starts queued ones as slots free up.
# ---------------------------------------------------------------------------
class DownloadCard(QFrame):
    """One download's full status row: overall + per-stage bars, speed/ETA,
    and a context action button (Cancel / Open folder / Remove)."""

    def __init__(self, game: Game, target: str, fix_only: bool,
                 defender_folders, manager):
        super().__init__()
        self.game = game
        self.target = target
        self.fix_only = fix_only
        self._defender_folders = defender_folders
        self.manager = manager
        self.state = "queued"  # queued | running | done | error | canceled
        self.thread = None
        self.worker = None
        self.defender_thread = None
        self.defender_worker = None
        self._installed_path = None
        self._main_done = False  # main game installed → a retry can be fix-only

        self.setObjectName("DownloadCard")
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(8)

        head = QHBoxLayout()
        name = QLabel(game.name)
        name.setObjectName("DownloadName")
        head.addWidget(name)
        if fix_only:
            tag = QLabel("fix only")
            tag.setObjectName("CardMeta")
            head.addWidget(tag)
        head.addStretch(1)
        self.chip = QLabel("Queued")
        self.chip.setObjectName("ChipQueued")
        head.addWidget(self.chip)
        self.retry_btn = QPushButton("Retry")
        self.retry_btn.clicked.connect(self._retry)
        self.retry_btn.setVisible(False)
        head.addWidget(self.retry_btn)
        self.action_btn = QPushButton("Cancel")
        self.action_btn.clicked.connect(self._action_clicked)
        head.addWidget(self.action_btn)
        root.addLayout(head)

        self.status_label = QLabel("Waiting to start…")
        self.status_label.setObjectName("StatusLabel")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        metrics = QHBoxLayout()
        self.speed_label = QLabel("")
        self.speed_label.setObjectName("MetricLabel")
        metrics.addWidget(self.speed_label)
        metrics.addStretch(1)
        self.elapsed_label = QLabel("")
        self.elapsed_label.setObjectName("MetricLabel")
        metrics.addWidget(self.elapsed_label)
        metrics.addSpacing(10)
        self.eta_label = QLabel("")
        self.eta_label.setObjectName("MetricLabel")
        metrics.addWidget(self.eta_label)
        root.addLayout(metrics)

        self.overall_bar = self._bar(root, "Overall")
        self.dl_lbl, self.dl_bar = self._bar(root, "Download", ret=True)
        self.ex_lbl, self.ex_bar = self._bar(root, "Extraction", ret=True)
        self.fix_dl_lbl, self.fix_dl_bar = self._bar(root, "Fix download", ret=True)
        self.fix_ap_lbl, self.fix_ap_bar = self._bar(root, "Applying fix", ret=True)
        if fix_only:
            for w in (self.dl_lbl, self.dl_bar, self.ex_lbl, self.ex_bar):
                w.setVisible(False)
        elif not game.has_fix:
            for w in (self.fix_dl_lbl, self.fix_dl_bar, self.fix_ap_lbl, self.fix_ap_bar):
                w.setVisible(False)

    # -- ui helpers ---------------------------------------------------------
    def _bar(self, layout, caption, ret=False):
        lbl = QLabel(caption)
        lbl.setObjectName("SmallLabel")
        layout.addWidget(lbl)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        layout.addWidget(bar)
        if ret:
            return lbl, bar
        # caller (overall) doesn't keep the caption label
        return bar

    def _set_bar(self, bar, value):
        if value < 0:
            bar.setRange(0, 0)  # indeterminate / busy
        else:
            bar.setRange(0, 100)
            bar.setValue(value)

    def _set_chip(self, text, kind):
        self.chip.setText(text)
        self.chip.setObjectName(f"Chip{kind}")
        self.chip.style().unpolish(self.chip)
        self.chip.style().polish(self.chip)

    # -- lifecycle (manager-driven) -----------------------------------------
    def start(self):
        """Begin this download. Called by the manager when a slot is free."""
        self.state = "running"
        self.retry_btn.setVisible(False)
        self._set_chip("Downloading", "Running")
        self.action_btn.setText("Cancel")
        self.action_btn.setEnabled(True)
        if self._defender_folders:
            self._start_defender()
        else:
            self._start_worker(remove_temp_exclusion=False)

    def _start_defender(self):
        self.status_label.setText("Adding Defender exclusion…")
        self.action_btn.setEnabled(False)
        self.defender_thread = QThread()
        self.defender_worker = DefenderWorker(self._defender_folders)
        self.defender_worker.moveToThread(self.defender_thread)
        self.defender_thread.started.connect(self.defender_worker.run)
        self.defender_worker.finished.connect(self._on_defender_done)
        self.defender_thread.start()

    @pyqtSlot(bool, str, list)
    def _on_defender_done(self, ok, message, added):
        self._teardown_defender()
        self.action_btn.setEnabled(True)
        if not ok:
            self._fail(f"Defender exclusion failed: {message}")
            return
        self.status_label.setText("Exclusion added. Starting download…")
        # Only remove %TEMP% afterwards if this run is what added it — a
        # pre-existing entry (possibly created by another concurrent card or
        # by the user) must survive.
        temp = os.path.normcase(tempfile.gettempdir())
        self._start_worker(
            remove_temp_exclusion=any(os.path.normcase(p) == temp for p in added)
        )

    def _start_worker(self, remove_temp_exclusion):
        # If a previous attempt already installed the main game, retry the fix
        # alone — the installed files persist, so there's nothing to re-download.
        effective_fix_only = self.fix_only or self._main_done
        self.thread = QThread()
        self.worker = InstallWorker(self.game, self.target, fix_only=effective_fix_only,
                                    remove_temp_exclusion=remove_temp_exclusion)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.main_installed.connect(self._on_main_installed)
        self.worker.download_progress.connect(lambda v: self._set_bar(self.dl_bar, v))
        self.worker.extract_progress.connect(lambda v: self._set_bar(self.ex_bar, v))
        self.worker.fix_download_progress.connect(lambda v: self._set_bar(self.fix_dl_bar, v))
        self.worker.fix_apply_progress.connect(lambda v: self._set_bar(self.fix_ap_bar, v))
        self.worker.status.connect(self.status_label.setText)
        self.worker.speed_text.connect(self.speed_label.setText)
        self.worker.eta_text.connect(self.eta_label.setText)
        self.worker.elapsed_text.connect(self.elapsed_label.setText)
        self.worker.overall_progress.connect(lambda v: self._set_bar(self.overall_bar, v))
        self.worker.success.connect(self._on_success)
        self.worker.error.connect(self._on_error)
        self.worker.canceled.connect(self._on_canceled)
        self.thread.start()

    def _teardown_thread(self):
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait()
            self.worker.deleteLater()
            self.thread.deleteLater()
            self.worker = None
            self.thread = None

    def _teardown_defender(self):
        if self.defender_thread is not None:
            self.defender_thread.quit()
            self.defender_thread.wait()
            self.defender_worker.deleteLater()
            self.defender_thread.deleteLater()
            self.defender_worker = None
            self.defender_thread = None

    # -- worker slots -------------------------------------------------------
    @pyqtSlot()
    def _on_main_installed(self):
        """Main game landed on disk. Remember it so a later failure retries the
        fix alone rather than re-downloading and re-extracting the game."""
        self._main_done = True
        _save_resume_job(self.game, self.target, self.fix_only, main_done=True)

    @pyqtSlot(str)
    def _on_success(self, path):
        self._installed_path = path
        self._set_bar(self.overall_bar, 100)
        if not self.fix_only:
            self._set_bar(self.dl_bar, 100)
            self._set_bar(self.ex_bar, 100)
        if self.game.has_fix or self.fix_only:
            self._set_bar(self.fix_dl_bar, 100)
            self._set_bar(self.fix_ap_bar, 100)
        self._teardown_thread()
        self.state = "done"
        _remove_resume_job(self.game, self.target)
        self._set_chip("Done ✓", "Done")
        self.status_label.setText(f"Installed to {path}")
        self.speed_label.setText("")
        self.eta_label.setText("")
        self.action_btn.setText("Open folder")
        self.action_btn.setEnabled(True)
        self._maybe_create_shortcut(path)
        self.manager.on_state_changed()

    def _maybe_create_shortcut(self, install_dir):
        """If the user enabled it in Settings, drop a desktop shortcut to the
        installed game's main executable. Best-effort — never breaks the
        install if it fails."""
        if not load_prefs().get("create_desktop_shortcut", False):
            return
        try:
            exe = game_launch_executable(install_dir, self.game)
            if not exe:
                _log.debug("shortcut: no .exe found under %s", install_dir)
                return
            lnk = create_desktop_shortcut(exe, self.game.name)
            if lnk:
                _log.debug("shortcut created  %s  ->  %s", lnk, exe)
                self.status_label.setText(
                    f"Installed to {install_dir}  ·  Desktop shortcut created")
            else:
                _log.debug("shortcut: creation failed for %s", exe)
        except Exception as exc:  # never let a shortcut failure break success
            _log.debug("shortcut: unexpected error  %s", exc)

    @pyqtSlot(str)
    def _on_error(self, message):
        self._teardown_thread()
        self._fail(message)

    @pyqtSlot()
    def _on_canceled(self):
        self._teardown_thread()
        self.state = "canceled"
        self._set_chip("Canceled", "Canceled")
        self.status_label.setText("Download canceled.")
        self.speed_label.setText("")
        self.eta_label.setText("")
        # The partial download is kept, so a resume is possible.
        self.retry_btn.setVisible(True)
        self.retry_btn.setEnabled(True)
        self.action_btn.setText("Remove")
        self.action_btn.setEnabled(True)
        self.manager.on_state_changed()

    def _fail(self, message):
        self.state = "error"
        self._set_chip("Failed", "Error")
        self.status_label.setText(message)
        self.speed_label.setText("")
        self.eta_label.setText("")
        # Offer a resume: partial downloads were kept, so Retry continues from
        # where it stopped rather than starting over.
        self.retry_btn.setVisible(True)
        self.retry_btn.setEnabled(True)
        self.action_btn.setText("Remove")
        self.action_btn.setEnabled(True)
        self.manager.on_state_changed()

    def _retry(self):
        """Re-queue a failed download. Kept partials resume automatically; if the
        main game was already installed, only the fix is retried."""
        if self.state not in ("error", "canceled", "paused"):
            return
        self.retry_btn.setText("Retry")
        self.retry_btn.setVisible(False)
        self._set_chip("Queued", "Queued")
        self.status_label.setText("Waiting to retry…")
        self.speed_label.setText("")
        self.eta_label.setText("")
        self.elapsed_label.setText("")
        # Completed stages stay full; only the unfinished ones reset to 0.
        self._set_bar(self.overall_bar, 0)
        if self._main_done:
            self._set_bar(self.dl_bar, 100)
            self._set_bar(self.ex_bar, 100)
            self._set_bar(self.fix_dl_bar, 0)
            self._set_bar(self.fix_ap_bar, 0)
        else:
            for bar in (self.dl_bar, self.ex_bar, self.fix_dl_bar, self.fix_ap_bar):
                self._set_bar(bar, 0)
        self.action_btn.setText("Cancel")
        self.action_btn.setEnabled(True)
        self.state = "queued"
        self.manager.on_state_changed()

    # -- the context-sensitive button --------------------------------------
    def _action_clicked(self):
        if self.state == "queued":
            self.state = "canceled"
            self._set_chip("Canceled", "Canceled")
            self.status_label.setText("Canceled before starting.")
            self.action_btn.setText("Remove")
            self.retry_btn.setVisible(True)
            self.manager.on_state_changed()
        elif self.state == "running":
            if self.worker is not None:
                self.status_label.setText("Canceling…")
                self.action_btn.setEnabled(False)
                self.worker.cancel()
            # else: mid-Defender elevation — cannot interrupt cleanly, ignore
        elif self.state == "done":
            if self._installed_path and os.path.isdir(self._installed_path):
                try:
                    os.startfile(self._installed_path)
                except OSError:
                    pass
        else:  # error / canceled
            self.manager.remove_card(self)

    def force_stop(self):
        """Used on app shutdown so no QThread is left running."""
        if self.worker is not None:
            self.worker.cancel()
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait(3000)
        if self.defender_thread is not None:
            self.defender_thread.quit()
            self.defender_thread.wait(3000)


class DownloadManager(QObject):
    """Runs at most *max_concurrent* downloads at once; the rest wait queued
    and start automatically as running ones finish."""
    count_changed = pyqtSignal(int)  # number of active (queued + running) jobs

    def __init__(self, page, max_concurrent=3):
        super().__init__()
        self.page = page
        self.max_concurrent = max_concurrent
        self.cards = []
        page.clear_btn.clicked.connect(self.clear_finished)

    def enqueue(self, game, target, fix_only=False, defender_folders=None):
        if any(c.game.id == game.id and os.path.normcase(os.path.abspath(c.target)) == os.path.normcase(os.path.abspath(target))
               and c.state != 'done' for c in self.cards):
            return
        _save_resume_job(game, target, fix_only)
        card = DownloadCard(game, target, fix_only, defender_folders, self)
        self.cards.append(card)
        self.page.add_card(card)
        self.page.set_empty(False)
        _log.debug("DownloadManager.enqueue  id=%r  fix_only=%s  target=%r",
                   game.id, fix_only, target)
        self._pump()

    def restore_interrupted(self, games):
        """Offer explicit resume; do not start downloads when the app opens."""
        lookup = {g.id: g for g in games}
        for game in games:
            if game.more_players:
                variant = game.more_players_game()
                lookup[variant.id] = variant
        records = []
        directory = os.path.join(_prefs_dir(), 'download-jobs')
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_file() and entry.name.endswith('.json'):
                        try:
                            with open(entry.path, encoding='utf-8') as fh:
                                record = json.load(fh)
                            if isinstance(record, dict):
                                records.append(record)
                        except (OSError, ValueError):
                            _log.warning('Invalid download resume record: %s', entry.path)
        except FileNotFoundError:
            pass
        except OSError:
            _log.exception('Unable to read download resume records')
        # Older app versions kept the archives but did not save job metadata.
        base = default_game_directory()
        if base:
            for game in lookup.values():
                target = os.path.join(base, game.safe_folder_name)
                if _legacy_cache_exists(game, target):
                    records.append({'id': game.id, 'target': target,
                                    'identity': _download_identity(game)})
        for record in records:
            game = lookup.get(record.get('id'))
            target = record.get('target')
            if not game or not isinstance(target, str) or not os.path.isabs(target):
                continue
            if any(c.game.id == game.id and os.path.normcase(os.path.abspath(c.target)) == os.path.normcase(os.path.abspath(target)) for c in self.cards):
                continue
            card = DownloadCard(game, target, bool(record.get('fix_only')), None, self)
            card._main_done = bool(record.get('main_done')) and os.path.isdir(target)
            card.state = 'paused'
            card._set_chip('Interrupted', 'Canceled')
            card.action_btn.setText('Remove')
            card.retry_btn.setText('Resume')
            card.retry_btn.setVisible(True)
            if record.get('identity') != _download_identity(game):
                card.status_label.setText('The catalog download links changed. The old cache was kept; start a new download to use the current links.')
                card.retry_btn.setEnabled(False)
            else:
                card.status_label.setText('Interrupted download found. Resume verifies cached files and continues where supported.')
                _save_resume_job(game, target, card.fix_only, card._main_done)
            self.cards.append(card)
            self.page.add_card(card)
            self.page.set_empty(False)

    def on_state_changed(self):
        """A card finished/canceled — start the next queued one and refresh."""
        self._pump()

    def _pump(self):
        running = sum(1 for c in self.cards if c.state == "running")
        for c in self.cards:
            if running >= self.max_concurrent:
                break
            if c.state == "queued":
                c.start()
                running += 1
        active = sum(1 for c in self.cards if c.state in ("queued", "running"))
        self.count_changed.emit(active)

    def remove_card(self, card):
        _remove_resume_job(card.game, card.target)
        if card in self.cards:
            self.cards.remove(card)
        self.page.remove_card(card)
        self.page.set_empty(len(self.cards) == 0)
        self._pump()

    def clear_finished(self):
        for c in list(self.cards):
            if c.state in ("done", "error", "canceled"):
                self.remove_card(c)

    def shutdown(self):
        for c in self.cards:
            c.force_stop()


class DownloadsPage(QWidget):
    """Scrollable list of DownloadCards with a header + 'Clear finished'."""

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        bar = QFrame()
        bar.setObjectName("DownloadsBar")
        brow = QHBoxLayout(bar)
        brow.setContentsMargins(24, 24, 24, 18)
        headings = QVBoxLayout()
        headings.setSpacing(4)
        title = QLabel("Downloads")
        title.setObjectName("LibraryHeading")
        headings.addWidget(title)
        subtitle = QLabel("Track your installs and manage finished downloads.")
        subtitle.setObjectName("UpdatedLabel")
        headings.addWidget(subtitle)
        brow.addLayout(headings)
        brow.addStretch(1)
        self.clear_btn = QPushButton("Clear finished")
        self.clear_btn.setObjectName("SpacewarButton")
        brow.addWidget(self.clear_btn)
        lay.addWidget(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setObjectName("Scroll")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        self.list_layout = QVBoxLayout(container)
        self.list_layout.setContentsMargins(24, 18, 24, 18)
        self.list_layout.setSpacing(14)
        self.empty_label = QLabel("No downloads yet\nPick a game from the library to start an install.")
        self.empty_label.setObjectName("SubInfo")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.list_layout.addWidget(self.empty_label)
        self.list_layout.addStretch(1)
        scroll.setWidget(container)
        lay.addWidget(scroll, 1)

    def add_card(self, card):
        # insert just before the trailing stretch so cards stay top-aligned
        self.list_layout.insertWidget(self.list_layout.count() - 1, card)

    def remove_card(self, card):
        card.setParent(None)
        card.deleteLater()

    def set_empty(self, is_empty):
        self.empty_label.setVisible(is_empty)
        self.clear_btn.setEnabled(not is_empty)


# ---------------------------------------------------------------------------
# Self-update dialog
# ---------------------------------------------------------------------------
class UpdateDialog(QDialog):
    def __init__(self, update: UpdateRelease, parent=None, install_safe=True):
        super().__init__(parent)
        self.update = update
        self._thread = None
        self._worker = None
        self._new_exe: str | None = None

        self.setWindowTitle("Update available")
        self.setMinimumWidth(420)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        title = QLabel("An update is available")
        title.setObjectName("DialogTitle")
        root.addWidget(title)

        versions = QLabel(f"Installed: {VERSION}\nAvailable: {update.tag}")
        versions.setObjectName("CardMeta")
        versions.setWordWrap(True)
        root.addWidget(versions)

        date_text = update.build_time or update.build_date
        if date_text:
            date_label = QLabel(f"Release prepared: {date_text}" if update.build_time
                                else f"Build date: {date_text}")
            date_label.setObjectName("CardMeta")
            root.addWidget(date_label)

        changes = QLabel(update.changes_for(VERSION))
        changes.setTextFormat(Qt.TextFormat.PlainText)
        changes.setObjectName("CardDesc")
        changes.setWordWrap(True)
        changes_scroll = QScrollArea()
        changes_scroll.setWidgetResizable(True)
        changes_scroll.setMaximumHeight(180)
        changes_scroll.setMinimumHeight(65)
        changes_scroll.setWidget(changes)
        root.addWidget(changes_scroll)

        if update.self_updatable and not install_safe:
            sub_text = "Pause or cancel active downloads first. The app can only replace its files after its game-download jobs stop."
        elif update.self_updatable:
            sub_text = "A verified Windows x64 download will replace the running app after it closes."
        else:
            sub_text = update.manual_reason or "This update must be installed manually from the official release page."
        sub = QLabel(sub_text)
        sub.setObjectName("CardDesc")
        sub.setWordWrap(True)
        root.addWidget(sub)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setObjectName("CardMeta")
        self.status_label.setVisible(False)
        root.addWidget(self.status_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.later_btn = QPushButton("Later")
        self.later_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.later_btn)
        self.update_btn = QPushButton("Update" if update.self_updatable else "Open release page")
        self.update_btn.setObjectName("PrimaryButton")
        self.update_btn.setEnabled(install_safe if update.self_updatable else bool(update.release_url))
        self.update_btn.clicked.connect(self._start_download if update.self_updatable else self._open_release_page)
        btn_row.addWidget(self.update_btn)
        root.addLayout(btn_row)

    def _start_download(self):
        if not self.update.self_updatable:
            return
        self.update_btn.setEnabled(False)
        self.later_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.status_label.setText("Downloading update…")
        self.status_label.setVisible(True)

        self._thread = QThread()
        self._worker = UpdateDownloadWorker(self.update.download_url, self.update.sha256)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.success.connect(self._on_success)
        self._worker.error.connect(self._on_error)
        self._thread.start()

    def _teardown_thread(self):
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
            self._worker.deleteLater()
            self._thread.deleteLater()
            self._worker = None
            self._thread = None

    @pyqtSlot(int)
    def _on_progress(self, value):
        if value < 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(value)

    @pyqtSlot(str)
    def _on_success(self, path):
        self._teardown_thread()
        self._new_exe = path
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.status_label.setText("Download verified. Closing the app to install the update and restart…")
        QTimer.singleShot(800, self._apply_update)

    @pyqtSlot(str)
    def _on_error(self, message):
        # Step 7 (dialog layer) — download failed signal received by the dialog.
        _ulog.error("[7] UpdateDialog received download error: %s", message)
        self._teardown_thread()
        self.status_label.setText(f"Update failed: {message}")
        self.update_btn.setEnabled(True)
        self.update_btn.setText("Retry")
        self.update_btn.clicked.disconnect()
        self.update_btn.clicked.connect(self._start_download)
        self.later_btn.setEnabled(True)
        self.later_btn.setText("Later")

    def _open_release_page(self):
        if self.update.release_url:
            webbrowser.open(self.update.release_url)
        self.reject()

    def _apply_update(self):
        if not self._new_exe or not os.path.isfile(self._new_exe):
            _ulog.error(
                "[8] Replacement step skipped — downloaded file missing: %r",
                self._new_exe,
            )
            self.status_label.setText("Update file missing. Please restart manually.")
            self._show_apply_error("The staged update file is missing. Try the download again.")
            return
        if not getattr(sys, "frozen", False):
            _ulog.warning(
                "[8] Replacement step skipped — running from source (not a frozen exe)."
            )
            self.status_label.setText(
                "Self-update is only supported for the .exe build."
            )
            self.update_btn.setEnabled(True)
            self.later_btn.setEnabled(True)
            return
        # Step 8 — replacement step: log old path and new path.
        launcher_path = os.environ.get("GAMEINSTALLER_LAUNCHER", sys.executable)
        _ulog.info(
            "[8] Replacement step running  new_exe=%r  current_exe=%r  launcher=%r",
            self._new_exe, sys.executable, launcher_path,
        )
        try:
            _launch_update_bat(self._new_exe, launcher_path)
        except Exception as exc:
            # Step 9 — exception during replacement.
            _ulog.exception("[9] Exception during _launch_update_bat — full traceback:")
            self._show_apply_error(f"Could not start the updater: {exc}")
            return
        QApplication.quit()

    def _show_apply_error(self, message):
        self.status_label.setText(message)
        self.status_label.setVisible(True)
        self.update_btn.setEnabled(True)
        self.update_btn.setText("Open release page")
        self.update_btn.clicked.disconnect()
        self.update_btn.clicked.connect(self._open_release_page)
        self.later_btn.setEnabled(True)

    def closeEvent(self, event):
        # Block the close button while a download is in progress.
        if self._thread is not None:
            event.ignore()
        else:
            event.accept()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(CONFIG["window_title"])
        self.setMinimumSize(900, 600)

        self.manifest_thread = None
        self.manifest_worker = None
        self._manifest_background_refresh = False
        self._manual_catalog_refresh_requested = False
        self._catalog_refresh_timer = None
        self._spacewar_check_done = False  # fires at most once per session
        self._update_thread = None
        self._update_worker = None
        self._update_notice = UpdateNoticeState()
        self._manual_update_check = False
        self._update_restart_status = ""
        self._sweep_thread = None
        self._sweep_worker = None
        self._current_games: list = []
        saved_favorites = load_prefs().get("favorite_game_ids", [])
        self._favorite_game_ids = (
            {game_id for game_id in saved_favorites if isinstance(game_id, str)}
            if isinstance(saved_favorites, list) else set()
        )

        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_header())
        self._update_restart_status = consume_update_status()
        if self._update_restart_status == "installed":
            self.check_updates_btn.setToolTip("The update was installed and Game Installer restarted successfully.")
        elif self._update_restart_status == "failed":
            self.check_updates_btn.setToolTip("The update could not be applied; the previous app was reopened. Click to retry or open the release page.")

        self.stack = AnimatedStack()
        outer.addWidget(self.stack, 1)
        self.loading_page = self._build_loading_page()
        self.error_page = self._build_error_page()
        self.content_page = self._build_content_page()
        self.settings_page = self._build_settings_page()
        self.downloads_page = DownloadsPage()
        self.download_manager = DownloadManager(self.downloads_page, max_concurrent=3)
        self.download_manager.count_changed.connect(self._update_downloads_badge)
        self.downloads_page.set_empty(True)
        self.stack.addWidget(self.loading_page)
        self.stack.addWidget(self.error_page)
        self.stack.addWidget(self.content_page)
        self.stack.addWidget(self.settings_page)
        self.stack.addWidget(self.downloads_page)
        self.stack.currentChanged.connect(self._sync_navigation)

        self.load_manifest()
        self._catalog_refresh_timer = QTimer(self)
        self._catalog_refresh_timer.setInterval(CATALOG_AUTO_REFRESH_INTERVAL_MS)
        self._catalog_refresh_timer.timeout.connect(self._refresh_catalog_automatically)
        self._catalog_refresh_timer.start()
        # Let the first window paint, then check without holding up the GUI.
        schedule_background_check(QTimer.singleShot, self._start_update_check)
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(120_000)
        self._update_timer.timeout.connect(self._start_update_check)
        self._update_timer.start()

    def closeEvent(self, event):
        # Stop any in-flight downloads so no QThread is left running at exit.
        try:
            self.download_manager.shutdown()
        except Exception:
            pass
        if self._update_thread is not None:
            self._update_thread.quit()
            self._update_thread.wait()
        if self._sweep_thread is not None:
            if self._sweep_worker is not None:
                self._sweep_worker.cancel()
            self._sweep_thread.quit()
            self._sweep_thread.wait(3000)
        super().closeEvent(event)

    # -- self-update ----------------------------------------------------------
    def _start_update_check(self):
        if self._update_thread is not None:
            return
        self._update_thread = QThread()
        self._update_worker = UpdateChecker()
        self._update_worker.moveToThread(self._update_thread)
        self._update_thread.started.connect(self._update_worker.run)
        self._update_worker.finished.connect(self._update_thread.quit)
        self._update_thread.finished.connect(self._update_check_finished)
        self._update_worker.result.connect(self._on_update_check_result)
        self._update_thread.start()

    def _update_check_finished(self):
        if self._update_thread is not None:
            self._update_worker.deleteLater()
            self._update_thread.deleteLater()
            self._update_worker = None
            self._update_thread = None

    @pyqtSlot(object, str)
    def _on_update_check_result(self, update, error):
        if error:
            self.check_updates_btn.setToolTip(f"Update check failed: {error}")
        else:
            self._update_notice.release = update
            if update is None:
                if self._update_restart_status == "installed":
                    self.check_updates_btn.setToolTip("The update was installed and Game Installer restarted successfully.")
                elif self._update_restart_status == "failed":
                    self.check_updates_btn.setToolTip("The update could not be applied; the previous app was reopened. Click to retry or open the release page.")
                else:
                    self.check_updates_btn.setToolTip("No compatible update is available.")
            else:
                self._update_notice.offer(update)
                self.check_updates_btn.setToolTip(f"Open details for Game Installer {update.tag}")
            self._refresh_update_button()
        if self._manual_update_check and not error and update is None:
            self.check_updates_btn.setToolTip("You're up to date for this Windows x64 build.")
        self._manual_update_check = False

    def _update_button_clicked(self):
        update = self._update_notice.open_details()
        if update is None:
            self._manual_update_check = True
            self.check_updates_btn.setToolTip("Checking GitHub releases…")
            self._start_update_check()
            return
        try:
            install_safe = not any(card.state in ("queued", "running")
                                   for card in self.download_manager.cards)
            UpdateDialog(update, parent=self, install_safe=install_safe).exec()
        finally:
            # Dismissing details does not discard the release or its indicator.
            self._update_notice.later()

    def _refresh_update_button(self):
        update = self._update_notice.release
        if update is None:
            self.check_updates_btn.setText("Updates")
            self.check_updates_btn.setObjectName("NavButton")
        else:
            self.check_updates_btn.setText("Update available")
            self.check_updates_btn.setObjectName("UpdateAvailable")
        self.check_updates_btn.style().unpolish(self.check_updates_btn)
        self.check_updates_btn.style().polish(self.check_updates_btn)
        self.check_updates_btn.update()

    def _update_downloads_badge(self, active):
        self.downloads_btn.setText("Downloads" if active == 0 else f"Downloads ({active})")

    # -- header -------------------------------------------------------------
    def _build_header(self):
        header = QFrame()
        header.setObjectName("Header")
        header.setFixedWidth(210)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(16, 24, 16, 20)
        header_layout.setSpacing(10)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel(CONFIG["window_title"])
        title.setObjectName("AppTitle")
        title_box.addWidget(title)
        self.updated_label = QLabel("")
        self.updated_label.setObjectName("UpdatedLabel")
        self.updated_label.setWordWrap(True)
        title_box.addWidget(self.updated_label)
        header_layout.addLayout(title_box)
        header_layout.addSpacing(16)
        lay = header_layout

        self.library_btn = QPushButton("Library")
        self.library_btn.setObjectName("NavButton")
        self.library_btn.clicked.connect(
            self._show_library)
        lay.addWidget(self.library_btn)

        self.owned_btn = QPushButton("Owned")
        self.owned_btn.setObjectName("NavButton")
        self.owned_btn.clicked.connect(self._show_owned_games)
        lay.addWidget(self.owned_btn)

        self.downloads_btn = QPushButton("Downloads")
        self.downloads_btn.setObjectName("NavButton")
        self.downloads_btn.clicked.connect(
            lambda: self.stack.setCurrentWidget(self.downloads_page))
        lay.addWidget(self.downloads_btn)

        self.settings_btn = QPushButton("Settings")
        self.settings_btn.setObjectName("NavButton")
        self.settings_btn.clicked.connect(
            lambda: self.stack.setCurrentWidget(self.settings_page))
        lay.addWidget(self.settings_btn)

        self.check_updates_btn = QPushButton("Updates")
        self.check_updates_btn.setObjectName("NavButton")
        self.check_updates_btn.clicked.connect(self._update_button_clicked)
        lay.addStretch(1)
        lay.addWidget(self.check_updates_btn)

        self.spacewar_btn = QPushButton("Get Spacewar")
        self.spacewar_btn.setObjectName("SpacewarButton")
        self.spacewar_btn.setToolTip(
            "Install Spacewar (app 480) via Steam — enables online multiplayer "
            "for some games in this library"
        )
        self.spacewar_btn.clicked.connect(self._launch_spacewar_manual)
        lay.addWidget(self.spacewar_btn)

        self.launch_steam_btn = QPushButton("Launch Steam")
        self.launch_steam_btn.setObjectName("NavButton")
        self.launch_steam_btn.setToolTip("Open the Steam client")
        self.launch_steam_btn.clicked.connect(self._launch_steam)
        lay.addWidget(self.launch_steam_btn)

        self.refresh_btn = QPushButton("↻  Refresh")
        self.refresh_btn.setObjectName("PrimaryButton")
        self.refresh_btn.clicked.connect(lambda: self.load_manifest(manual=True))
        lay.addWidget(self.refresh_btn)
        return header

    def _sync_navigation(self, *_args):
        page = self.stack.currentWidget()
        owned = page is self.content_page and self._library_filter.currentText() == "Installed games"
        if page is self.content_page:
            self._library_heading.setText("Your games" if owned else "Your library")
            self._library_subtitle.setText("Play and manage games in your selected folder." if owned else "Find, install, and manage your games.")
        for button, selected in ((self.library_btn, page is self.content_page and not owned),
                                 (self.owned_btn, owned),
                                 (self.downloads_btn, page is self.downloads_page),
                                 (self.settings_btn, page is self.settings_page)):
            button.setProperty("active", selected)
            button.style().unpolish(button)
            button.style().polish(button)

    # -- pages --------------------------------------------------------------
    def _build_loading_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addStretch(1)
        lbl = QLabel("Loading games…")
        lbl.setObjectName("BigInfo")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(lbl)
        sub = QLabel("Fetching the latest library for you.")
        sub.setObjectName("SubInfo")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(sub)
        lay.addStretch(1)
        return page

    def _build_error_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addStretch(1)
        icon = QLabel("⚠")
        icon.setObjectName("BigInfo")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(icon)
        self.error_label = QLabel("Something went wrong.")
        self.error_label.setObjectName("SubInfo")
        self.error_label.setWordWrap(True)
        self.error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.error_label)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        retry = QPushButton("Retry")
        retry.setObjectName("PrimaryButton")
        retry.clicked.connect(lambda: self.load_manifest(manual=True))
        btn_row.addWidget(retry)
        btn_row.addStretch(1)
        lay.addSpacing(10)
        lay.addLayout(btn_row)
        lay.addStretch(1)
        return page

    def _build_content_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        intro = QFrame()
        intro.setObjectName("LibraryIntro")
        intro_layout = QVBoxLayout(intro)
        intro_layout.setContentsMargins(24, 24, 24, 4)
        intro_layout.setSpacing(4)
        self._library_heading = QLabel("Your library")
        self._library_heading.setObjectName("LibraryHeading")
        intro_layout.addWidget(self._library_heading)
        self._library_subtitle = QLabel("Find, install, and manage your games.")
        self._library_subtitle.setObjectName("UpdatedLabel")
        intro_layout.addWidget(self._library_subtitle)
        lay.addWidget(intro)

        search_row = QWidget()
        sr_lay = QHBoxLayout(search_row)
        sr_lay.setContentsMargins(24, 14, 24, 6)
        self._search_bar = QLineEdit()
        self._search_bar.setPlaceholderText("Search games...")
        self._search_bar.setClearButtonEnabled(True)
        self._search_bar.textChanged.connect(self._on_search_changed)
        sr_lay.addWidget(self._search_bar)
        lay.addWidget(search_row)

        controls_row = QWidget()
        controls_lay = QHBoxLayout(controls_row)
        controls_lay.setContentsMargins(24, 0, 24, 10)
        controls_lay.setSpacing(8)
        self._library_filter = QComboBox()
        self._library_filter.addItems(["All games", "Installed games", "Favorites", "Name A–Z", "Name Z–A", "Newest first", "Installed first", "Size: smallest first", "Size: largest first", "Players: fewest first", "Players: most first"])
        self._library_filter.setCurrentText("Newest first")
        self._library_filter.currentIndexChanged.connect(lambda _: self._on_search_changed(self._search_bar.text()))
        self._library_filter.currentIndexChanged.connect(self._sync_navigation)
        controls_lay.addWidget(self._library_filter)
        controls_lay.addStretch(1)
        self._games_folder_btn = QPushButton("Games folder…")
        self._games_folder_btn.clicked.connect(self._choose_games_folder)
        controls_lay.addWidget(self._games_folder_btn)
        self._move_games_btn = QPushButton("Move games…")
        self._move_games_btn.clicked.connect(self._move_games_folder)
        controls_lay.addWidget(self._move_games_btn)
        lay.addWidget(controls_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setObjectName("Scroll")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        self.cards_layout = FlowLayout(container, margin=24, spacing=18)
        scroll.setWidget(container)
        scroll.verticalScrollBar().valueChanged.connect(lambda _value: self._refresh_card_hover_after_scroll(scroll))
        lay.addWidget(scroll)
        return page

    def _refresh_card_hover_after_scroll(self, scroll):
        cursor = QCursor.pos()
        in_view = scroll.viewport().rect().contains(scroll.viewport().mapFromGlobal(cursor))
        for index in range(self.cards_layout.count()):
            card = self.cards_layout.itemAt(index).widget()
            if not isinstance(card, GameCard):
                continue
            hovered = in_view and card.rect().contains(card.mapFromGlobal(cursor))
            card._set_hovered(hovered)
        scroll.viewport().update()

    def _build_settings_page(self):
        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setObjectName("Scroll")
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)

        title = QLabel("Settings")
        title.setObjectName("LibraryHeading")
        lay.addWidget(title)

        subtitle = QLabel("Manage your catalog, installs, and shortcuts.")
        subtitle.setObjectName("UpdatedLabel")
        lay.addWidget(subtitle)
        lay.addSpacing(12)

        catalog_title = QLabel("Game catalog")
        catalog_title.setObjectName("SectionTitle")
        lay.addWidget(catalog_title)
        self.catalog_source_combo = QComboBox()
        self.catalog_source_combo.addItem("Game-Downloader online catalog", "url")
        self.catalog_source_combo.addItem("Pastebin or other HTTPS link", "url")
        self.catalog_source_combo.addItem("Local games.json file", "local")
        self.catalog_source_combo.addItem("Paste or edit JSON", "paste")
        self.catalog_source_combo.addItem("Saved catalog (offline)", "cached")
        catalog_prefs = load_prefs()
        selected_mode = catalog_prefs.get("catalog_mode", "url")
        # The two URL choices share behavior; the URL itself distinguishes them.
        if selected_mode == "url" and catalog_prefs.get("catalog_url", CONFIG["manifest_url"]) != CONFIG["manifest_url"]:
            self.catalog_source_combo.setCurrentIndex(1)
        else:
            for index in range(self.catalog_source_combo.count()):
                if self.catalog_source_combo.itemData(index) == selected_mode:
                    self.catalog_source_combo.setCurrentIndex(index)
                    break
        self.catalog_source_combo.currentIndexChanged.connect(self._on_catalog_source_changed)
        self.catalog_source_combo.setMaximumWidth(850)
        lay.addWidget(self.catalog_source_combo)

        self.catalog_url_edit = QLineEdit(catalog_prefs.get("catalog_url", CONFIG["manifest_url"]))
        self.catalog_url_edit.setPlaceholderText("https://pastebin.com/raw/…")
        self.catalog_url_edit.setMaximumWidth(850)
        lay.addWidget(self.catalog_url_edit)
        self.catalog_save_url_btn = QPushButton("Save link and refresh")
        self.catalog_save_url_btn.setObjectName("SpacewarButton")
        self.catalog_save_url_btn.setMaximumWidth(260)
        self.catalog_save_url_btn.clicked.connect(self._save_catalog_url)
        lay.addWidget(self.catalog_save_url_btn)

        self.catalog_auto_check = QCheckBox("Automatically update from this link when Game Installer opens")
        self.catalog_auto_check.setChecked(bool(catalog_prefs.get("catalog_auto_update", True)))
        self.catalog_auto_check.toggled.connect(self._on_catalog_auto_toggled)
        lay.addWidget(self.catalog_auto_check)

        self.catalog_file_label = QLabel("A selected file is copied into Game Installer's own data folder.")
        self.catalog_file_label.setObjectName("SubInfo")
        self.catalog_file_label.setWordWrap(True)
        lay.addWidget(self.catalog_file_label)
        self.catalog_choose_file_btn = QPushButton("Choose local games.json…")
        self.catalog_choose_file_btn.setObjectName("NavButton")
        self.catalog_choose_file_btn.setMaximumWidth(260)
        self.catalog_choose_file_btn.clicked.connect(self._choose_catalog_file)
        lay.addWidget(self.catalog_choose_file_btn)

        self.catalog_json_edit = QPlainTextEdit()
        self.catalog_json_edit.setPlaceholderText("Paste a games.json array here. You can edit it before applying.")
        self.catalog_json_edit.setMaximumHeight(180)
        self.catalog_json_edit.setMaximumWidth(850)
        lay.addWidget(self.catalog_json_edit)
        self.catalog_apply_paste_btn = QPushButton("Apply pasted JSON")
        self.catalog_apply_paste_btn.setObjectName("PrimaryButton")
        self.catalog_apply_paste_btn.setMaximumWidth(260)
        self.catalog_apply_paste_btn.clicked.connect(self._apply_pasted_catalog)
        lay.addWidget(self.catalog_apply_paste_btn)
        self.catalog_status_label = QLabel("")
        self.catalog_status_label.setObjectName("SubInfo")
        self.catalog_status_label.setWordWrap(True)
        lay.addWidget(self.catalog_status_label)
        self._on_catalog_source_changed(self.catalog_source_combo.currentIndex())

        if sys.platform == "win32":
            self.defender_settings_check = QCheckBox(
                "Exclude installed game folders from Microsoft Defender scans"
            )
            self.defender_settings_check.setChecked(
                bool(load_prefs().get("defender_exclusion_default", True))
            )
            self.defender_settings_check.toggled.connect(self._on_defender_pref_toggled)
            lay.addWidget(self.defender_settings_check)
            defender_hint = QLabel(
                "On by default for new installs. The game folder and its contents "
                "will not be scanned by Microsoft Defender. The temporary folder "
                "is excluded during installation if needed. Turning this off does "
                "not remove existing exclusions. Administrator approval may be required."
            )
            defender_hint.setObjectName("SubInfo")
            defender_hint.setWordWrap(True)
            lay.addWidget(defender_hint)

        self.shortcut_check = QCheckBox(
            "Create a desktop shortcut to the game after installing"
        )
        self.shortcut_check.setChecked(
            bool(load_prefs().get("create_desktop_shortcut", False))
        )
        self.shortcut_check.toggled.connect(self._on_shortcut_pref_toggled)
        lay.addWidget(self.shortcut_check)

        hint = QLabel(
            "When on, a shortcut to the game's main program is placed on your "
            "Desktop once an install finishes, so you can launch the game with "
            "a double-click. The app picks the main .exe automatically. "
            "Off by default."
        )
        hint.setObjectName("SubInfo")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        # --- Retroactive: shortcuts for games installed earlier -------------
        self.bulk_shortcut_btn = QPushButton("Create shortcuts for installed games")
        self.bulk_shortcut_btn.setObjectName("SpacewarButton")
        self.bulk_shortcut_btn.setMaximumWidth(300)
        self.bulk_shortcut_btn.clicked.connect(self._on_bulk_shortcuts_clicked)
        lay.addWidget(self.bulk_shortcut_btn)

        self.bulk_shortcut_status = QLabel("")
        self.bulk_shortcut_status.setObjectName("SubInfo")
        self.bulk_shortcut_status.setWordWrap(True)
        lay.addWidget(self.bulk_shortcut_status)

        bulk_hint = QLabel(
            "Scans your install folder and adds a Desktop shortcut for every "
            "game already in it — handy for games you installed before the "
            "option above existed. You'll be asked to pick/confirm the folder "
            "first, and any shortcut that already exists is simply refreshed."
        )
        bulk_hint.setObjectName("SubInfo")
        bulk_hint.setWordWrap(True)
        lay.addWidget(bulk_hint)

        # --- Game Catalog / Custom games.json ------------------------------
        catalog_sep = QFrame()
        catalog_sep.setFrameShape(QFrame.Shape.HLine)
        catalog_sep.setStyleSheet("color: #232b40; margin-top: 8px; margin-bottom: 4px;")
        lay.addWidget(catalog_sep)

        catalog_title = QLabel("Game Catalog (games.json)")
        catalog_title.setStyleSheet("font-size: 15px; font-weight: 600; color: #e6e9f0;")
        lay.addWidget(catalog_title)

        catalog_desc = QLabel(
            "Import your own games.json file to customize the game list, add "
            "private titles, or point to your own download mirrors. The app "
            "saves your custom catalog directly next to the executable."
        )
        catalog_desc.setObjectName("SubInfo")
        catalog_desc.setWordWrap(True)
        lay.addWidget(catalog_desc)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.import_catalog_btn = QPushButton("Import custom games.json…")
        self.import_catalog_btn.setObjectName("PrimaryButton")
        self.import_catalog_btn.clicked.connect(self._on_import_catalog_clicked)
        btn_row.addWidget(self.import_catalog_btn)

        self.reset_catalog_btn = QPushButton("Reset to default catalog")
        self.reset_catalog_btn.setObjectName("SpacewarButton")
        self.reset_catalog_btn.clicked.connect(self._on_reset_catalog_clicked)
        btn_row.addWidget(self.reset_catalog_btn)

        self.open_catalog_btn = QPushButton("Open catalog folder")
        self.open_catalog_btn.setObjectName("SpacewarButton")
        self.open_catalog_btn.clicked.connect(self._on_open_catalog_folder_clicked)
        btn_row.addWidget(self.open_catalog_btn)

        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        self.catalog_status_label = QLabel("")
        self.catalog_status_label.setObjectName("SubInfo")
        self.catalog_status_label.setWordWrap(True)
        lay.addWidget(self.catalog_status_label)

        lay.addStretch(1)
        page.setWidget(content)
        return page

    def _on_import_catalog_clicked(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select custom games.json",
            app_dir(),
            "JSON Files (*.json);;All Files (*)",
        )
        if not file_path:
            return

        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                raw = fh.read()
            games = parse_manifest(raw)
            if not games:
                raise ValueError("The selected JSON file does not contain any game entries.")
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Invalid games.json",
                f"Could not load the selected catalog:\n\n{exc}"
            )
            return

        target_path = os.path.join(app_dir(), LOCAL_MANIFEST)
        try:
            with open(target_path, "w", encoding="utf-8") as fh:
                fh.write(raw)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Write Error",
                f"Could not save games.json to {target_path}:\n\n{exc}"
            )
            return

        settings = load_settings()
        settings["manifest_url"] = ""
        save_settings(settings)

        self.catalog_status_label.setText(
            f"Successfully imported {len(games)} games from {os.path.basename(file_path)}."
        )
        QMessageBox.information(
            self,
            "Catalog Imported",
            f"Successfully imported {len(games)} games from {os.path.basename(file_path)}.\n\n"
            "The library is now refreshing with your custom catalog."
        )
        self.load_manifest()

    def _on_reset_catalog_clicked(self):
        bundled_path = os.path.join(getattr(sys, "_MEIPASS", app_dir()), LOCAL_MANIFEST)
        target_path = os.path.join(app_dir(), LOCAL_MANIFEST)

        if not os.path.isfile(bundled_path):
            QMessageBox.warning(
                self,
                "Default Catalog Missing",
                "The bundled default catalog could not be found."
            )
            return

        reply = QMessageBox.question(
            self,
            "Reset Catalog",
            "Are you sure you want to restore the default game list? "
            "Any custom games.json in this folder will be replaced with the original catalog.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            shutil.copyfile(bundled_path, target_path)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Reset Failed",
                f"Could not restore default catalog:\n\n{exc}"
            )
            return

        settings = load_settings()
        settings.pop("manifest_url", None)
        save_settings(settings)

        self.catalog_status_label.setText("Default catalog restored.")
        QMessageBox.information(
            self,
            "Catalog Reset",
            "The default game catalog has been restored. Reloading library…"
        )
        self.load_manifest()

    def _on_open_catalog_folder_clicked(self):
        directory = app_dir()
        try:
            if sys.platform == "win32":
                os.startfile(directory)
            elif sys.platform == "darwin":
                subprocess.run(["open", directory])
            else:
                subprocess.run(["xdg-open", directory])
        except Exception as exc:
            QMessageBox.warning(self, "Open Folder", f"Could not open folder:\n{exc}")

    def _on_defender_pref_toggled(self, checked):
        prefs = load_prefs()
        prefs["defender_exclusion_default"] = bool(checked)
        save_prefs(prefs)

    def _on_shortcut_pref_toggled(self, checked):
        prefs = load_prefs()
        prefs["create_desktop_shortcut"] = bool(checked)
        save_prefs(prefs)

    def _on_catalog_source_changed(self, _index):
        prefs = load_prefs()
        mode = self.catalog_source_combo.currentData()
        prefs["catalog_mode"] = mode
        is_url = mode == "url"
        is_local = mode == "local"
        is_paste = mode == "paste"
        self.catalog_url_edit.setVisible(is_url)
        self.catalog_save_url_btn.setVisible(is_url)
        self.catalog_auto_check.setVisible(is_url)
        self.catalog_auto_check.setEnabled(is_url)
        self.catalog_file_label.setVisible(is_local or is_paste)
        self.catalog_choose_file_btn.setVisible(is_local or is_paste)
        self.catalog_json_edit.setVisible(is_paste)
        self.catalog_apply_paste_btn.setVisible(is_paste)
        if mode == "url" and self.catalog_source_combo.currentIndex() == 0:
            prefs["catalog_url"] = CONFIG["manifest_url"]
            self.catalog_url_edit.setText(CONFIG["manifest_url"])
        save_prefs(prefs)

    def _on_catalog_auto_toggled(self, checked):
        prefs = load_prefs()
        prefs["catalog_auto_update"] = bool(checked)
        save_prefs(prefs)

    def _save_catalog_url(self):
        try:
            url = normalize_catalog_url(self.catalog_url_edit.text())
            prefs = load_prefs()
            prefs.update({"catalog_mode": "url", "catalog_url": url})
            save_prefs(prefs)
            self.catalog_url_edit.setText(url)
            target_index = 0 if url == CONFIG["manifest_url"] else 1
            self.catalog_source_combo.blockSignals(True)
            self.catalog_source_combo.setCurrentIndex(target_index)
            self.catalog_source_combo.blockSignals(False)
            self.catalog_status_label.setText("Saved. Loading catalog from the HTTPS link…")
            self.load_manifest(manual=True)
        except Exception as exc:
            QMessageBox.warning(self, "Catalog link", str(exc))

    def _save_local_catalog_data(self, raw: str, mode: str, label: str):
        games = validate_manifest(raw)
        path = user_catalog_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(raw)
        prefs = load_prefs()
        prefs["catalog_mode"] = mode
        save_prefs(prefs)
        for index in range(self.catalog_source_combo.count()):
            if self.catalog_source_combo.itemData(index) == mode:
                self.catalog_source_combo.blockSignals(True)
                self.catalog_source_combo.setCurrentIndex(index)
                self.catalog_source_combo.blockSignals(False)
                break
        self.catalog_status_label.setText(f"Saved {len(games)} games from {label}.")
        self.load_manifest(manual=True)

    def _choose_catalog_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose games.json", "", "JSON files (games.json *.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                raw = fh.read()
            self._save_local_catalog_data(raw, "local", os.path.basename(path))
            self.catalog_file_label.setText(f"Using a managed copy of: {path}")
        except Exception as exc:
            QMessageBox.warning(self, "Local game catalog", f"This file could not be used:\n{exc}")

    def _apply_pasted_catalog(self):
        try:
            self._save_local_catalog_data(self.catalog_json_edit.toPlainText(), "paste", "pasted JSON")
        except Exception as exc:
            QMessageBox.warning(self, "Pasted game catalog", f"This JSON could not be used:\n{exc}")

    # -- bulk shortcut creation for already-installed games -----------------
    def _on_bulk_shortcuts_clicked(self):
        if self._sweep_thread is not None:
            return  # a sweep is already running
        start = load_prefs().get("install_path", "").strip()
        if not start or not os.path.isdir(start):
            start = os.path.expanduser("~")
        base = QFileDialog.getExistingDirectory(
            self, "Select the folder your games are installed in", start)
        if not base:
            return
        self.bulk_shortcut_btn.setEnabled(False)
        self.bulk_shortcut_status.setText("Scanning…")
        self._sweep_thread = QThread()
        self._sweep_worker = ShortcutSweepWorker(base)
        self._sweep_worker.moveToThread(self._sweep_thread)
        self._sweep_thread.started.connect(self._sweep_worker.run)
        self._sweep_worker.progress.connect(
            lambda name: self.bulk_shortcut_status.setText(f"Processing {name}…"))
        self._sweep_worker.done.connect(self._on_bulk_shortcuts_done)
        self._sweep_thread.start()

    @pyqtSlot(list, list)
    def _on_bulk_shortcuts_done(self, created, skipped):
        if self._sweep_thread is not None:
            self._sweep_thread.quit()
            self._sweep_thread.wait()
            self._sweep_worker.deleteLater()
            self._sweep_thread.deleteLater()
            self._sweep_worker = None
            self._sweep_thread = None
        self.bulk_shortcut_btn.setEnabled(True)

        n = len(created)
        summary = f"Created {n} shortcut{'' if n == 1 else 's'} on your Desktop."
        if skipped:
            summary += f"  Skipped {len(skipped)}."
        self.bulk_shortcut_status.setText(summary)

        lines = [summary]
        if created:
            lines += [""] + [f"✓  {name}" for name in created]
        if skipped:
            lines += ["", "Skipped (no game program found):"]
            lines += [f"•  {name}" for name, _reason in skipped]
        QMessageBox.information(self, "Desktop shortcuts", "\n".join(lines))

    # -- manifest loading ---------------------------------------------------
    def _refresh_catalog_automatically(self):
        prefs = load_prefs()
        if prefs.get("catalog_mode", "url") != "url" or not prefs.get("catalog_auto_update", True):
            return
        self.load_manifest(background=True)

    def load_manifest(self, manual: bool = False, background: bool = False):
        if self.manifest_thread is not None:
            if manual:
                self._manual_catalog_refresh_requested = True
            return
        prefs = load_prefs()
        mode = prefs.get("catalog_mode", "url")
        url = prefs.get("catalog_url", CONFIG["manifest_url"])
        if mode == "url" and not manual and not prefs.get("catalog_auto_update", True):
            mode = "cached"
        self._manifest_background_refresh = background
        if not background:
            self.refresh_btn.setEnabled(False)
            self.stack.setCurrentWidget(self.loading_page)

        self.manifest_thread = QThread()
        self.manifest_worker = ManifestLoader(url if mode == "url" else "", mode)
        self.manifest_worker.moveToThread(self.manifest_thread)
        self.manifest_thread.started.connect(self.manifest_worker.run)
        self.manifest_worker.loaded.connect(self._on_manifest_loaded)
        self.manifest_worker.failed.connect(self._on_manifest_failed)
        self.manifest_worker.unchanged.connect(self._on_manifest_unchanged)
        self.manifest_thread.start()

    def _teardown_manifest_thread(self):
        if self.manifest_thread is not None:
            self.manifest_thread.quit()
            self.manifest_thread.wait()
            self.manifest_worker.deleteLater()
            self.manifest_thread.deleteLater()
            self.manifest_worker = None
            self.manifest_thread = None
        self.refresh_btn.setEnabled(True)

    def _run_queued_manual_catalog_refresh(self):
        if not self._manual_catalog_refresh_requested:
            return
        self._manual_catalog_refresh_requested = False
        QTimer.singleShot(0, lambda: self.load_manifest(manual=True))

    @pyqtSlot(list, str)
    def _on_manifest_loaded(self, games, source):
        background = self._manifest_background_refresh
        self._teardown_manifest_thread()
        self._current_games = games
        self.download_manager.restore_interrupted(games)
        if not background:
            self._search_bar.clear()
        self._on_search_changed(self._search_bar.text())
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.updated_label.setText(f"Last updated {stamp}  ·  {source}")
        if not background:
            self.stack.setCurrentWidget(self.content_page)
        # Defer the Spacewar check until after the cards are painted.
        if not background:
            QTimer.singleShot(400, self._maybe_prompt_spacewar)
        self._run_queued_manual_catalog_refresh()

    @pyqtSlot()
    def _on_manifest_unchanged(self):
        self._teardown_manifest_thread()
        self._run_queued_manual_catalog_refresh()

    @pyqtSlot(str)
    def _on_manifest_failed(self, message):
        background = self._manifest_background_refresh
        self._teardown_manifest_thread()
        if background and self._current_games:
            self.updated_label.setToolTip(message)
            self._run_queued_manual_catalog_refresh()
            return
        self.error_label.setText(message)
        self.stack.setCurrentWidget(self.error_page)
        self._run_queued_manual_catalog_refresh()

    # -- Spacewar ------------------------------------------------------------
    def _maybe_prompt_spacewar(self):
        """One-time auto-prompt: ask the user to install Spacewar if it is not
        already present.  Runs at most once per install (flag in settings.json)
        and at most once per app session (self._spacewar_check_done)."""
        if self._spacewar_check_done:
            return
        self._spacewar_check_done = True

        settings = load_settings()
        if settings.get("spacewar_prompted"):
            return  # Already asked in a previous session.

        # Silently mark as prompted regardless of what happens next.
        settings["spacewar_prompted"] = True
        save_settings(settings)

        if is_spacewar_installed():
            return  # Already installed — nothing to do.

        reply = QMessageBox.question(
            self,
            "Install Spacewar?",
            "Spacewar is a free Steam title that enables online multiplayer "
            "features for some games in this library.\n\n"
            "Would you like to install it now?  Steam will open and handle the "
            "download automatically.\n\n"
            "You can also install it at any time using the "
            "“Get Spacewar” button in the toolbar.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._do_launch_spacewar()

    def _do_launch_spacewar(self):
        """Open steam://install/480.  Shows a friendly error if Steam is missing."""
        if not launch_spacewar_install():
            QMessageBox.warning(
                self,
                "Steam not found",
                "Could not open the Steam installer link.\n\n"
                "Make sure Steam is installed on this PC, then try again.\n"
                "You can download Steam at store.steampowered.com",
            )

    def _launch_spacewar_manual(self):
        """Manual button handler — always launches, ignores the one-time flag."""
        self._do_launch_spacewar()

    def _launch_steam(self):
        if not launch_steam_client():
            QMessageBox.warning(
                self,
                "Steam not found",
                "Could not open Steam. Make sure Steam is installed, then try again.\n\n"
                "You can download Steam at store.steampowered.com",
            )

    def _on_search_changed(self, text: str):
        q = text.strip()
        filtered = (
            [g for g in self._current_games
             if matches_game_search(g.name, g.description, q)]
            if q else self._current_games
        )
        mode = self._library_filter.currentText()
        if mode == "Installed games":
            base = default_game_directory()
            editions = [edition for game in filtered for edition in
                        ([game, game.more_players_game()] if game.more_players else [game])]
            filtered = [g for g in editions if self._is_installed_game(base, g)]
        elif mode == "Favorites":
            filtered = [g for g in filtered if g.id in self._favorite_game_ids]
        elif mode == "Name A–Z":
            filtered = sorted(filtered, key=lambda g: g.name.casefold())
        elif mode == "Name Z–A":
            filtered = sorted(filtered, key=lambda g: g.name.casefold(), reverse=True)
        elif mode == "Newest first":
            filtered = list(reversed(filtered))
        elif mode == "Installed first":
            base = default_game_directory()
            filtered = sorted(filtered, key=lambda g: not (self._is_installed_game(base, g) or
                (g.more_players and self._is_installed_game(base, g.more_players_game()))))
        elif mode.startswith("Size:") or mode.startswith("Players:"):
            def metric(game):
                if mode.startswith("Size:"):
                    match = re.search(r"(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB)\b", game.size, re.I)
                    return float(match.group(1)) * {"KB": 1, "MB": 1024, "GB": 1024**2, "TB": 1024**3}[match.group(2).upper()] if match else None
                values = [int(n) for n in re.findall(r"\d+", game.players)]
                return max(values) if values else None
            reverse = "largest" in mode or "most" in mode
            known = [(g, metric(g)) for g in filtered]
            filtered = [g for g, v in sorted((pair for pair in known if pair[1] is not None), key=lambda pair: pair[1], reverse=reverse)] + [g for g, v in known if v is None]
        self._populate_cards(filtered)

    def _show_library(self):
        image = self.stack.capture_page() if self.stack.currentWidget() is self.content_page else None
        self._library_heading.setText("Your library")
        self._library_subtitle.setText("Find, install, and manage your games.")
        self._library_filter.setCurrentText("Newest first")
        self._on_search_changed(self._search_bar.text())
        self.stack.setCurrentWidget(self.content_page)
        self.stack.animate_from(image, -1)

    def _show_owned_games(self):
        image = self.stack.capture_page() if self.stack.currentWidget() is self.content_page else None
        self._library_heading.setText("Your games")
        self._library_subtitle.setText("Play and manage games in your selected folder.")
        self._library_filter.setCurrentText("Installed games")
        self._on_search_changed(self._search_bar.text())
        self.stack.setCurrentWidget(self.content_page)
        self.stack.animate_from(image, 1)

    def _toggle_favorite(self, game):
        favorites = set(self._favorite_game_ids)
        if game.id in favorites:
            favorites.remove(game.id)
        else:
            favorites.add(game.id)
        prefs = load_prefs()
        prefs["favorite_game_ids"] = sorted(favorites)
        if save_prefs(prefs) is False:
            QMessageBox.warning(self, "Could not save favorite", "Your favorite games could not be saved.")
            return
        self._favorite_game_ids = favorites
        self._on_search_changed(self._search_bar.text())

    @staticmethod
    def _is_installed_game(base, game):
        if not base:
            return False
        path = os.path.join(base, game.safe_folder_name)
        try:
            return os.path.isdir(path) and not os.path.islink(path) and bool(os.listdir(path))
        except OSError:
            return False

    def _choose_games_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose games folder", default_game_directory())
        if not folder:
            return
        prefs = load_prefs()
        prefs["install_path"] = folder
        if save_prefs(prefs) is False:
            QMessageBox.warning(self, "Could not select games folder", "The folder preference could not be saved.")
            return
        self._on_search_changed(self._search_bar.text())

    def _move_games_folder(self):
        if any(c.state in ("queued", "running") for c in self.download_manager.cards):
            QMessageBox.warning(self, "Downloads active", "Wait for active downloads to finish before moving games.")
            return
        source = default_game_directory()
        if not source or not os.path.isdir(source):
            QMessageBox.warning(self, "Games folder unavailable", "Choose an existing games folder first.")
            return
        source = os.path.abspath(source)
        target = QFileDialog.getExistingDirectory(self, "Choose destination for installed games", source)
        if not target:
            return
        target = os.path.abspath(target)
        if os.path.normcase(target) == os.path.normcase(source):
            return
        games = [edition for g in self._current_games for edition in
                 ([g, g.more_players_game()] if g.more_players else [g])]
        names = list(dict.fromkeys(g.safe_folder_name for g in games if self._is_installed_game(source, g)))
        if not names:
            QMessageBox.information(self, "No games found", "No installed catalog games were found in the current folder.")
            return
        try:
            cross_drive = os.path.splitdrive(source)[0].casefold() != os.path.splitdrive(target)[0].casefold()
            if not cross_drive and any(os.path.normcase(os.path.commonpath([os.path.realpath(target), os.path.realpath(os.path.join(source, n))])) == os.path.normcase(os.path.realpath(os.path.join(source, n))) for n in names):
                raise ValueError("Choose a destination outside the game folders being moved.")
            conflicts = [n for n in names if os.path.lexists(os.path.join(target, n))]
            if conflicts:
                raise ValueError("Destination already contains: " + ", ".join(conflicts))
            if cross_drive:
                self._copy_games_between_drives(source, target, names)
                return
            answer = QMessageBox.question(self, "Move games", f"Move {len(names)} game folders to:\n{target}\n\nExisting shortcuts and Defender exclusions may need to be updated.")
            if answer != QMessageBox.StandardButton.Yes:
                return
            moved = []
            try:
                for name in names:
                    os.rename(os.path.join(source, name), os.path.join(target, name))
                    moved.append(name)
            except OSError as exc:
                failures = []
                for name in reversed(moved):
                    try:
                        os.rename(os.path.join(target, name), os.path.join(source, name))
                    except OSError:
                        failures.append(name)
                raise OSError(f"Move failed: {exc}. " + ("Could not restore: " + ", ".join(failures) if failures else "Moved folders were restored.")) from exc
            prefs = load_prefs()
            prefs["install_path"] = target
            if save_prefs(prefs) is False:
                raise OSError(f"The games were moved to {target}, but the folder preference could not be saved. Select that folder manually.")
            self._on_search_changed(self._search_bar.text())
            QMessageBox.information(self, "Games moved", f"Moved {len(names)} game folders to:\n{target}")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Could not move games", str(exc))

    def _copy_games_between_drives(self, source, target, names):
        # Keep originals as recoverable backups; never delete user game data.
        answer = QMessageBox.question(self, "Copy games to another drive",
            f"Copy {len(names)} game folders to:\n{target}\n\n"
            "The original folders will remain as backups. Existing shortcuts and Defender exclusions will not be updated.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        staged = tempfile.mkdtemp(prefix="game-installer-copy-", dir=target)
        published = []
        try:
            def digest(path):
                h = hashlib.sha256()
                with open(path, "rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        h.update(chunk)
                return h.digest()

            for name in names:
                original = os.path.join(source, name)
                for root, dirs, files in os.walk(original, followlinks=False):
                    if any(os.path.islink(os.path.join(root, entry)) or (getattr(os.lstat(os.path.join(root, entry)), "st_file_attributes", 0) & 0x400) for entry in dirs + files):
                        raise ValueError(f"{name} contains a symbolic link or junction; no games were moved.")
                copy = os.path.join(staged, name)
                shutil.copytree(original, copy)
                for root, dirs, files in os.walk(original):
                    for filename in files:
                        path = os.path.join(root, filename)
                        counterpart = os.path.join(copy, os.path.relpath(path, original))
                        if digest(path) != digest(counterpart):
                            raise ValueError(f"Verification failed for {name}; originals were retained.")
            for name in names:
                destination = os.path.join(target, name)
                if os.path.lexists(destination):
                    raise FileExistsError(f"Destination already exists: {destination}")
                os.rename(os.path.join(staged, name), destination)
                published.append(name)
            prefs = load_prefs()
            prefs["install_path"] = target
            if save_prefs(prefs) is False:
                raise OSError(f"The copies were created in {target}, but the folder preference could not be saved. Select that folder manually.")
            self._on_search_changed(self._search_bar.text())
            QMessageBox.information(self, "Games copied", f"Copied {len(names)} game folders to:\n{target}\n\nOriginals remain in:\n{source}")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Copy incomplete", f"{exc}\nOriginals remain in {source}. Published copies: {', '.join(published) or 'none'}. Temporary files: {staged}")
            return
        os.rmdir(staged)

    def _populate_cards(self, games):
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not games:
            searching = self._search_bar.text().strip() if hasattr(self, "_search_bar") else ""
            if hasattr(self, "_library_filter") and self._library_filter.currentText() == "Installed games":
                msg = "No installed games found in the selected games folder."
            elif hasattr(self, "_library_filter") and self._library_filter.currentText() == "Favorites":
                msg = "No favorite games yet. Select the star on a game card to add one."
            else:
                msg = "No games match your search." if searching else "No games are available right now."
            placeholder = QLabel(msg)
            placeholder.setObjectName("SubInfo")
            self.cards_layout.addWidget(placeholder)
            return

        owned = self._library_filter.currentText() == "Installed games"
        for game in games:
            if owned:
                self.cards_layout.addWidget(GameCard(
                    game, self._launch_owned_game, installed=True,
                    on_choose=self._choose_owned_executable,
                    on_favorite=self._toggle_favorite,
                    is_favorite=game.id in self._favorite_game_ids,
                ))
            else:
                self.cards_layout.addWidget(GameCard(
                    game, self._open_install,
                    on_favorite=self._toggle_favorite,
                    is_favorite=game.id in self._favorite_game_ids,
                ))

    def _choose_owned_executable(self, game):
        root = os.path.abspath(os.path.join(default_game_directory(), game.safe_folder_name))
        path, _ = QFileDialog.getOpenFileName(self, f"Choose executable for {game.name}", root, "Windows applications (*.exe)")
        if not path:
            return None
        if not os.path.isfile(path) or not path.lower().endswith('.exe'):
            QMessageBox.warning(self, "Invalid executable", "Choose an existing Windows executable.")
            return None
        prefs = load_prefs()
        try:
            inside = os.path.commonpath([root, os.path.abspath(path)]).casefold() == root.casefold()
        except ValueError:
            inside = False
        prefs.setdefault("owned_game_executables", {})[game.safe_folder_name] = os.path.relpath(path, root) if inside else os.path.abspath(path)
        if save_prefs(prefs) is False:
            QMessageBox.warning(self, "Could not save", "The executable choice could not be saved.")
            return None
        return path

    def _launch_owned_game(self, game):
        root = os.path.abspath(os.path.join(default_game_directory(), game.safe_folder_name))
        choice = load_prefs().get("owned_game_executables", {}).get(game.safe_folder_name)
        path = os.path.abspath(os.path.join(root, choice)) if choice else game_launch_executable(root, game)
        if path and not choice and _NON_GAME_EXE_RE.search(os.path.basename(path)):
            path = None
        if not path or not os.path.isfile(path):
            if QMessageBox.question(self, "Choose game executable", "The game executable could not be found. Choose it now?") != QMessageBox.StandardButton.Yes:
                return
            path = self._choose_owned_executable(game)
        if path:
            try:
                subprocess.Popen([path], cwd=os.path.dirname(path))
            except OSError as exc:
                QMessageBox.warning(self, "Could not launch game", str(exc))

    def _open_install(self, game: Game):
        _log.debug(
            "_open_install  id=%r  name=%r  type=%r  zip_url=%r  fix_url=%r",
            game.id, game.name, game.entry_type, game.zip_url, game.fix_url,
        )
        if game.entry_type == "steam":
            try:
                os.startfile(game.zip_url)
            except OSError:
                subprocess.Popen(
                    ["cmd", "/c", "start", game.zip_url],
                    creationflags=_NO_WINDOW,
                )
            return

        if game.is_patch:
            # Step 1 — ask whether the user already has the base game installed.
            ownership_dlg = FixOwnershipDialog(game, self)
            result = ownership_dlg.exec()

            if result == FixOwnershipDialog.RESULT_NO:
                # Step 2a — No: launch the Steam install link, then return home.
                zip_url = game.zip_url
                if zip_url.startswith("steam://"):
                    try:
                        os.startfile(zip_url)
                    except OSError:
                        subprocess.Popen(
                            ["cmd", "/c", "start", zip_url],
                            creationflags=_NO_WINDOW,
                        )
                return

            if result != FixOwnershipDialog.RESULT_YES:
                # Dialog was dismissed without choosing — do nothing.
                return

            # Step 2b — Yes: proceed with the existing fix-apply flow.
            steam_common = r"C:\Program Files (x86)\Steam\steamapps\common"
            start_dir = steam_common if os.path.isdir(steam_common) else os.path.expanduser("~")
            folder = QFileDialog.getExistingDirectory(
                self,
                f"Select game folder to apply '{game.name}' into",
                start_dir,
            )
            if not folder:
                return
            dlg = InstallDialog(game, self, patch_target=folder,
                                manager=self.download_manager)
            self._show_game_details(dlg)
            return

        # Normal (non-fix) game — unchanged.
        dlg = InstallDialog(game, self, manager=self.download_manager)
        self._show_game_details(dlg)

    def _show_game_details(self, dialog):
        scrim = QFrame(self.centralWidget())
        scrim.setObjectName('DetailScrim')
        scrim.setStyleSheet('#DetailScrim { background-color: rgba(0, 0, 0, 155); }')
        layout = QVBoxLayout(scrim)
        layout.setContentsMargins(20, 20, 20, 20)
        panel = QFrame()
        panel.setObjectName('DetailPanel')
        panel.setStyleSheet('#DetailPanel { background-color: #0c111c; border: 1px solid #394452; border-radius: 18px; }')
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(12, 10, 12, 12)
        close = QPushButton('✕')
        close.setObjectName('NavButton')
        close.setToolTip('Close details')
        close.clicked.connect(dialog.reject)
        panel_layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        dialog.setParent(scroll)
        dialog.setWindowFlags(Qt.WindowType.Widget)
        dialog.setModal(False)
        scroll.setWidget(dialog)
        panel_layout.addWidget(scroll, 1)
        layout.addWidget(panel, 0, Qt.AlignmentFlag.AlignCenter)
        self._details_overlay = (scrim, panel)
        self._resize_details_overlay()

        def finished(result):
            self._details_overlay = None
            scrim.hide()
            scrim.deleteLater()
            if result == QDialog.DialogCode.Accepted:
                self.stack.setCurrentWidget(self.downloads_page)

        dialog.finished.connect(finished)
        scrim.show()
        scrim.raise_()
        dialog.show()
        if ui_motion_enabled():
            effect = QGraphicsOpacityEffect(scrim)
            scrim.setGraphicsEffect(effect)
            animation = QVariantAnimation(scrim)
            animation.setDuration(220)
            animation.setStartValue(0.0)
            animation.setEndValue(1.0)
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            animation.valueChanged.connect(effect.setOpacity)
            animation.finished.connect(lambda: scrim.setGraphicsEffect(None))
            scrim._opening_animation = animation
            animation.start()

    def _resize_details_overlay(self):
        overlay = getattr(self, '_details_overlay', None)
        if overlay:
            scrim, panel = overlay
            area = self.centralWidget().rect()
            scrim.setGeometry(area)
            panel.setFixedSize(max(1, min(900, area.width() - 40)),
                               max(1, min(940, area.height() - 40)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_details_overlay()


# ---------------------------------------------------------------------------
# Styling — dark navy/charcoal theme with a teal accent.
# ---------------------------------------------------------------------------
STYLE = """
QWidget {
    background-color: #0c111c;
    color: #e8edf5;
    font-family: 'Segoe UI', sans-serif;
    font-size: 14px;
}
/* Labels paint transparently so the card surface shows through; specific
   id-selectors below re-add their own backgrounds. */
QLabel { background-color: transparent; }

#Header { background-color: #121b2a; border-right: 1px solid #29374b; }
#AppTitle { font-size: 22px; font-weight: 700; color: #f7fafc; }
#LibraryHeading { font-size: 28px; font-weight: 700; color: #f7fafc; }
#UpdatedLabel { color: #a4b2c4; font-size: 12px; }

#Scroll { border: none; }

#GameCard {
    background-color: #172132;
    border: 1px solid #2a3a4d;
    border-radius: 14px;
}
#GameCard:hover { border: 1px solid #5de2cc; background-color: #1d2c3c; }
#Thumb {
    background-color: #0b0f1a;
    border-radius: 12px;
    color: #6b7280;
}
#Thumb[placeholder="true"] {
    color: #aeb4c6;
    font-size: 16px;
    font-weight: 700;
}
#CardTitle { font-size: 16px; font-weight: 700; color: #ffffff; }
#CardDesc { color: #aeb4c6; font-size: 13px; }
#CardMeta { color: #8b93a7; font-size: 12px; }
#CardPlayers { color: #93c5fd; font-size: 12px; font-weight: 600; }

#BadgeAvailable { color: #2dd4bf; font-size: 12px; font-weight: 700; }
#BadgeFix { color: #a78bfa; font-size: 12px; font-weight: 700; }
#BadgeSoon {
    color: #fbbf24; font-size: 12px; font-weight: 700;
    background-color: #3a2f12; border-radius: 6px; padding: 3px 8px;
}

/* macCompatibility tier badges (data written at publish time) */

/* Tier 0 — Native macOS build */
#CompatBadge_Native {
    color: #4ade80; font-size: 12px; font-weight: 700;
    background-color: #052e16; border-radius: 6px; padding: 3px 8px;
    border: 1px solid #166534;
}
/* Tier 1 — Runs great */
#CompatBadge_Great {
    color: #22c55e; font-size: 12px; font-weight: 700;
    background-color: #0d2b1a; border-radius: 6px; padding: 3px 8px;
}
/* Tier 2 — Runs with a recipe */
#CompatBadge_Recipe {
    color: #fbbf24; font-size: 12px; font-weight: 700;
    background-color: #2e2510; border-radius: 6px; padding: 3px 8px;
}
/* Tier 3 — Partial, standard risk */
#CompatBadge_Partial {
    color: #fb923c; font-size: 12px; font-weight: 700;
    background-color: #2a1a08; border-radius: 6px; padding: 3px 8px;
}
/* Tier 3 — Partial, HIGH risk (DX12/Vulkan + known bad engine) */
#CompatBadge_PartialHigh {
    color: #f97316; font-size: 12px; font-weight: 700;
    background-color: #3b1407; border-radius: 6px; padding: 3px 8px;
    border: 1px solid #c2410c;
}
/* Tier 4 — Not yet */
#CompatBadge_NotYet {
    color: #f87171; font-size: 12px; font-weight: 700;
    background-color: #2b0f0f; border-radius: 6px; padding: 3px 8px;
}
/* Fallback */
#CompatBadge_Unknown {
    color: #8b93a7; font-size: 12px; font-weight: 700;
}

/* Reasoning label — standard */
#CompatReasoning {
    color: #8b93a7; font-size: 12px;
    background-color: #131826; border-radius: 6px;
    padding: 5px 8px;
}
/* Reasoning label — Low confidence (muted + italic to signal "estimated") */
#CompatReasoning_Low {
    color: #6b7280; font-size: 12px; font-style: italic;
    background-color: #0f131e; border-radius: 6px;
    padding: 5px 8px; border: 1px dashed #374151;
}


#SysCheckBox {
    background-color: #161b2e;
    border: 1px solid #232a42;
    border-radius: 10px;
}
#SysVerdictPass { color: #22c55e; font-size: 14px; font-weight: 700; background: transparent; }
#SysVerdictWarn { color: #fbbf24; font-size: 14px; font-weight: 700; background: transparent; }
#SysVerdictFail { color: #f87171; font-size: 14px; font-weight: 700; background: transparent; }
#SysVerdictUnknown { color: #8b93a7; font-size: 14px; font-weight: 700; background: transparent; }
#SysCheckDetail { color: #c4cad8; font-size: 12px; background: transparent; }
#SysCheckTip { color: #8b93a7; font-size: 11px; background: transparent; }

#DialogTitle { font-size: 20px; font-weight: 700; color: #ffffff; }
#SectionTitle { color: #e8edf5; font-size: 16px; font-weight: 700; margin-top: 8px; }
#SectionLabel { font-size: 13px; font-weight: 600; color: #c4cad8; margin-top: 4px; }
#SmallLabel { color: #8b93a7; font-size: 12px; }
#StatusLabel { color: #c4cad8; font-size: 13px; }
#MetricLabel { color: #2dd4bf; font-size: 12px; font-weight: 500; }
#DefenderWarning { color: #f59e0b; font-size: 12px; }

#BigInfo { font-size: 34px; font-weight: 700; color: #ffffff; }
#SubInfo { color: #aeb4c6; font-size: 15px; }

QLineEdit, QComboBox, QPlainTextEdit {
    background-color: #0b0f1a;
    border: 1px solid #232b40;
    border-radius: 8px;
    padding: 8px 10px;
    selection-background-color: #2dd4bf;
    selection-color: #06231f;
}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border: 1px solid #5de2cc; }
QComboBox QAbstractItemView { background-color: #172132; color: #e8edf5; selection-background-color: #28554f; border: 1px solid #34495c; }
QCheckBox { spacing: 9px; color: #e8edf5; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #8192a7; border-radius: 4px; background: #0b0f1a; }
QCheckBox::indicator:checked { background: #2dd4bf; border: 1px solid #5de2cc; }
QCheckBox::indicator:focus { border: 2px solid #80f1df; }

QPushButton {
    background-color: #232b40;
    border: 1px solid #2e3750;
    border-radius: 10px;
    padding: 9px 16px;
    color: #e6e9f0;
}
QPushButton:hover { background-color: #2e3750; }
QPushButton:pressed { background-color: #34465a; }
QPushButton:focus { border: 1px solid #5de2cc; }
QPushButton:disabled { color: #6b7280; background-color: #171d2e; }

#PrimaryButton {
    background-color: #2dd4bf;
    border: none;
    color: #06231f;
    font-weight: 700;
}
#PrimaryButton:hover { background-color: #5eead4; }
#PrimaryButton:disabled { background-color: #1f3b39; color: #6f8a86; }

#SpacewarButton {
    background-color: #1a2744;
    border: 1px solid #2a4a80;
    color: #7eaaee;
}
#SpacewarButton:hover { background-color: #1e3052; border-color: #4a7abf; }

QProgressBar {
    background-color: #0b0f1a;
    border: 1px solid #232b40;
    border-radius: 8px;
    height: 16px;
    text-align: center;
    color: #e6e9f0;
}
QProgressBar::chunk { background-color: #2dd4bf; border-radius: 7px; }

QScrollBar:vertical { background: #0f1320; width: 12px; margin: 0; }
QScrollBar::handle:vertical {
    background: #232b40; border-radius: 6px; min-height: 30px;
}
QScrollBar::handle:vertical:hover { background: #2e3750; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

/* Header navigation buttons */
#NavButton {
    background-color: transparent; color: #c4cad8;
    border: 1px solid #232a42; border-radius: 8px;
    padding: 10px 14px; font-weight: 600; font-size: 14px; text-align: left;
}
#NavButton:hover { background-color: #1a2036; color: #ffffff; }
#NavButton[active="true"] { background-color: #1b3b3c; color: #80f1df; border: 1px solid #3f9e91; }
#UpdateAvailable {
    background-color: #78420f; color: #ffdf9a; border: 1px solid #f59e0b;
    border-radius: 8px; padding: 7px 14px; font-weight: 700;
}
#UpdateAvailable:hover { background-color: #965313; }

/* Downloads page */
#DownloadsBar { background-color: #121b2a; border-bottom: 1px solid #29374b; }
#DownloadCard {
    background-color: #172132; border: 1px solid #2a3a4d; border-radius: 14px;
}
#DownloadName { font-size: 16px; font-weight: 700; color: #ffffff; }

#ChipQueued   { color: #8b93a7; font-size: 12px; font-weight: 700; padding: 2px 8px; }
#ChipRunning  { color: #2dd4bf; font-size: 12px; font-weight: 700; padding: 2px 8px; }
#ChipDone     { color: #22c55e; font-size: 12px; font-weight: 700; padding: 2px 8px; }
#ChipError    { color: #f87171; font-size: 12px; font-weight: 700; padding: 2px 8px; }
#ChipCanceled { color: #fbbf24; font-size: 12px; font-weight: 700; padding: 2px 8px; }
"""


def app_style():
    """Register bundled fonts, falling back to fonts available on the system."""
    font_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
    for filename in ("Manrope.ttf", "Outfit.ttf"):
        if QFontDatabase.addApplicationFont(os.path.join(font_dir, filename)) < 0:
            _log.warning("Could not load bundled font %s", filename)
    families = set(QFontDatabase.families())
    body = next((font for font in ("Manrope", "Segoe UI", "Arial") if font in families), "sans-serif")
    display = next((font for font in ("Outfit", "Bahnschrift", "Segoe UI", "Arial") if font in families), body)
    return STYLE.replace("'Segoe UI', sans-serif", f"'{body}'") + (
        f"\n#AppTitle, #LibraryHeading, #DialogTitle, #BigInfo "
        f"{{ font-family: '{display}'; font-weight: 700; }}\n"
        f"#CardTitle, #DownloadName, #SectionTitle {{ font-family: '{body}'; font-weight: 700; }}\n"
    )


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(CONFIG["window_title"])
    app.setStyle("Fusion")
    app.setStyleSheet(app_style())

    # Let in-flight thumbnail fetches finish before the interpreter tears down,
    # so Qt-pool threads are never running Python during finalizations much hehe
    app.aboutToQuit.connect(lambda: QThreadPool.globalInstance().waitForDone(3000))

    # Evict abandoned resume-partials from the download cache so they don't pile
    # up; recent ones are kept so an interrupted download can still be resumed.
    sweep_cache()

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    if sys.argv[1:] == ["--check-download-browser"]:
        sys.exit(subprocess.call(helper_command() + ["--self-check"],
                                 env={**os.environ, "PYINSTALLER_RESET_INVIRONMENT": "1"}))
    else:
        main()
