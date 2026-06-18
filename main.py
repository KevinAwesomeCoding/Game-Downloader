"""
Game Installer — a lightweight Windows desktop game launcher / installer.

Built with PyQt6 + requests. Two-stage install:
  1) download + extract the main game archive into the chosen folder
  2) (optional) download a repair/fix archive and merge it on top, overwriting
     matching files and merging folders recursively — with smart
     wrapper-folder detection so the patch never nests one level too deep.

Archives may be ZIP (handled natively with the zipfile module) or RAR
(extracted via 7-Zip when it is installed). The type is detected from the file
signature, falling back to the URL extension. If a .rar is encountered without
7-Zip available, a friendly error is shown instead of crashing.

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

# Network timeouts (connect, read) in seconds, and download chunk size.
REQUEST_TIMEOUT = (10, 30)
DOWNLOAD_CHUNK = 64 * 1024  # 64 KiB

SPACEWAR_APP_ID = 480
SETTINGS_FILE = "settings.json"


def app_dir() -> str:
    """Directory of the running app (works for source and PyInstaller exe)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# Hide the console window when launching 7-Zip from a windowed app.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SEVENZIP_PATH = None  # cached result of find_7zip()
_SEVENZIP_SEARCHED = False


def find_7zip():
    """Locate a 7-Zip executable for RAR extraction (cached).

    Looks on PATH (7z / 7za) and in the standard Windows install folders.
    Returns the executable path, or None if 7-Zip is not available.
    """
    global _SEVENZIP_PATH, _SEVENZIP_SEARCHED
    if _SEVENZIP_SEARCHED:
        return _SEVENZIP_PATH

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
        self.thumbnail = str(raw.get("thumbnail", "")).strip()
        self.zip_url = str(raw.get("zipUrl", "")).strip()
        self.fix_url = str(raw.get("fixZipUrl", "")).strip()

    @property
    def is_available(self) -> bool:
        return bool(self.zip_url)

    @property
    def has_fix(self) -> bool:
        return bool(self.fix_url)

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

    def __init__(self, folder: str):
        super().__init__()
        self.folder = folder

    @pyqtSlot()
    def run(self):
        success, message = add_defender_exclusion(self.folder)
        self.finished.emit(success, message)


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

    def __init__(self, game: Game, dest_path: str):
        super().__init__()
        self.game = game
        self.dest_path = dest_path
        self._cancel = threading.Event()
        self._temp_files = []
        self._temp_dirs = []
        # Progress tracking
        self._start_time = None  # time.monotonic()
        self._stage_start = None  # per-stage start time
        # Overall progress weights: (DL, EX, FIX_DL, FIX_AP)
        self._has_fix = game.has_fix
        if self._has_fix:
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
        suffix = ".rar" if url.lower().split("?")[0].endswith(".rar") else ".zip"
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
            if not self._extract_archive(main_archive, self.dest_path,
                                         self.game.zip_url, self.extract_progress, stage_idx=1):
                return
            self._remove_file(main_archive)
            if self._cancelled():
                return

            # --- 2) Optional fix / repair patch -----------------------------
            if self.game.has_fix:
                fix_archive = self._new_temp_archive(self.game.fix_url)
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
            self._cleanup_temp()
            elapsed = time.monotonic() - self._start_time
            self.elapsed_text.emit(self._fmt_time(elapsed))
            self.status.emit(f"Install completed successfully in {self._fmt_time(elapsed)}")
            self.overall_progress.emit(100)
            self.success.emit(self.dest_path)

        except Exception as exc:  # last-resort safety net — never crash the app
            traceback.print_exc()
            self._cleanup_temp()
            self.error.emit(f"Unexpected error:\n{exc}")

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
        progress_signal.emit(0)
        self._stage_progress[stage_idx] = 0
        self._update_overall_progress()
        self._emit_status(status_text, -1)
        try:
            with requests.get(url, stream=True, timeout=REQUEST_TIMEOUT) as resp:
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
            return True

        except requests.exceptions.MissingSchema:
            self._cleanup_temp()
            self.error.emit("The download URL is invalid.")
            return False
        except requests.exceptions.InvalidURL:
            self._cleanup_temp()
            self.error.emit("The download URL is invalid.")
            return False
        except requests.exceptions.ConnectionError:
            self._cleanup_temp()
            self.error.emit(
                "Network error: could not reach the download server.\n"
                "Check your connection and try again."
            )
            return False
        except requests.exceptions.Timeout:
            self._cleanup_temp()
            self.error.emit("The download timed out. Please try again.")
            return False
        except requests.exceptions.HTTPError as exc:
            self._cleanup_temp()
            self.error.emit(f"Download failed (server returned an error):\n{exc}")
            return False
        except requests.exceptions.RequestException as exc:
            self._cleanup_temp()
            self.error.emit(f"Download failed:\n{exc}")
            return False
        except OSError as exc:
            self._cleanup_temp()
            self.error.emit(f"Could not write the downloaded file:\n{exc}")
            return False

    # -- extraction (ZIP via zipfile, RAR via 7-Zip) ------------------------
    @staticmethod
    def _archive_kind(path, url):
        """Return 'zip', 'rar', or None — signature first, extension fallback."""
        try:
            with open(path, "rb") as fh:
                sig = fh.read(8)
        except OSError:
            sig = b""
        if sig.startswith(b"PK"):            # PK\x03\x04 / PK\x05\x06 / PK\x07\x08
            return "zip"
        if sig.startswith(b"Rar!"):          # RAR4 and RAR5
            return "rar"
        low = url.lower().split("?")[0]
        if low.endswith(".zip"):
            return "zip"
        if low.endswith(".rar"):
            return "rar"
        return None

    def _extract_archive(self, archive_path, dest_dir, url, progress_signal, stage_idx=1) -> bool:
        """Dispatch extraction by archive type. Caller emits the status text."""
        progress_signal.emit(0)
        self._stage_progress[stage_idx] = 0
        self._update_overall_progress()
        kind = self._archive_kind(archive_path, url)
        if kind == "zip":
            return self._extract_zip(archive_path, dest_dir, progress_signal, stage_idx=stage_idx)
        if kind == "rar":
            return self._extract_rar(archive_path, dest_dir, progress_signal, stage_idx=stage_idx)
        self._cleanup_temp()
        self.error.emit(
            "Unsupported archive format.\n"
            "Only .zip and .rar downloads are supported."
        )
        return False

    def _extract_zip(self, zip_path, dest_dir, progress_signal, stage_idx=1) -> bool:
        try:
            with zipfile.ZipFile(zip_path) as zf:
                members = zf.infolist()
                count = len(members)
                if count == 0:
                    self._cleanup_temp()
                    self.error.emit("The downloaded archive is empty.")
                    return False

                for index, member in enumerate(members, start=1):
                    if self._cancel.is_set():
                        self._abort()
                        return False
                    zf.extract(member, dest_dir)
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
                "This download is a .rar archive, which requires 7-Zip to "
                "extract — but 7-Zip was not found on this PC.\n\n"
                "Install 7-Zip from https://www.7-zip.org and try again "
                "(ZIP downloads work without it)."
            )
            return False

        try:
            os.makedirs(dest_dir, exist_ok=True)
            # x = extract with paths, -o = output dir, -y = assume yes,
            # -bsp1 = progress to stdout, -bb1 = log names so output flows.
            proc = subprocess.Popen(
                [seven, "x", rar_path, f"-o{dest_dir}", "-y", "-bsp1", "-bb1"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=_NO_WINDOW,
            )
        except (OSError, ValueError) as exc:
            self._cleanup_temp()
            self.error.emit(f"Could not start 7-Zip for RAR extraction:\n{exc}")
            return False

        # Drain 7-Zip's output on a dedicated thread so the pipe can never fill
        # and deadlock; it parses the latest "NN%" progress token it sees.
        latest = {"pct": None}

        def _drain():
            buf = b""
            try:
                while True:
                    chunk = proc.stdout.read(256)
                    if not chunk:
                        break
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
            self._cleanup_temp()
            self.error.emit(
                "RAR extraction failed — the archive may be corrupt, or 7-Zip "
                "could not read it."
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
        """Smart wrapper-folder detection.

        If the staging dir holds exactly one top-level folder F:
          * If F already exists in the installed game -> F is a real target
            folder (e.g. '<Game>_Data'); merge `staging` as-is so dest/F/...
            lines up.  Do NOT step inside.
          * Otherwise F is just a wrapper (e.g. 'GameName-Fix'); step inside
            it so its contents land directly in the game folder.
        Anything else -> merge `staging` directly.
        """
        entries = os.listdir(staging)
        if len(entries) == 1:
            only = os.path.join(staging, entries[0])
            if os.path.isdir(only):
                if os.path.exists(os.path.join(dest_dir, entries[0])):
                    return staging          # real target folder -> keep it
                return only                 # wrapper -> step inside
        return staging

    def _merge_tree(self, source_root, dest_dir) -> bool:
        """Recursively merge source_root into dest_dir: overwrite matching
        files, merge folders, copy new content, never delete existing files.
        Reports per-file progress via fix_apply_progress."""
        file_list = []
        for root, _dirs, files in os.walk(source_root):
            for name in files:
                file_list.append(os.path.join(root, name))

        total = len(file_list)
        if total == 0:
            self.fix_apply_progress.emit(100)
            self._stage_progress[3] = 100
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
            self.fix_apply_progress.emit(pct)
            self._stage_progress[3] = pct
            if index % max(1, total // 20) == 0:  # Update 20 times
                self._emit_status("Applying fix", pct, f"{index} / {total} files")
                self._update_overall_progress()

        self.fix_apply_progress.emit(100)
        self._stage_progress[3] = 100
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

        if game.is_available:
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
    def __init__(self, game: Game, parent=None):
        super().__init__(parent)
        self.game = game
        self.thread = None
        self.worker = None
        self._installing = False
        self.defender_thread = None
        self.defender_worker = None
        self._installed_path = None

        self.setWindowTitle(f"Install — {game.name}")
        self.setMinimumWidth(540)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(13)

        title = QLabel(game.name)
        title.setObjectName("DialogTitle")
        root.addWidget(title)

        desc = QLabel(game.description)
        desc.setObjectName("CardDesc")
        desc.setWordWrap(True)
        root.addWidget(desc)

        meta_bits = []
        if game.version:
            meta_bits.append(f"Version {game.version}")
        if game.size:
            meta_bits.append(f"Size {game.size}")
        if game.has_fix:
            meta_bits.append("Includes repair patch")
        if meta_bits:
            meta = QLabel("   •   ".join(meta_bits))
            meta.setObjectName("CardMeta")
            root.addWidget(meta)

        # Destination row.
        root.addWidget(self._label("Install location", "SectionLabel"))
        dest_row = QHBoxLayout()
        self.dest_edit = QLineEdit()
        self.dest_edit.setPlaceholderText("Choose a folder to install into…")
        self.dest_edit.setText(os.path.join(os.path.expanduser("~"), "Games"))
        dest_row.addWidget(self.dest_edit)
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.clicked.connect(self._browse)
        dest_row.addWidget(self.browse_btn)
        root.addLayout(dest_row)

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
        self.defender_check.setChecked(False)
        root.addWidget(self.defender_check)
        self.defender_warn = QLabel(
            "⚠  This reduces antivirus scanning for this folder."
        )
        self.defender_warn.setObjectName("DefenderWarning")
        root.addWidget(self.defender_warn)

        # Status + progress bars.
        self.status_label = QLabel("Ready.")
        self.status_label.setObjectName("StatusLabel")
        root.addWidget(self.status_label)

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
        root.addLayout(metrics_row)

        root.addWidget(self._label("Overall progress", "SmallLabel"))
        self.overall_bar = QProgressBar()
        self.overall_bar.setRange(0, 100)
        self.overall_bar.setValue(0)
        root.addWidget(self.overall_bar)

        self.dl_bar = self._add_bar(root, "Download")
        self.ex_bar = self._add_bar(root, "Extraction")
        # Fix bars only shown when this game ships a patch.
        self.fix_dl_label, self.fix_dl_bar = self._add_bar(root, "Fix download", ret_label=True)
        self.fix_ap_label, self.fix_ap_bar = self._add_bar(root, "Applying fix", ret_label=True)
        if not game.has_fix:
            for w in (self.fix_dl_label, self.fix_dl_bar,
                      self.fix_ap_label, self.fix_ap_bar):
                w.setVisible(False)

        # Buttons.
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._on_cancel_clicked)
        btn_row.addWidget(self.cancel_btn)
        self.install_btn = QPushButton("Execute / Install")
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

    def _target_path(self) -> str:
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
        base = self.dest_edit.text().strip()
        if not base:
            QMessageBox.warning(self, "No destination",
                                "Please choose a folder to install the game into.")
            return

        target = self._target_path()
        self._installing = True
        self.install_btn.setEnabled(False)
        self.browse_btn.setEnabled(False)
        self.dest_edit.setEnabled(False)
        self.defender_check.setEnabled(False)
        self.cancel_btn.setText("Cancel")
        for bar in (self.dl_bar, self.ex_bar, self.fix_dl_bar, self.fix_ap_bar):
            bar.setRange(0, 100)
            bar.setValue(0)

        self.thread = QThread()
        self.worker = InstallWorker(self.game, target)
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
        self._installed_path = path

        if self.defender_check.isChecked():
            self.status_label.setText("Adding Defender exclusion…")
            self.cancel_btn.setEnabled(False)
            self._start_defender(path)
        else:
            self._finish(path, None)

    # -- Defender exclusion (post-install) ------------------------------------
    def _start_defender(self, path: str):
        self.defender_thread = QThread()
        self.defender_worker = DefenderWorker(path)
        self.defender_worker.moveToThread(self.defender_thread)
        self.defender_thread.started.connect(self.defender_worker.run)
        self.defender_worker.finished.connect(self._on_defender_done)
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
    def _on_defender_done(self, success, message):
        self._teardown_defender_thread()
        self._finish(self._installed_path, (success, message))

    def _finish(self, path: str, defender_result):
        """Show the final completion dialog and close the install dialog."""
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
        self.stack.addWidget(self.loading_page)
        self.stack.addWidget(self.error_page)
        self.stack.addWidget(self.content_page)

        self.load_manifest()

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

    def _populate_cards(self, games):
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        if not games:
            placeholder = QLabel("No games are available right now.")
            placeholder.setObjectName("SubInfo")
            self.cards_layout.addWidget(placeholder)
            return

        for game in games:
            self.cards_layout.addWidget(GameCard(game, self._open_install))

    def _open_install(self, game: Game):
        InstallDialog(game, self).exec()


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

#BadgeAvailable { color: #2dd4bf; font-size: 12px; font-weight: 700; }
#BadgeSoon {
    color: #fbbf24; font-size: 12px; font-weight: 700;
    background-color: #3a2f12; border-radius: 6px; padding: 3px 8px;
}

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
"""


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(CONFIG["window_title"])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)

    # Let in-flight thumbnail fetches finish before the interpreter tears down,
    # so Qt-pool threads are never running Python during finalization.
    app.aboutToQuit.connect(lambda: QThreadPool.globalInstance().waitForDone(3000))

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
