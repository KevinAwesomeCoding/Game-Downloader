"""Pin Windows' own winmm before Qt loads its statically imported DLL."""

import ctypes
import os
import sys


if os.name == "nt":
    system_winmm = os.path.join(os.environ["SystemRoot"], "System32", "winmm.dll")
    # Qt6Core.dll imports WINMM.dll by name. Keep the absolute-path load alive
    # so the loader reuses this trusted module instead of resolving a winmm.dll
    # beside the executable (for example, an unrelated game's proxy DLL).
    sys._gameinstaller_system_winmm = ctypes.WinDLL(system_winmm)
