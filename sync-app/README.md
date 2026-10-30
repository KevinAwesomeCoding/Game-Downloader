# Game Downloader Sync

A small Windows/macOS companion app for moving committed Game-Downloader changes between computers using an already configured Git remote. The current setup uses the Mac-hosted `mac-lan` remote; it transfers over the local network and does not contact GitHub.

## What it does

- Shows working-tree files and lets you select exactly which files to commit.
- Previews a selected change before committing it.
- Fetches the chosen remote and reports outgoing/incoming commits.
- Pushes local commits, or pulls incoming commits with fast-forward only.
- Stops on divergent branches, staged changes, and dirty working trees that would make a pull unsafe.
- Does not silently stage unselected files, stash, reset, rebase, merge conflicts, or force-push.

This is a manual sync button, not instant background file synchronization. Both computers need network access to the configured remote, Git installed, and the remote authentication already working. Uncommitted changes stay on their current computer unless selected and committed.

## Run from source

Install Python 3.10+ and Git, then run:

```sh
python sync_app.py
```

The first run lets you select the Game-Downloader project folder. Pick the `mac-lan` remote to use the existing Mac-hosted LAN remote. The app remembers the project, remote, and branch in the current user profile.

## Build on macOS

From this folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip pyinstaller
pyinstaller --noconfirm --clean --windowed --name "Game Downloader Sync" --osx-bundle-identifier com.kevinawesomecoding.GameDownloaderSync sync_app.py
```

The app is created at `dist/Game Downloader Sync.app`.

## Build on Windows

Run `build_windows.ps1` in PowerShell from this folder. It creates `dist\Game Downloader Sync.exe` using PyInstaller. Build separately on Windows; the macOS build cannot create a Windows executable.
