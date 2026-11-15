"""Smoke-test packaged app startup beside a controlled winmm.dll collision."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile


class ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", wintypes.LPVOID),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


class StartupTimeout(RuntimeError):
    """The packaged startup check did not finish within its deadline."""


def _check_once(executable: Path, fixture: Path) -> None:
    adjacent_fixture = executable.parent / "winmm.dll"
    if adjacent_fixture.exists():
        raise RuntimeError(f"Refusing to overwrite existing DLL: {adjacent_fixture}")

    marker = Path(tempfile.gettempdir()) / f"gameinstaller-winmm-fixture-{os.getpid()}.marker"
    marker.unlink(missing_ok=True)
    adjacent_fixture.write_bytes(fixture.read_bytes())
    os.environ["GAMEINSTALLER_DLL_FIXTURE_MARKER"] = str(marker)
    try:
        shell_execute = ctypes.windll.shell32.ShellExecuteExW
        shell_execute.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
        shell_execute.restype = wintypes.BOOL
        info = ShellExecuteInfo()
        info.cbSize = ctypes.sizeof(info)
        info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
        # "open" honors the executable's asInvoker/requireAdministrator
        # manifest, while allowing local smoke builds without an elevation prompt.
        info.lpVerb = "open"
        info.lpFile = str(executable)
        info.lpParameters = "--check-download-browser"
        info.lpDirectory = str(executable.parent)
        info.nShow = 0
        # The packaged application requests elevation for normal interactive
        # use. CI only needs its early browser-check path; run that path as the
        # current user so ShellExecuteEx does not wait on an unattended UAC UI.
        previous_compat_layer = os.environ.get("__COMPAT_LAYER")
        os.environ["__COMPAT_LAYER"] = "RunAsInvoker"
        try:
            if not shell_execute(ctypes.byref(info)):
                raise ctypes.WinError()
        finally:
            if previous_compat_layer is None:
                os.environ.pop("__COMPAT_LAYER", None)
            else:
                os.environ["__COMPAT_LAYER"] = previous_compat_layer
        try:
            kernel = ctypes.windll.kernel32
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel.TerminateProcess.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            wait_result = kernel.WaitForSingleObject(info.hProcess, 180_000)
            if wait_result == 0xFFFFFFFF:  # WAIT_FAILED
                raise ctypes.WinError()
            if wait_result != 0:
                # The helper launches a driver and browser; terminating only
                # the parent leaves children behind on an unattended runner.
                kernel.GetProcessId.argtypes = [wintypes.HANDLE]
                kernel.GetProcessId.restype = wintypes.DWORD
                pid = kernel.GetProcessId(info.hProcess)
                if pid:
                    try:
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       timeout=10, check=False)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                kernel.TerminateProcess(info.hProcess, 1)
                kernel.WaitForSingleObject(info.hProcess, 10_000)
                raise StartupTimeout(f"Packaged startup timed out: {executable}")
            exit_code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
                raise ctypes.WinError()
        finally:
            kernel.CloseHandle(info.hProcess)
        if exit_code.value != 0:
            raise RuntimeError(f"Packaged startup failed ({exit_code.value}): {executable}")
        if marker.exists():
            raise RuntimeError(f"Adjacent winmm.dll was loaded by the app: {executable}")
    finally:
        adjacent_fixture.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)


def check(executable: Path, fixture: Path) -> None:
    try:
        _check_once(executable, fixture)
    except StartupTimeout:
        print(f"Packaged startup timed out; retrying once: {executable}", flush=True)
        _check_once(executable, fixture)


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("This check requires Windows.")
    # Keep the short R: alias from the Windows CI build instead of expanding
    # it to the runner's long checkout path; Playwright launches Chromium from
    # a deeply nested directory beneath the packaged app.
    fixture_path = Path(sys.argv[1]).absolute()
    for executable_path in sys.argv[2:]:
        check(Path(executable_path).absolute(), fixture_path)
