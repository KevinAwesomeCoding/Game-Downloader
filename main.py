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
import logging
import os
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
import winreg
import zipfile
from datetime import datetime

import requests
try:
    from PyQt6.QtCore import (
        Qt, QObject, QThread, QThreadPool, QRunnable, QSize, QRect, QPoint,
        QTimer, pyqtSignal, pyqtSlot,
    )
    from PyQt6.QtGui import QPixmap, QImage
    from PyQt6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout,
        QHBoxLayout, QScrollArea, QFrame, QStackedWidget, QDialog, QLineEdit,
        QProgressBar, QFileDialog, QMessageBox, QSizePolicy, QLayout, QCheckBox,
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

# Bundled fallback manifest, looked up next to this file / the exe.
LOCAL_MANIFEST = "games.json"
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
RELEASES_API = "https://api.github.com/repos/KevinAwesomeCoding/Game-Downloader/releases/latest"

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
        return os.path.dirname(sys.executable)
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
    """Return (major, minor, patch) ints from a tag like 'v1.2.3' or 'v1.2.3-20260624-42'."""
    core = tag.lstrip("v").split("-")[0]
    parts = core.split(".")
    try:
        return tuple(int(p) for p in parts[:3])
    except ValueError:
        return (0, 0, 0)


def _launch_update_bat(new_exe: str, current_exe: str) -> None:
    """Write and launch a detached .bat that waits for this process to exit,
    atomically replaces current_exe with new_exe, relaunches, then self-deletes.

    On Windows a running exe cannot be overwritten directly, so the bat polls
    for the PID to disappear before issuing the move.
    """
    pid = os.getpid()
    fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="gameupdate_")
    os.close(fd)
    script = (
        "@echo off\n"
        ":wait\n"
        f'tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL\n'
        "if not errorlevel 1 (\n"
        "    timeout /t 1 /nobreak >nul\n"
        "    goto wait\n"
        ")\n"
        f'move /y "{new_exe}" "{current_exe}"\n'
        f'start "" "{current_exe}"\n'
        'del "%~f0"\n'
    )
    with open(bat_path, "w", encoding="ascii") as fh:
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
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_prefs(data: dict):
    d = _prefs_dir()
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "prefs.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass


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


# ---------------------------------------------------------------------------
# Microsoft Defender exclusion helpers
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


def add_defender_exclusion(folder: str) -> tuple:
    """Add *folder* to Windows Defender's ExclusionPath.

    Strategy
    --------
    1. Skip silently if the path is already excluded.
    2. Attempt a non-elevated PowerShell Add-MpPreference (succeeds when the
       app is already running as administrator).
    3. On access-denied, retry with an elevated PowerShell script launched
       via ShellExecuteExW + runas.  A temp result file lets the elevated
       process pass its outcome back to this process.

    Returns (success: bool, message: str).
    """
    if defender_exclusion_exists(folder):
        return (True, "This folder is already excluded — no changes needed.")

    ps_path = _ps_escape(folder)
    command = f"Add-MpPreference -ExclusionPath '{ps_path}'"

    # Non-elevated attempt — works when the app is already running as admin.
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=15,
            creationflags=_NO_WINDOW,
        )
        if result.returncode == 0:
            return (True, "Defender exclusion added successfully.")
        stderr = (result.stderr or result.stdout or "").strip()
        access_keywords = (
            "0x80070005", "Access", "privilege", "administrator",
            "Unauthorized", "UnauthorizedAccess",
        )
        if not any(kw in stderr for kw in access_keywords):
            # A non-permission error — no point retrying elevated.
            return (False, stderr or "PowerShell returned a non-zero exit code.")
    except subprocess.TimeoutExpired:
        return (False, "PowerShell timed out while adding the exclusion.")
    except FileNotFoundError:
        return (False, "PowerShell was not found on this system.")

    # Elevated attempt — UAC prompt will appear.
    result_path = os.path.join(
        tempfile.gettempdir(), f"defex_result_{os.getpid()}.txt"
    )
    rp_esc = _ps_escape(result_path)
    script = (
        f"try {{\n"
        f"    Add-MpPreference -ExclusionPath '{ps_path}'\n"
        f"    Set-Content -LiteralPath '{rp_esc}' -Value 'OK'\n"
        f"}} catch {{\n"
        f"    Set-Content -LiteralPath '{rp_esc}' -Value \"FAIL: $_\"\n"
        f"}}\n"
    )

    exit_code = _run_elevated_ps(script)

    if exit_code == -1:
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
        return (True, "Defender exclusion added successfully.")
    prefix = "FAIL: "
    detail = text[len(prefix):].strip() if text.startswith(prefix) else text
    return (False, detail or "Unknown error from the elevated process.")


def add_defender_exclusions(folders: list) -> tuple:
    """Add multiple folders to Defender exclusions in a single PowerShell call.

    Filters out already-excluded paths first so it only touches what's needed.
    Falls back to a single UAC-elevated script if the non-elevated attempt is
    denied — the user sees at most one UAC prompt regardless of how many folders
    are being added.

    Returns (success: bool, message: str).
    """
    to_add = [f for f in folders if not defender_exclusion_exists(f)]
    if not to_add:
        return (True, "All folders are already excluded — no changes needed.")

    # PowerShell accepts a comma-separated array: -ExclusionPath 'a','b'
    paths_ps = ",".join(f"'{_ps_escape(f)}'" for f in to_add)
    command = f"Add-MpPreference -ExclusionPath {paths_ps}"

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=15,
            creationflags=_NO_WINDOW,
        )
        if result.returncode == 0:
            return (True, "Defender exclusions added successfully.")
        stderr = (result.stderr or result.stdout or "").strip()
        access_keywords = (
            "0x80070005", "Access", "privilege", "administrator",
            "Unauthorized", "UnauthorizedAccess",
        )
        if not any(kw in stderr for kw in access_keywords):
            return (False, stderr or "PowerShell returned a non-zero exit code.")
    except subprocess.TimeoutExpired:
        return (False, "PowerShell timed out while adding exclusions.")
    except FileNotFoundError:
        return (False, "PowerShell was not found on this system.")

    # Elevated attempt — one UAC prompt covers all folders.
    result_path = os.path.join(
        tempfile.gettempdir(), f"defex_result_{os.getpid()}.txt"
    )
    rp_esc = _ps_escape(result_path)
    script = (
        f"try {{\n"
        f"    Add-MpPreference -ExclusionPath {paths_ps}\n"
        f"    Set-Content -LiteralPath '{rp_esc}' -Value 'OK'\n"
        f"}} catch {{\n"
        f"    Set-Content -LiteralPath '{rp_esc}' -Value \"FAIL: $_\"\n"
        f"}}\n"
    )

    exit_code = _run_elevated_ps(script)
    if exit_code == -1:
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
        return (True, "Defender exclusions added successfully.")
    prefix = "FAIL: "
    detail = text[len(prefix):].strip() if text.startswith(prefix) else text
    return (False, detail or "Unknown error from the elevated process.")


