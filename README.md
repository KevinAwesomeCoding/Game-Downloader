# Game Installer

A lightweight Windows desktop game launcher / installer built with **Python +
PyQt6**. It shows a grid of games, lets you pick an install location, then
downloads the game zip and extracts it into the folder you chose — with live
download and extraction progress, full cancellation support, and a responsive
UI at all times.

![Native desktop app — not a website.]

---

## Features

- Dark, modern launcher-style UI (scrollable card grid).
- Loads games from a local `games.json`, or a remote manifest URL with
  automatic fallback to the local file.
- Per-game install dialog with **native Windows folder picker**.
- Optional install into a per-game subfolder (e.g. `C:\Games\Gang Beasts`).
- **Comprehensive progress reporting:**
  - Overall progress bar for the entire install (weighted by stage).
  - Per-stage progress bars (download, extraction, fix download, fix apply).
  - Download speed (MB/s) and ETA when content-length is available.
  - Elapsed time and file counts during extraction.
  - Graceful degradation when progress info is unavailable.
- Optional repair/fix ZIP or RAR that merges into the game after install.
- ZIP support native; RAR support via 7-Zip when installed.
- Cancel a running download or extraction safely at any time.
- Friendly loading / error states, a **Retry** button, **Refresh**, and a
  "Last updated" timestamp.
- All long-running work runs on background threads — the window never freezes.

---

## 1. Install dependencies

You need **Python 3.9+** installed and on your `PATH`.

```powershell
# From the project folder
python -m pip install -r requirements.txt
```

`requirements.txt` contains:

```
PyQt6
requests
```

(Using a virtual environment is recommended but optional.)

```powershell
python -m venv venv
venv\Scripts\activate
python -m pip install -r requirements.txt
```

---

## 2. Run the app

```powershell
python main.py
```

The window opens as a native Windows application (not a browser). Click a game
card to open its install dialog, choose a destination with **Browse…**, then
click **Execute / Install**.

---

## 3. Edit `games.json`

`games.json` is a JSON array. Each entry describes one game:

```json
[
  {
    "id": "gang-beasts",
    "name": "Gang Beasts",
    "description": "A silly multiplayer party game with wobbly characters.",
    "version": "1.0",
    "size": "850 MB",
    "thumbnail": "https://example.com/header.jpg",
    "zipUrl": "https://example.com/gang-beasts.zip"
  }
]
```

| Field        | Meaning                                                                 |
|--------------|-------------------------------------------------------------------------|
| `id`         | Unique slug for the game.                                                |
| `name`       | Display name. Also used as the install subfolder name.                  |
| `description`| Short description shown on the card.                                     |
| `version`    | Version string (shown on the card).                                     |
| `size`       | Human-readable size. Leave `""` to show "Size unknown".                 |
| `thumbnail`  | Image URL for the card. Leave `""` for a placeholder.                   |
| `zipUrl`     | Direct URL to the game archive (`.zip` or `.rar`). **Leave `""` to mark "Coming Soon".** |
| `fixZipUrl`  | *(optional)* URL to a second "repair/fix patch" archive (`.zip` or `.rar`). Leave `""` to skip. |

A game with an empty `zipUrl` shows a **Coming Soon** badge and its install
button is disabled.

### Archive formats: ZIP and RAR

Both the main game archive (`zipUrl`) and the fix archive (`fixZipUrl`) may be
either a **ZIP** or a **RAR** file:

- **ZIP** is handled natively with Python's built-in `zipfile` module — nothing
  extra to install.
