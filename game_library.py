"""Platform-neutral helpers for recognizing games in a library folder."""

import os
import re


_NON_GAME_EXE_RE = re.compile(
    r"unins|uninstall|setup|install|vc_?redist|vcredist|dxsetup|dxwebsetup|"
    r"directx|dotnet|ndp\d|netfx|redist|prereq|oalinst|openal|"
    r"crashhandler|crashreport|crashpad|unitycrashhandler|"
    r"unrealcefsubprocess|cefsubprocess|epicwebhelper|eossdk|"
    r"activation|touchup|notification|helper|python|node",
    re.IGNORECASE,
)


def _normalize_title(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def find_main_executable(root, game_name=""):
    """Return the likeliest game executable, or None if there are no .exes."""
    candidates = []
    for dirpath, _dirs, files in os.walk(root):
        for filename in files:
            if not filename.lower().endswith(".exe"):
                continue
            full = os.path.join(dirpath, filename)
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            rel = os.path.relpath(dirpath, root)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            candidates.append((full, filename, size, depth))
    if not candidates:
        return None

    game_key = _normalize_title(game_name)

    def score(entry):
        _full, filename, size, depth = entry
        stem = _normalize_title(os.path.splitext(filename)[0])
        points = 0.0
        if _NON_GAME_EXE_RE.search(filename):
            points -= 1_000_000
        if game_key and stem and (game_key in stem or stem in game_key):
            points += 1_000_000
        points -= depth * 1000
        points += min(size, 4_000_000_000) / 1_000_000
        return points

    return max(candidates, key=score)[0]


def scan_installed_games(games, games_folder, chosen_exes=None):
    """Report each regular catalog game as found or missing in the folder.

    Only non-empty immediate subfolders matching a catalog title count as
    installed. Hidden folders, symlinks, and repair-only catalog entries are
    ignored, matching the macOS Games Owned scan behavior.
    """
    folder_by_name = {}
    if games_folder and os.path.isdir(games_folder):
        try:
            with os.scandir(games_folder) as entries:
                for entry in entries:
                    try:
                        if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                            continue
                        with os.scandir(entry.path) as contents:
                            if not next(contents, None):
                                continue
                    except OSError:
                        continue
                    folder_by_name.setdefault(entry.name.casefold(), entry.path)
        except OSError:
            pass

    if not isinstance(chosen_exes, dict):
        chosen_exes = {}
    results = []
    for game in games:
        if game.is_patch:
            continue
        folder = folder_by_name.get(game.safe_folder_name.casefold())
        exe = None
        if folder:
            exe = chosen_exes.get(game.id)
            try:
                if exe and (
                    os.path.commonpath([os.path.abspath(folder), os.path.abspath(exe)])
                    != os.path.abspath(folder)
                    or not os.path.isfile(exe)
                    or not exe.lower().endswith(".exe")
                ):
                    exe = None
            except (OSError, ValueError):
                exe = None
            if not exe:
                exe = find_main_executable(folder, game.name)
        results.append({"game": game, "folder": folder, "exe": exe, "root": games_folder})
    return results