def remove_defender_exclusion(folder: str) -> tuple:
    """Remove *folder* from Defender's ExclusionPath.

    No UAC fallback — the app already runs elevated via --uac-admin so the
    PowerShell subprocess inherits that and can call Remove-MpPreference
    directly.  Failure is non-fatal; the exclusion simply stays in place.

    Returns (success: bool, message: str).
    """
    if not defender_exclusion_exists(folder):
        return (True, "Folder was not in the exclusion list.")

    ps_path = _ps_escape(folder)
    command = f"Remove-MpPreference -ExclusionPath '{ps_path}'"
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=15,
            creationflags=_NO_WINDOW,
        )
        if result.returncode == 0:
            _log.debug("remove_defender_exclusion  OK  folder=%r", folder)
            return (True, "Defender exclusion removed successfully.")
        stderr = (result.stderr or result.stdout or "").strip()
        _log.debug("remove_defender_exclusion  FAIL  folder=%r  stderr=%r", folder, stderr)
        return (False, stderr or "PowerShell returned a non-zero exit code.")
    except subprocess.TimeoutExpired:
        return (False, "PowerShell timed out while removing the exclusion.")
    except FileNotFoundError:
        return (False, "PowerShell was not found on this system.")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
class Game:
    """A single game entry parsed from the manifest."""

    def __init__(self, raw: dict):
        self.id = str(raw.get("id", "")).strip()
        self.name = str(raw.get("name", "Untitled")).strip()
        self.description = str(raw.get("description", "")).strip()
        self.version = str(raw.get("version", "")).strip()
        self.size = str(raw.get("size", "")).strip()
        self.players = str(raw.get("players", "")).strip()
        self.thumbnail = str(raw.get("thumbnail", "")).strip()
        self.zip_url = str(raw.get("zipUrl", "")).strip()
        self.fix_url = str(raw.get("fixZipUrl", "")).strip()
        self.entry_type = str(raw.get("type", "game")).strip().lower()
        _reqs = raw.get("requirements")
        self.requirements = _reqs if isinstance(_reqs, dict) else {}

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
    def safe_folder_name(self) -> str:
        """A filesystem-safe folder name derived from the game's name."""
        invalid = '<>:"/\\|?*'
        cleaned = "".join(c for c in self.name if c not in invalid).strip()
        return cleaned or (self.id or "Game")