- **RAR** is extracted using **7-Zip**. Install it from
  [7-zip.org](https://www.7-zip.org); the app finds `7z.exe` on your `PATH` or
  in the standard `C:\Program Files\7-Zip\` location automatically.
- The archive type is detected from the file **signature** (`PK…` = ZIP,
  `Rar!…` = RAR), falling back to the URL **extension** — so links with query
  strings like `patch.rar?token=abc` still work.
- If a `.rar` download is encountered and 7-Zip is **not** installed, the app
  shows a clear, friendly message explaining that RAR extraction requires 7-Zip
  (it does **not** crash). ZIP downloads keep working without 7-Zip.

> The field names remain `zipUrl` / `fixZipUrl` for backward compatibility, but
> each may point at a `.rar` as well as a `.zip`.

### Optional fix / repair patch (`fixZipUrl`)

If a game entry includes a non-empty `fixZipUrl`, the installer applies it as a
second step **after** the main game is installed. The full install order is:

1. Download the main game zip.
2. Extract the main game zip into the destination folder.
3. Download the fix zip.
4. Apply the fix: its contents are **merged** into the game folder, overwriting
   any matching files.

The merge follows safe rules:

- **Matching files** are overwritten.
- **Matching folders** are merged into recursively (existing folders like
  `data/` gain the patched files instead of being replaced wholesale).
- Files/folders that exist **only in the fix** are copied in.
- **Unrelated existing game files are never deleted.**

#### Smart wrapper-folder detection

Fix archives are often zipped one of two ways. The installer inspects the
extracted fix and picks the right behaviour automatically when there is exactly
one top-level folder `F`:

- **Wrapper folder** — if `F` does *not* already exist in the installed game
  (e.g. `GangBeasts-Fix/...`), it's treated as a throwaway wrapper. The
  installer **steps inside** `F` before merging, so you never get a stray
  `…\Gang Beasts\GangBeasts-Fix\` directory.
- **Real target folder** — if `F` *does* already exist in the installed game
  (e.g. the patch ships `Gamble with Your Friends_Data/Plugins/...` and the game
  already has `Gamble with Your Friends_Data/`), `F` is kept and merged into the
  matching installed folder — so the patch lands at the correct depth instead of
  being nested one level too deep.

If there are multiple top-level entries, the fix contents are merged in as-is.

#### Progress & status

The installer provides **comprehensive progress reporting**:

**Overall progress:** A single bar shows the overall completion of the entire
install, weighted by stage. The weights are:
- **Without a fix:** Download 50%, Extraction 50%
- **With a fix:** Download 35%, Extraction 25%, Fix Download 20%, Fix Apply 20%

**Per-stage progress:** Individual progress bars show:
- **Download** — percentage + downloaded bytes / total bytes + current speed (MB/s) + ETA
- **Extraction** — percentage + files extracted / total files
- **Fix Download** — percentage + speed + ETA (only shown if `fixZipUrl` is present)
- **Fix Apply** — percentage + files merged / total files (only shown if `fixZipUrl` is present)

**Status text examples:**
- `Downloading Gang Beasts 37%  412 MB / 1.2 GB at 8.4 MB/s, 1m 34s remaining`
- `Extracting 82%  421 / 1080 files`
- `Install completed successfully in 2m 15s`

**Elapsed time** is shown throughout the install, so you can see how long the
process is taking.

**Cancellation:** You can cancel during any phase. Temporary files are cleaned
up, and the app stays responsive throughout.

### Using a remote manifest

Open `main.py` and edit the `CONFIG` block near the top:

```python
CONFIG = {
    "manifest_url": "https://example.com/games.json",
    "window_title": "Game Installer",
    "default_install_subfolder": True
}
```

- If `manifest_url` is **blank**, games load from the local `games.json`.
- If `manifest_url` is **set**, the app fetches it first and falls back to the
  local `games.json` if the fetch fails.
- `default_install_subfolder`: when `True`, the game is installed into a
  subfolder named after the game inside the folder you pick
  (e.g. picking `C:\Games` installs to `C:\Games\Gang Beasts`). When `False`,
  files extract directly into the folder you pick.

---

## 4. Package to a standalone `.exe` with PyInstaller

Install PyInstaller and build a single-file, windowed executable:

```powershell
python -m pip install pyinstaller
pyinstaller --onefile --windowed --name GameInstaller main.py
```

The executable is created in the `dist\` folder as `dist\GameInstaller.exe`.

> **Note:** `games.json` is read from the same folder as the running app/exe.
> Place your `games.json` next to `GameInstaller.exe` (or configure a
> `manifest_url`) so the packaged app can find its game library.

---

## Troubleshooting

- **"Could not find games.json"** — make sure `games.json` is next to
  `main.py` (or `GameInstaller.exe`), or set a `manifest_url`.
- **Download fails / "not a valid zip archive"** — verify the `zipUrl` points
  directly at a real `.zip` file (not an HTML download page).
- **Permission denied** — choose a destination folder your user can write to
  (e.g. somewhere under your home folder) instead of a protected location.
