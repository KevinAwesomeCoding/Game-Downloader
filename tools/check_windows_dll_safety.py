"""Smoke-test packaged app startup beside a controlled winmm.dll collision."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
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


def check(executable: Path, fixture: Path) -> None:
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
        if not shell_execute(ctypes.byref(info)):
            raise ctypes.WinError()
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
            if wait_result != 0:
                kernel.TerminateProcess(info.hProcess, 1)
                kernel.WaitForSingleObject(info.hProcess, 10_000)
                raise RuntimeError(f"Packaged startup timed out: {executable}")
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


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("This check requires Windows.")
    fixture_path = Path(sys.argv[1]).resolve()
    for executable_path in sys.argv[2:]:
        check(Path(executable_path).resolve(), fixture_path)