def parse_manifest(text: str) -> list:
    """Parse manifest JSON text into a list of Game objects."""
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("Manifest must be a JSON array of games.")
    return [Game(item) for item in data if isinstance(item, dict)]


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

    def __init__(self, manifest_url: str):
        super().__init__()
        self.manifest_url = manifest_url

    @pyqtSlot()
    def run(self):
        local_path = os.path.join(app_dir(), LOCAL_MANIFEST)

        # 1) Try the remote manifest URL.
        if self.manifest_url:
            try:
                req = urllib.request.Request(
                    self.manifest_url,
                    headers={"User-Agent": "GameInstaller/1.0"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw = resp.read().decode("utf-8")
                games = parse_manifest(raw)
                # Persist to disk so the next offline launch uses the latest list.
                try:
                    with open(local_path, "w", encoding="utf-8") as fh:
                        fh.write(raw)
                except OSError:
                    pass  # write failure is non-fatal; we already have the data
                self.loaded.emit(games, "remote manifest")
                return
            except Exception:
                pass  # network or parse error — fall through to local copy

        # 2) Local games.json fallback (bundled copy or last successful download).
        try:
            with open(local_path, "r", encoding="utf-8") as fh:
                games = parse_manifest(fh.read())
            label = "local games.json"
            if self.manifest_url:
                label = "local games.json (remote unavailable)"
            self.loaded.emit(games, label)
        except FileNotFoundError:
            self.failed.emit(
                "Could not reach the game list server and no local copy was found.\n\n"
                "Check your internet connection and try Refresh, or re-download "
                "the installer to restore the bundled game list."
            )
        except json.JSONDecodeError as exc:
            self.failed.emit(f"games.json is not valid JSON:\n{exc}")
        except Exception as exc:
            self.failed.emit(f"Failed to load games:\n{exc}")


# ---------------------------------------------------------------------------
# Defender exclusion worker — runs add_defender_exclusion off the GUI thread
# so UAC + PowerShell never freeze the window.
# ---------------------------------------------------------------------------
class DefenderWorker(QObject):
    finished = pyqtSignal(bool, str)  # success, message

    def __init__(self, folders):
        super().__init__()
        # Accept a single path string or a list of paths.
        self.folders = [folders] if isinstance(folders, str) else list(folders)

    @pyqtSlot()
    def run(self):
        success, message = add_defender_exclusions(self.folders)
        self.finished.emit(success, message)


# ---------------------------------------------------------------------------
# Self-update workers
# ---------------------------------------------------------------------------
class UpdateChecker(QObject):
    """Fetches the latest GitHub release and signals if a newer version exists."""
    update_available = pyqtSignal(str, str)  # tag_name, exe_download_url

    @pyqtSlot()
    def run(self):
        # Step 1 — app starts: log the current VERSION value.
        _ulog.info("[1] App started. Current VERSION = %r", VERSION)
        _log.debug("UpdateChecker: runtime VERSION = %r", VERSION)
        try:
            # Step 2 — GitHub API request: log the URL being called.
            _ulog.info("[2] GitHub API request  URL = %s", RELEASES_API)
            resp = requests.get(
                RELEASES_API,
                timeout=(5, 10),
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "GameInstaller-updater",
                },
            )
            # Step 3 — response received: log raw tag_name and HTTP status.
            _log.debug("UpdateChecker: HTTP status = %s", resp.status_code)
            resp.raise_for_status()
            data = resp.json()
            tag = data.get("tag_name", "")
            _ulog.info(
                "[3] GitHub API response  status=%s  tag_name=%r",
                resp.status_code, tag,
            )
            _log.debug("UpdateChecker: tag_name from API = %r", tag)

            if not tag:
                _ulog.warning(
                    "[3] No tag_name in response — aborting update check. "
                    "Payload keys: %s", list(data.keys()),
                )
                _log.debug("UpdateChecker: no tag_name in response — aborting. "
                            "Full payload keys: %s", list(data.keys()))
                return

            local_v = _parse_version(VERSION)
            remote_v = _parse_version(tag)
            is_newer = remote_v > local_v
            # Step 4 — version comparison: log both versions and the result.
            _ulog.info(
                "[4] Version comparison  local=%s  remote=%s  update_needed=%s",
                VERSION, tag, "yes" if is_newer else "no",
            )
            _log.debug(
                "UpdateChecker: comparing remote %s vs local %s -> remote_is_newer=%s",
                remote_v, local_v, is_newer,
            )

            if not is_newer:
                return

            exe_url = next(
                (a["browser_download_url"] for a in data.get("assets", [])
                 if a.get("name", "").endswith(".exe")),
                None,
            )
            _log.debug("UpdateChecker: matched exe asset url = %r", exe_url)
            if exe_url:
                # Step 5 — update dialog will be shown.
                _ulog.info(
                    "[5] Update dialog will be shown for tag=%r  exe_url=%r",
                    tag, exe_url,
                )
                self.update_available.emit(tag, exe_url)
            else:
                _ulog.warning(
                    "[5] Newer tag %r found but no .exe asset on release — "
                    "dialog will NOT show. Asset names: %s",
                    tag, [a.get("name") for a in data.get("assets", [])],
                )
                _log.debug(
                    "UpdateChecker: newer tag found but no .exe asset attached to "
                    "the release — dialog will not show. Asset names: %s",
                    [a.get("name") for a in data.get("assets", [])],
                )
        except Exception:
            # Step 9 — exception anywhere in the update flow: log full traceback.
            _ulog.exception("[9] Exception in UpdateChecker.run — full traceback:")
            _log.exception("UpdateChecker: update check failed with an exception")
            # still fail silently to the UI — update check must never affect
            # normal operation — but now it's logged instead of invisible.


class UpdateDownloadWorker(QObject):
    """Downloads the new release exe to a temp file."""
    progress = pyqtSignal(int)   # 0-100, or -1 for indeterminate
    success = pyqtSignal(str)    # path to downloaded temp file
    error = pyqtSignal(str) #test again

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    @pyqtSlot()
    def run(self):
        # Step 6 — download starts: log the download URL.
        _ulog.info("[6] Download started  url=%s", self.url)
        try:
            fd, dest = tempfile.mkstemp(suffix=".exe", prefix="GameInstaller_update_")
            os.close(fd)
            with requests.get(self.url, stream=True, timeout=REQUEST_TIMEOUT) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                with open(dest, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        done += len(chunk)
                        self.progress.emit(int(done * 100 / total) if total else -1)
            self.progress.emit(100)
            self.success.emit(dest)
        except Exception as exc:
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
                    cancel=None) -> bool:
    """Download a *public* Google Drive file by id, handling the large-file
    virus-scan interstitial automatically.

    Returns True on success, False on any failure — network error, per-file
    download quota, or the server handing us the HTML warning page instead of
    the real file (in which case no bad file is left on disk).

    progress_callback(bytes_downloaded, total_bytes): total_bytes is 0 when the
    server doesn't report a Content-Length.
    Optional: api_key enables the Drive-API fallback; cancel may be a
    threading.Event or a zero-arg callable returning True to abort.
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
        total = resp.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else 0
        done = 0
        first = True
        with open(dest_path, "wb") as out:
            for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK):
                if cancelled():
                    return False
                if not chunk:
                    continue
                if first:
                    first = False
                    if _looks_like_html(chunk[:64]):
                        return False          # headers lied; it's the warning page
                out.write(chunk)
                done += len(chunk)
                if progress_callback is not None:
                    progress_callback(done, total)
        return done > 0

    def fetch(url, params=None):
        """-> ('file', ok) after streaming, ('html', text), or ('error', None)."""
        try:
            with session.get(url, params=params, stream=True,
                             timeout=REQUEST_TIMEOUT, allow_redirects=True) as resp:
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
        return _validate_not_html(dest_path)
    html = payload if kind == "html" else ""

    # 2) scrape the interstitial form and resubmit (carries the required uuid/at)
    if is_quota(html):
        return False                          # per-file quota: no method fixes this
    action, fields = _parse_gdrive_form(html)
    if not action:
        kind, payload = fetch("https://drive.google.com/uc",
                              {"export": "download", "id": file_id})
        if kind == "file" and payload:
            return _validate_not_html(dest_path)
        html = payload if kind == "html" else ""
        if is_quota(html):
            return False
        action, fields = _parse_gdrive_form(html)
    if action:
        kind, payload = fetch(action, fields)
        if kind == "file" and payload:
            return _validate_not_html(dest_path)

    # 3) Drive API alt=media — most stable transport, needs api_key + public file
    if api_key:
        kind, payload = fetch(f"https://www.googleapis.com/drive/v3/files/{file_id}",
                              {"alt": "media", "key": api_key})
        if kind == "file" and payload:
            return _validate_not_html(dest_path)

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

    def __init__(self, game: Game, dest_path: str, fix_only: bool = False,
                 remove_temp_exclusion: bool = False):
        super().__init__()
        self.game = game
        self.dest_path = dest_path
        self._fix_only = fix_only
        self._remove_temp_exclusion = remove_temp_exclusion
        self._cancel = threading.Event()
        self._temp_files = []
        self._temp_dirs = []
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
    def _new_temp_archive(self, url: str) -> str:
        # Give the temp file the right extension so 7-Zip / zipfile and any
        # signature-agnostic tooling behave predictably.
        low = url.lower().split("?")[0]
        if low.endswith(".rar"):
            suffix = ".rar"
        elif low.endswith(".7z"):
            suffix = ".7z"
        else:
            suffix = ".zip"
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="gameinstall_")
        os.close(fd)
        self._temp_files.append(path)
        return path

    def _new_temp_dir(self) -> str:
        path = tempfile.mkdtemp(prefix="gamefix_")
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
        """Clean temp files then, if the %TEMP% Defender exclusion was added
        for this install, remove it now that extraction is fully done.
        Clears the flag so a subsequent call (e.g. from the finally guard in
        run()) is a no-op."""
        self._cleanup_temp()
        if self._remove_temp_exclusion:
            _log.debug("_finish_cleanup  removing %%TEMP%% exclusion  path=%r",
                       tempfile.gettempdir())
            self._remove_temp_exclusion = False
            remove_defender_exclusion(tempfile.gettempdir())

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
                fix_archive = self._new_temp_archive(self.game.fix_url)
                self._stage_start = time.monotonic()
                if not self._download(self.game.fix_url, fix_archive,
                                      "Downloading fix", self.fix_download_progress,
                                      stage_idx=2):
                    return
                if self._cancelled():
                    return
                self._stage_start = time.monotonic()
                if not self._apply_fix(fix_archive, self.dest_path,
                                       self.game.fix_url, stage_idx=3):
                    return
                self._remove_file(fix_archive)
                if self._cancelled():
                    return
                self._finish_cleanup()
                elapsed = time.monotonic() - self._start_time
                self.elapsed_text.emit(self._fmt_time(elapsed))
                self.status.emit(f"Fix applied in {self._fmt_time(elapsed)}")
                self.overall_progress.emit(100)
                self.success.emit(self.dest_path)
                return

            if not self.game.zip_url:
                self.error.emit("This game has no download URL configured.")
                return
            if not self._prepare_destination():
                return

            # --- 1) Main game: download then extract ------------------------
            main_archive = self._new_temp_archive(self.game.zip_url)
            self._stage_start = time.monotonic()
            if not self._download(self.game.zip_url, main_archive,
                                  "Downloading game", self.download_progress, stage_idx=0):
                return
            if self._cancelled():
                return
            self._stage_start = time.monotonic()
            # Extract to staging so wrapper-folder detection can run before
            # any files land in the install folder.
            main_staging = self._new_temp_dir()
            if not self._extract_archive(main_archive, main_staging,
                                         self.game.zip_url, self.extract_progress, stage_idx=1):
                return
            self._remove_file(main_archive)
            if self._cancelled():
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

            # --- 2) Optional fix / repair patch -----------------------------
            if self.game.has_fix:
                _log.debug(
                    "run FIX BRANCH  id=%r  name=%r  fix_url=%r",
                    self.game.id, self.game.name, self.game.fix_url,
                )
                fix_archive = self._new_temp_archive(self.game.fix_url)
                _log.debug("run FIX ARCHIVE  path=%s", fix_archive)
                self._stage_start = time.monotonic()
                if not self._download(self.game.fix_url, fix_archive,
                                      "Downloading fix", self.fix_download_progress, stage_idx=2):
                    return
                if self._cancelled():
                    return
                self._stage_start = time.monotonic()
                if not self._apply_fix(fix_archive, self.dest_path, self.game.fix_url, stage_idx=3):
                    return
                self._remove_file(fix_archive)
                if self._cancelled():
                    return

            # --- Done -------------------------------------------------------
            self._finish_cleanup()
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
    def _download(self, url, dest_file, status_text, progress_signal, stage_idx=0) -> bool:
        _log.debug("_download ENTER  stage=%d  url=%s", stage_idx, url)
        progress_signal.emit(0)
        self._stage_progress[stage_idx] = 0
        self._update_overall_progress()
        self._emit_status(status_text, -1)
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

        # Google Drive links route through the dedicated handler, which deals
        # with the large-file virus-scan interstitial and validates that we got
        # the file (not the HTML warning page). All other URLs fall through to
        # the generic streaming path below. (_gdrive_confirm is retained above
        # for rollback but no longer called.)
        if _is_gdrive_url(url):
            file_id = _gdrive_file_id(url)
            _log.debug("_download GDRIVE route  file_id=%r  url=%s", file_id, url)
            if not file_id:
                self._cleanup_temp()
                self.error.emit("Could not read the Google Drive file ID from the link.")
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
                    progress_signal.emit(pct)
                    self._stage_progress[stage_idx] = pct
                    speed = done / elapsed if elapsed > 0 else 0
                    remaining = total - done
                    eta = remaining / speed if speed > 0 else 0
                    speed_str = f"{speed / 1e6:.1f} MB/s" if speed > 0 else "calculating..."
                    eta_str = self._fmt_time(eta) if eta > 0 else "calculating..."
                    bytes_str = f"{self._fmt_bytes(done)} / {self._fmt_bytes(total)}"
                    self.speed_text.emit(speed_str)
                    self.eta_text.emit(f"{eta_str} remaining")
                    self._emit_status(status_text, pct, bytes_str)
                    self._update_overall_progress()
                else:
                    progress_signal.emit(-1)  # indeterminate
                    self._emit_status(status_text, -1, f"{self._fmt_bytes(done)}")

            try:
                ok = download_gdrive(file_id, dest_file, _gd_progress,
                                     session=session,
                                     cancel=self._cancel,
                                     api_key=GDRIVE_API_KEY)
            except OSError as exc:
                _log.debug("_download GDRIVE OSError  stage=%d  %s", stage_idx, exc)
                self._cleanup_temp()
                self.error.emit(f"Could not write the downloaded file:\n{exc}")
                return False

            if self._cancel.is_set():
                self._abort()
                return False
            if not ok:
                self._cleanup_temp()
                self.error.emit(
                    "Google Drive download failed.\n\n"
                    "The file may be private, the per-file download quota may be "
                    "exceeded, or Google returned its virus-scan warning page "
                    "instead of the file. Please try again later."
                )
                return False

            progress_signal.emit(100)
            self._stage_progress[stage_idx] = 100
            self._update_overall_progress()
            try:
                with open(dest_file, "rb") as _probe:
                    _first16 = _probe.read(16)
                _log.debug("_download GDRIVE DONE  stage=%d  first16=%s (%r)",
                           stage_idx, _first16.hex(), _first16[:16])
            except OSError:
                pass
            return True

        try:
            with session.get(url, stream=True, timeout=REQUEST_TIMEOUT) as resp:
                _log.debug(
                    "_download RESPONSE  status=%d  content-type=%r  "
                    "content-length=%r  url-after-redirects=%s",
                    resp.status_code,
                    resp.headers.get("Content-Type"),
                    resp.headers.get("Content-Length"),
                    resp.url,
                )
                resp.raise_for_status()
                total = resp.headers.get("Content-Length")
                total = int(total) if total and total.isdigit() else 0
                done = 0
                stage_start = time.monotonic()
                last_update = stage_start

                with open(dest_file, "wb") as out:
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
                            progress_signal.emit(pct)
                            self._stage_progress[stage_idx] = pct
                            # Compute speed and ETA
                            speed = done / elapsed if elapsed > 0 else 0
                            remaining = total - done
                            eta = remaining / speed if speed > 0 else 0
                            speed_str = f"{speed / 1e6:.1f} MB/s" if speed > 0 else "calculating..."
                            eta_str = self._fmt_time(eta) if eta > 0 else "calculating..."
                            bytes_str = f"{self._fmt_bytes(done)} / {self._fmt_bytes(total)}"
                            self.speed_text.emit(speed_str)
                            self.eta_text.emit(f"{eta_str} remaining")
                            self._emit_status(status_text, pct, bytes_str)
                            self._update_overall_progress()
                        else:
                            progress_signal.emit(-1)  # indeterminate
                            self._emit_status(status_text, -1, f"{self._fmt_bytes(done)}")

            progress_signal.emit(100)
            self._stage_progress[stage_idx] = 100
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

        except requests.exceptions.MissingSchema:
            _log.debug("_download ERROR  stage=%d  MissingSchema  url=%s", stage_idx, url)
            self._cleanup_temp()
            self.error.emit("The download URL is invalid.")
            return False
        except requests.exceptions.InvalidURL:
            _log.debug("_download ERROR  stage=%d  InvalidURL  url=%s", stage_idx, url)
            self._cleanup_temp()
            self.error.emit("The download URL is invalid.")
            return False
        except requests.exceptions.ConnectionError as exc:
            _log.debug("_download ERROR  stage=%d  ConnectionError  %s", stage_idx, exc)
            self._cleanup_temp()
            self.error.emit(
                "Network error: could not reach the download server.\n"
                "Check your connection and try again."
            )
            return False
        except requests.exceptions.Timeout as exc:
            _log.debug("_download ERROR  stage=%d  Timeout  %s", stage_idx, exc)
            self._cleanup_temp()
            self.error.emit("The download timed out. Please try again.")
            return False
        except requests.exceptions.HTTPError as exc:
            _log.debug("_download ERROR  stage=%d  HTTPError  %s", stage_idx, exc)
            self._cleanup_temp()
            self.error.emit(f"Download failed (server returned an error):\n{exc}")
            return False
        except requests.exceptions.RequestException as exc:
            _log.debug("_download ERROR  stage=%d  RequestException  %s", stage_idx, exc)
            self._cleanup_temp()
            self.error.emit(f"Download failed:\n{exc}")
            return False
        except OSError as exc:
            _log.debug("_download ERROR  stage=%d  OSError  %s", stage_idx, exc)
            self._cleanup_temp()
            self.error.emit(f"Could not write the downloaded file:\n{exc}")
            return False

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

                    pct = int(index * 100 / count)
                    progress_signal.emit(pct)
                    self._stage_progress[stage_idx] = pct
                    if index % max(1, count // 20) == 0:  # Update 20 times
                        self._emit_status("Extracting", pct, f"{index} / {count} files")
                        self._update_overall_progress()

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
            shutil.copy2(src, target)  # overwrites if it already exists
            pct = int(index * 100 / total)
            _sig.emit(pct)
            self._stage_progress[stage_idx] = pct
            if index % max(1, total // 20) == 0:  # Update 20 times
                self._emit_status(status_text, pct, f"{index} / {total} files")
                self._update_overall_progress()

        _sig.emit(100)
        self._stage_progress[stage_idx] = 100
        self._update_overall_progress()
        return True


# ---------------------------------------------------------------------------
# Game card widget
# ---------------------------------------------------------------------------
class GameCard(QFrame):
    CARD_W = 280
    THUMB_H = 130

    def __init__(self, game: Game, on_click):
        super().__init__()
        self.game = game
        self._on_click = on_click
        self.setObjectName("GameCard")
        self.setFixedWidth(self.CARD_W)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 12)
        root.setSpacing(10)

        self.thumb = QLabel()
        self.thumb.setObjectName("Thumb")
        self.thumb.setFixedHeight(self.THUMB_H)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setText("Loading image…")
        root.addWidget(self.thumb)

        body = QVBoxLayout()
        body.setContentsMargins(14, 0, 14, 0)
        body.setSpacing(6)

        title = QLabel(game.name)
        title.setObjectName("CardTitle")
        title.setWordWrap(True)
        body.addWidget(title)

        desc = QLabel(game.description)
        desc.setObjectName("CardDesc")
        desc.setWordWrap(True)
        body.addWidget(desc)

        meta_bits = []
        if game.version:
            meta_bits.append(f"v{game.version}")
        meta_bits.append(game.size if game.size else "Size unknown")
        if game.has_fix:
            meta_bits.append("+ patch")
        meta = QLabel("  •  ".join(meta_bits))
        meta.setObjectName("CardMeta")
        body.addWidget(meta)

        if game.players:
            players_lbl = QLabel(f"👥 {game.players}")
            players_lbl.setObjectName("CardPlayers")
            players_lbl.setWordWrap(True)
            body.addWidget(players_lbl)

        if game.is_patch:
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
        pix = QPixmap.fromImage(image).scaled(
            self.CARD_W, self.THUMB_H,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = max(0, (pix.width() - self.CARD_W) // 2)
        y = max(0, (pix.height() - self.THUMB_H) // 2)
        self.thumb.setPixmap(pix.copy(x, y, self.CARD_W, self.THUMB_H))

    def _show_placeholder(self):
        self.thumb.setPixmap(QPixmap())
        self.thumb.setText(self.game.name)
        self.thumb.setProperty("placeholder", True)
        self.thumb.style().unpolish(self.thumb)
        self.thumb.style().polish(self.thumb)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_click(self.game)
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# Install dialog — owns the worker thread for one install.
# ---------------------------------------------------------------------------
class InstallDialog(QDialog):
    def __init__(self, game: Game, parent=None, *, patch_target: str | None = None,
                 manager=None):
        super().__init__(parent)
        self.game = game
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

        title_row = QHBoxLayout()
        title = QLabel(game.name)
        title.setObjectName("DialogTitle")
        title_row.addWidget(title)
        title_row.addStretch()
        self.fix_only_btn = QPushButton("Download Just Fix")
        self.fix_only_btn.setObjectName("FixOnlyButton")
        self.fix_only_btn.setVisible(game.has_fix)
        self.fix_only_btn.clicked.connect(self._start_fix_only)
        _log.debug(
            "InstallDialog.__init__  id=%r  name=%r  has_fix=%s  fix_url=%r",
            game.id, game.name, game.has_fix, game.fix_url,
        )
        title_row.addWidget(self.fix_only_btn)
        root.addLayout(title_row)

        desc = QLabel(game.description)
        desc.setObjectName("CardDesc")
        desc.setWordWrap(True)
        root.addWidget(desc)

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

        # "Will it run on your PC?" — compared against this game's requirements.
        self._build_system_check(root)

        # Destination row — hidden for patch installs (folder already chosen).
        self._dest_label = self._label("Install location", "SectionLabel")
        root.addWidget(self._dest_label)
        dest_row = QHBoxLayout()
        self.dest_edit = QLineEdit()
        self.dest_edit.setPlaceholderText("Choose a folder to install into…")
        _saved = load_prefs().get("install_path", "")
        self.dest_edit.setText(_saved or os.path.join(os.path.expanduser("~"), "Games"))
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

        # Optional Defender exclusion.
        self.defender_check = QCheckBox(
            "Add this install folder to Microsoft Defender exclusions  "
            "(Admin required)"
        )
        self.defender_check.setChecked(True)
        root.addWidget(self.defender_check)
        self.defender_warn = QLabel(
            "⚠  This reduces antivirus scanning for this folder."
        )
        self.defender_warn.setObjectName("DefenderWarning")
        root.addWidget(self.defender_warn)

        if patch_target:
            self._dest_label.setVisible(False)
            self._dest_row_widget.setVisible(False)
            self.defender_check.setVisible(False)
            self.defender_warn.setVisible(False)
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
        install_label = "Apply Fix" if patch_target else "Execute / Install"
        self.install_btn = QPushButton(install_label)
        self.install_btn.setObjectName("PrimaryButton")
        self.install_btn.clicked.connect(self._start_install)
        btn_row.addWidget(self.install_btn)
        root.addLayout(btn_row)

        if not game.is_available:
            self.install_btn.setEnabled(False)
            self.install_btn.setText("Coming Soon")
            self.status_label.setText("This game isn't available to download yet.")

    # -- ui helpers ---------------------------------------------------------
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
        default_dest = (load_prefs().get("install_path", "")
                        or os.path.join(os.path.expanduser("~"), "Games"))
        path_for_storage = self._patch_target or default_dest
        result = evaluate_requirements(self.game.requirements, path_for_storage)

        frame = QFrame()
        frame.setObjectName("SysCheckBox")
        box = QVBoxLayout(frame)
        box.setContentsMargins(14, 10, 14, 12)
        box.setSpacing(6)

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
        if CONFIG["default_install_subfolder"]:
            return os.path.join(base, self.game.safe_folder_name)
        return base

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

        # Hand the job to the download manager and close — it runs in the
        # background on the Downloads page (and applies the Defender exclusion
        # there if requested, just like the old inline pre-step did).
        defender_folders = None
        if self.defender_check.isChecked():
            try:
                os.makedirs(target, exist_ok=True)
            except OSError as exc:
                QMessageBox.critical(
                    self, "Cannot create folder",
                    f"Could not create the install folder:\n{exc}",
                )
                return
            defender_folders = [target, tempfile.gettempdir()]

        if not self._patch_target:
            prefs = load_prefs()
            prefs["install_path"] = self.dest_edit.text().strip()
            save_prefs(prefs)

        self._manager.enqueue(self.game, target, fix_only=False,
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

    @pyqtSlot(bool, str)
    def _on_pre_install_defender_done(self, success: bool, message: str):
        self._teardown_defender_thread()
        if success:
            self.status_label.setText("Exclusion added. Starting download…")
            self._launch_install_worker(self._install_target, remove_temp_exclusion=True)
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
        self.defender_check.setEnabled(True)

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

    @pyqtSlot(bool, str)
    def _on_defender_done(self, ok, message):
        self._teardown_defender()
        self.action_btn.setEnabled(True)
        if ok:
            self.status_label.setText("Exclusion added. Starting download…")
            self._start_worker(remove_temp_exclusion=True)
        else:
            self._fail(f"Defender exclusion failed: {message}")

    def _start_worker(self, remove_temp_exclusion):
        self.thread = QThread()
        self.worker = InstallWorker(self.game, self.target, fix_only=self.fix_only,
                                    remove_temp_exclusion=remove_temp_exclusion)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
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
        self._set_chip("Done ✓", "Done")
        self.status_label.setText(f"Installed to {path}")
        self.speed_label.setText("")
        self.eta_label.setText("")
        self.action_btn.setText("Open folder")
        self.action_btn.setEnabled(True)
        self.manager.on_state_changed()

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
        self.action_btn.setText("Remove")
        self.action_btn.setEnabled(True)
        self.manager.on_state_changed()

    def _fail(self, message):
        self.state = "error"
        self._set_chip("Failed", "Error")
        self.status_label.setText(message)
        self.speed_label.setText("")
        self.eta_label.setText("")
        self.action_btn.setText("Remove")
        self.action_btn.setEnabled(True)
        self.manager.on_state_changed()

    # -- the context-sensitive button --------------------------------------
    def _action_clicked(self):
        if self.state == "queued":
            self.state = "canceled"
            self._set_chip("Canceled", "Canceled")
            self.status_label.setText("Canceled before starting.")
            self.action_btn.setText("Remove")
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
        card = DownloadCard(game, target, fix_only, defender_folders, self)
        self.cards.append(card)
        self.page.add_card(card)
        self.page.set_empty(False)
        _log.debug("DownloadManager.enqueue  id=%r  fix_only=%s  target=%r",
                   game.id, fix_only, target)
        self._pump()

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
        brow.setContentsMargins(24, 14, 24, 14)
        title = QLabel("Downloads")
        title.setObjectName("AppTitle")
        brow.addWidget(title)
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
        self.empty_label = QLabel("No downloads yet.\nPick a game from the Library to start one.")
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
    def __init__(self, tag: str, exe_url: str, parent=None):
        super().__init__(parent)
        self.tag = tag
        self.exe_url = exe_url
        self._thread = None
        self._worker = None
        self._new_exe: str | None = None

        self.setWindowTitle("Update available")
        self.setMinimumWidth(420)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        title = QLabel(f"Version {tag} is available")
        title.setObjectName("DialogTitle")
        root.addWidget(title)

        sub = QLabel(
            "A new version of Game Installer is ready.\n"
            "The app will restart automatically after downloading."
        )
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
        self.update_btn = QPushButton("Update")
        self.update_btn.setObjectName("PrimaryButton")
        self.update_btn.clicked.connect(self._start_download)
        btn_row.addWidget(self.update_btn)
        root.addLayout(btn_row)

    def _start_download(self):
        self.update_btn.setEnabled(False)
        self.later_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.status_label.setText("Downloading update…")
        self.status_label.setVisible(True)

        self._thread = QThread()
        self._worker = UpdateDownloadWorker(self.exe_url)
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
        self.status_label.setText("Download complete — restarting…")
        QTimer.singleShot(800, self._apply_update)

    @pyqtSlot(str)
    def _on_error(self, message):
        # Step 7 (dialog layer) — download failed signal received by the dialog.
        _ulog.error("[7] UpdateDialog received download error: %s", message)
        self._teardown_thread()
        self.status_label.setText(f"Update failed: {message}")
        self.later_btn.setEnabled(True)
        self.later_btn.setText("Close")

    def _apply_update(self):
        if not self._new_exe or not os.path.isfile(self._new_exe):
            _ulog.error(
                "[8] Replacement step skipped — downloaded file missing: %r",
                self._new_exe,
            )
            self.status_label.setText("Update file missing. Please restart manually.")
            return
        if not getattr(sys, "frozen", False):
            _ulog.warning(
                "[8] Replacement step skipped — running from source (not a frozen exe)."
            )
            self.status_label.setText(
                "Self-update is only supported for the .exe build."
            )
            self.later_btn.setEnabled(True)
            self.later_btn.setText("Close")
            return
        # Step 8 — replacement step: log old path and new path.
        _ulog.info(
            "[8] Replacement step running  new_exe=%r  current_exe=%r",
            self._new_exe, sys.executable,
        )
        try:
            _launch_update_bat(self._new_exe, sys.executable)
        except Exception:
            # Step 9 — exception during replacement.
            _ulog.exception("[9] Exception during _launch_update_bat — full traceback:")
            raise
        QApplication.quit()

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
        self._spacewar_check_done = False  # fires at most once per session
        self._update_thread = None
        self._update_worker = None
        self._current_games: list = []

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_header())

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.loading_page = self._build_loading_page()
        self.error_page = self._build_error_page()
        self.content_page = self._build_content_page()
        self.downloads_page = DownloadsPage()
        self.download_manager = DownloadManager(self.downloads_page, max_concurrent=3)
        self.download_manager.count_changed.connect(self._update_downloads_badge)
        self.downloads_page.set_empty(True)
        self.stack.addWidget(self.loading_page)
        self.stack.addWidget(self.error_page)
        self.stack.addWidget(self.content_page)
        self.stack.addWidget(self.downloads_page)

        self.load_manifest()
        # Defer update check so the window is painted before any network I/O.
        QTimer.singleShot(2000, self._start_update_check)

    def closeEvent(self, event):
        # Stop any in-flight downloads so no QThread is left running at exit.
        try:
            self.download_manager.shutdown()
        except Exception:
            pass
        if self._update_thread is not None:
            self._update_thread.quit()
            self._update_thread.wait()
        super().closeEvent(event)

    # -- self-update ----------------------------------------------------------
    def _start_update_check(self):
        self._update_thread = QThread()
        self._update_worker = UpdateChecker()
        self._update_worker.moveToThread(self._update_thread)
        self._update_thread.started.connect(self._update_worker.run)
        self._update_worker.update_available.connect(self._on_update_available)
        self._update_thread.finished.connect(self._update_thread.deleteLater)
        self._update_thread.start()

    @pyqtSlot(str, str)
    def _on_update_available(self, tag: str, exe_url: str):
        if self._update_thread is not None:
            self._update_thread.quit()
            self._update_thread.wait()
            self._update_worker.deleteLater()
            self._update_thread = None
            self._update_worker = None
        UpdateDialog(tag, exe_url, parent=self).exec()

    def _update_downloads_badge(self, active):
        self.downloads_btn.setText("Downloads" if active == 0 else f"Downloads ({active})")

    # -- header -------------------------------------------------------------
    def _build_header(self):
        header = QFrame()
        header.setObjectName("Header")
        lay = QHBoxLayout(header)
        lay.setContentsMargins(24, 16, 24, 16)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel(CONFIG["window_title"])
        title.setObjectName("AppTitle")
        title_box.addWidget(title)
        self.updated_label = QLabel("")
        self.updated_label.setObjectName("UpdatedLabel")
        title_box.addWidget(self.updated_label)
        lay.addLayout(title_box)
        lay.addStretch(1)

        self.library_btn = QPushButton("Library")
        self.library_btn.setObjectName("NavButton")
        self.library_btn.clicked.connect(
            lambda: self.stack.setCurrentWidget(self.content_page))
        lay.addWidget(self.library_btn)

        self.downloads_btn = QPushButton("Downloads")
        self.downloads_btn.setObjectName("NavButton")
        self.downloads_btn.clicked.connect(
            lambda: self.stack.setCurrentWidget(self.downloads_page))
        lay.addWidget(self.downloads_btn)

        self.spacewar_btn = QPushButton("Get Spacewar")
        self.spacewar_btn.setObjectName("SpacewarButton")
        self.spacewar_btn.setToolTip(
            "Install Spacewar (app 480) via Steam — enables online multiplayer "
            "for some games in this library"
        )
        self.spacewar_btn.clicked.connect(self._launch_spacewar_manual)
        lay.addWidget(self.spacewar_btn)

        self.refresh_btn = QPushButton("↻  Refresh")
        self.refresh_btn.setObjectName("PrimaryButton")
        self.refresh_btn.clicked.connect(self.load_manifest)
        lay.addWidget(self.refresh_btn)
        return header

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
        retry.clicked.connect(self.load_manifest)
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

        search_row = QWidget()
        sr_lay = QHBoxLayout(search_row)
        sr_lay.setContentsMargins(24, 14, 24, 10)
        self._search_bar = QLineEdit()
        self._search_bar.setPlaceholderText("Search games...")
        self._search_bar.setClearButtonEnabled(True)
        self._search_bar.textChanged.connect(self._on_search_changed)
        sr_lay.addWidget(self._search_bar)
        lay.addWidget(search_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setObjectName("Scroll")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        self.cards_layout = FlowLayout(container, margin=24, spacing=18)
        scroll.setWidget(container)
        lay.addWidget(scroll)
        return page

    # -- manifest loading ---------------------------------------------------
    def load_manifest(self):
        if self.manifest_thread is not None:
            return
        self.refresh_btn.setEnabled(False)
        self.stack.setCurrentWidget(self.loading_page)

        self.manifest_thread = QThread()
        self.manifest_worker = ManifestLoader(CONFIG["manifest_url"])
        self.manifest_worker.moveToThread(self.manifest_thread)
        self.manifest_thread.started.connect(self.manifest_worker.run)
        self.manifest_worker.loaded.connect(self._on_manifest_loaded)
        self.manifest_worker.failed.connect(self._on_manifest_failed)
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

    @pyqtSlot(list, str)
    def _on_manifest_loaded(self, games, source):
        self._teardown_manifest_thread()
        self._current_games = games
        self._search_bar.clear()
        self._populate_cards(games)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.updated_label.setText(f"Last updated {stamp}  ·  {source}")
        self.stack.setCurrentWidget(self.content_page)
        # Defer the Spacewar check until after the cards are painted.
        QTimer.singleShot(400, self._maybe_prompt_spacewar)

    @pyqtSlot(str)
    def _on_manifest_failed(self, message):
        self._teardown_manifest_thread()
        self.error_label.setText(message)
        self.stack.setCurrentWidget(self.error_page)

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

    def _on_search_changed(self, text: str):
        q = text.strip().lower()
        filtered = (
            [g for g in self._current_games if q in g.name.lower()]
            if q else self._current_games
        )
        self._populate_cards(filtered)

    def _populate_cards(self, games):
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not games:
            searching = self._search_bar.text().strip() if hasattr(self, "_search_bar") else ""
            msg = "No games match your search." if searching else "No games are available right now."
            placeholder = QLabel(msg)
            placeholder.setObjectName("SubInfo")
            self.cards_layout.addWidget(placeholder)
            return

        for game in games:
            self.cards_layout.addWidget(GameCard(game, self._open_install))

    def _open_install(self, game: Game):
        _log.debug(
            "_open_install  id=%r  name=%r  type=%r  zip_url=%r  fix_url=%r",
            game.id, game.name, game.entry_type, game.zip_url, game.fix_url,
        )
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
            if dlg.exec():
                self.stack.setCurrentWidget(self.downloads_page)
            return

        # Normal (non-fix) game — unchanged.
        dlg = InstallDialog(game, self, manager=self.download_manager)
        # Dialog is now just a configure-and-queue step; accepted == enqueued,
        # so jump to the Downloads page to show progress.
        if dlg.exec():
            self.stack.setCurrentWidget(self.downloads_page)


# ---------------------------------------------------------------------------
# Styling — dark navy/charcoal theme with a teal accent.
# ---------------------------------------------------------------------------
STYLE = """
QWidget {
    background-color: #0f1320;
    color: #e6e9f0;
    font-family: 'Segoe UI', sans-serif;
    font-size: 14px;
}
/* Labels paint transparently so the card surface shows through; specific
   id-selectors below re-add their own backgrounds. */
QLabel { background-color: transparent; }

#Header { background-color: #141a2b; border-bottom: 1px solid #232b40; }
#AppTitle { font-size: 22px; font-weight: 700; color: #ffffff; }
#UpdatedLabel { color: #8b93a7; font-size: 12px; }

#Scroll { border: none; }

#GameCard {
    background-color: #171d2e;
    border: 1px solid #232b40;
    border-radius: 14px;
}
#GameCard:hover { border: 1px solid #2dd4bf; background-color: #1c2336; }
#Thumb {
    background-color: #0b0f1a;
    border-top-left-radius: 14px;
    border-top-right-radius: 14px;
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
#SectionLabel { font-size: 13px; font-weight: 600; color: #c4cad8; margin-top: 4px; }
#SmallLabel { color: #8b93a7; font-size: 12px; }
#StatusLabel { color: #c4cad8; font-size: 13px; }
#MetricLabel { color: #2dd4bf; font-size: 12px; font-weight: 500; }
#DefenderWarning { color: #f59e0b; font-size: 12px; }

