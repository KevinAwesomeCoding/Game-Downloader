#!/usr/bin/env python3
"""Build the native browser helper and matched headless browser in build/."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import venv


def main():
    root = Path(__file__).resolve().parents[1]
    environment = root / "build" / "host-helper-env"
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(environment)
    subprocess.run([str(python), "-m", "pip", "install", "-r",
                    str(root / "tools/requirements-gofile.txt"), "pyinstaller>=6.0,<7"], check=True)
    # Keep Chromium out of PyInstaller's binary rewriting. Its signed native
    # libraries must retain their original loader paths; copy them after freezing.
    browser_cache = root / "build" / "host-browser"
    package = Path(subprocess.check_output([
        str(python), "-c", "import pathlib,playwright;print(pathlib.Path(playwright.__file__).parent / 'driver/package')"
    ], text=True).strip())
    previous = package / ".local-browsers"
    if previous.is_dir():
        shutil.rmtree(previous)  # Generated only inside this isolated build venv.
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(browser_cache)}
    subprocess.run([str(python), "-m", "playwright", "install", "chromium", "--only-shell"], env=env, check=True)
    subprocess.run([
        str(python), "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--name", "HostResolver", "--distpath", str(root / "build/host-resolver"),
        "--workpath", str(root / "build/host-resolver-work"),
        "--specpath", str(root / "build"), "--collect-all", "playwright",
        str(root / "host_resolver.py"),
    ], cwd=root, env=env, check=True)
    destination = root / "build/host-resolver/HostResolver/_internal/playwright/driver/package/.local-browsers"
    shutil.copytree(browser_cache, destination, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".links"))
    executable = root / "build/host-resolver/HostResolver" / ("HostResolver.exe" if sys.platform == "win32" else "HostResolver")
    subprocess.run([str(executable), "--self-check"], check=True,
                   env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})
    print(f"Built helper: {root / 'build/host-resolver/HostResolver'}")


if __name__ == "__main__":
    main()