#BigInfo { font-size: 34px; font-weight: 700; color: #ffffff; }
#SubInfo { color: #aeb4c6; font-size: 15px; }

QLineEdit {
    background-color: #0b0f1a;
    border: 1px solid #232b40;
    border-radius: 8px;
    padding: 8px 10px;
    selection-background-color: #2dd4bf;
    selection-color: #06231f;
}
QLineEdit:focus { border: 1px solid #2dd4bf; }

QPushButton {
    background-color: #232b40;
    border: 1px solid #2e3750;
    border-radius: 8px;
    padding: 8px 16px;
    color: #e6e9f0;
}
QPushButton:hover { background-color: #2e3750; }
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
    padding: 7px 14px; font-weight: 600;
}
#NavButton:hover { background-color: #1a2036; color: #ffffff; }

/* Downloads page */
#DownloadsBar { background-color: #11152340; border-bottom: 1px solid #1c2336; }
#DownloadCard {
    background-color: #161b2e; border: 1px solid #232a42; border-radius: 12px;
}
#DownloadName { font-size: 16px; font-weight: 700; color: #ffffff; }

#ChipQueued   { color: #8b93a7; font-size: 12px; font-weight: 700; padding: 2px 8px; }
#ChipRunning  { color: #2dd4bf; font-size: 12px; font-weight: 700; padding: 2px 8px; }
#ChipDone     { color: #22c55e; font-size: 12px; font-weight: 700; padding: 2px 8px; }
#ChipError    { color: #f87171; font-size: 12px; font-weight: 700; padding: 2px 8px; }
#ChipCanceled { color: #fbbf24; font-size: 12px; font-weight: 700; padding: 2px 8px; }
"""


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(CONFIG["window_title"])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)

    # Let in-flight thumbnail fetches finish before the interpreter tears down,
    # so Qt-pool threads are never running Python during finalizations much
    app.aboutToQuit.connect(lambda: QThreadPool.globalInstance().waitForDone(3000))

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main() 