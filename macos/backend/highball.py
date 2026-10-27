"""Supported Highball CLI boundary. Tests inject inspection and process adapters."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from archives import safe_path
from universal_setup import native_first_dll_names


_NON_GAME_EXECUTABLES = {
    "unins000", "uninstaller", "uninstall", "setup", "install", "installer",
    "vc_redist.x64", "vc_redist.x86", "dxsetup", "unitycrashhandler64",
    "unitycrashhandler32", "crashreportclient", "easyanticheat_eos_setup",
    "battleye_launcher", "redist", "directxsetup",
}


def _normalized_name(value):
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def detect_game_executable(root, game_name):
    """Choose a high-confidence game executable without applying compatibility fixes.

    Exact title matches win. Otherwise recognize common shipping executable names,
    then use a sole plausible executable only when the folder has no ambiguity.
    """
    root = Path(root).resolve(strict=True)
    title = _normalized_name(game_name)
    candidates = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories
                                if not (Path(current) / name).is_symlink())
        for filename in filenames:
            path = Path(current) / filename
            if path.suffix.casefold() != ".exe" or path.is_symlink() or not path.is_file():
                continue
            stem = path.stem
            normalized = _normalized_name(stem)
            path_parts = {part.casefold() for part in path.relative_to(root).parts[:-1]}
            if (normalized in _NON_GAME_EXECUTABLES or
                    any(token in normalized for token in ("crashhandler", "unins", "redist", "prereqsetup")) or
                    any(part in {"_commonredist", "redist", "redistributables", "support"} for part in path_parts)):
                continue
            score = 0
            if normalized == title:
                score = 100
            elif title and (normalized.startswith(title) or title.startswith(normalized)):
                score = 90
            elif title and title in normalized and any(tag in normalized for tag in ("win64shipping", "shipping", "game")):
                score = 85
            elif title and normalized in title and len(normalized) >= max(5, len(title) // 2):
                score = 80
            if "binaries" in path_parts and ("win64" in path_parts or "win32" in path_parts):
                score += 5
            candidates.append((score, str(path.relative_to(root))))

    if not candidates:
        return None
    ranked = sorted(candidates, key=lambda item: (-item[0], item[1].casefold()))
    if ranked[0][0] >= 80 and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
        return ranked[0][1]
    # A single non-installer executable is a safe default; multiple unrelated
    # executables require the user to choose explicitly.
    if len(ranked) == 1:
        return ranked[0][1]
    return None


def parse_pins(output):
    pins = {}
    for line in output.splitlines():
        if not line.strip() or line.startswith("no pins in "):
            continue
        fields = line.split("\t")
        if len(fields) < 2 or fields[0].casefold() in pins:
            raise ValueError("Unrecognized or duplicate Highball pin listing; no changes made.")
        pins[fields[0].casefold()] = {"name": fields[0], "path": fields[1], "extras": fields[2:]}
    return pins


def same_external_path(stored, actual):
    # Current Highball stores external Unix paths directly; also understand Wine's Z: spelling.
    if stored.startswith("Z:") or stored.startswith("z:"):
        stored = stored[2:].replace("\\", "/")
    return stored.startswith("/") and Path(stored).resolve() == Path(actual).resolve()


class Highball:
    def __init__(self, helper=None, inspector=None, runner=None, gui_running=None, launcher=None, launch_directory=None):
        support = Path.home() / "Library/Application Support/Game-Downloader/HighballHelper/0.10.22"
        self.helper = Path(helper) if helper else support / "highball"
        self.manifest = support / "build-manifest.json"
        self.inspector = inspector
        self.runner = runner or subprocess.run
        self.gui_running = gui_running or self._gui_running
        self.launcher = launcher or subprocess.Popen
        self.launch_directory = Path(launch_directory) if launch_directory else (
            Path.home() / "Library/Application Support/Game-Downloader/MacPreview/LaunchLogs")
        self.home = None

    @staticmethod
    def _gui_running():
        return subprocess.run(["/usr/bin/pgrep", "-x", "Highball"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0

    def ready(self, bottle):
        if not bottle or any(c in bottle for c in "\t\n\r"):
            raise ValueError("Choose a Highball bottle in Settings first.")
        report = self._readiness_report()
        runtime = report.get("readiness") or {}
        self.home = runtime.get("home")
        selected = next((b for b in runtime.get("bottles", []) if b["name"] == bottle), None)
        if not selected or not selected.get("ready") or not self.home or runtime.get("warning"):
            raise ValueError("Highball/bottle is not ready. Open Highball, finish setup, and refresh Settings.")
        return selected, runtime

    def _readiness_report(self):
        if self.inspector:
            return self.inspector()
        resource_scripts = Path(__file__).resolve().parent.parent
        source_scripts = Path(__file__).resolve().parents[2] / "spikes/highball"
        sys.path.insert(0, str(resource_scripts if (resource_scripts / "readiness.py").exists() else source_scripts))
        from readiness import check
        return check(Path("/Applications/Highball.app"), self.helper, self.manifest)

    def apply_prerequisites(self, bottle, recipe_ids, announce=lambda message: None, stop=lambda: False):
        """Apply only the reviewed Highball prerequisite recipes missing from this bottle."""
        supported = {"steam", "vcrun2022"}
        requested = list(dict.fromkeys(recipe_ids))
        if not requested or set(requested) - supported:
            raise ValueError("The requested Highball prerequisite recipe is not in the reviewed allowlist.")
        selected, _runtime = self.ready(bottle)
        applied = set(filter(None, selected.get("recipes", "").split(",")))
        recipe_dir = Path(__file__).resolve().with_name("HighballRecipes")
        for recipe_id in requested:
            if recipe_id in applied:
                continue
            if stop():
                raise InterruptedError("Highball setup stopped before the next prerequisite; completed setup steps remain installed.")
            recipe_path = recipe_dir / f"{recipe_id}.json"
            if not recipe_path.is_file() or recipe_path.is_symlink():
                raise ValueError(f"The bundled Highball {recipe_id} setup recipe is missing or unsafe.")
            if recipe_id == "steam":
                # The upstream Steam recipe suggests DXMT. Keep this bottle's current
                # renderer as an explicit user choice before applying that launcher recipe.
                renderer = selected.get("renderer")
                if renderer not in {"wined3d", "dxmt", "d3dmetal", "dxvk"}:
                    raise ValueError("Highball did not report a supported bottle renderer; no prerequisite was applied.")
                self.command(["bottle", "set", bottle, "renderer", renderer])
            announce(f"Installing {recipe_id} in the {bottle} Highball bottle…")
            self.command(["recipe", "apply", bottle, str(recipe_path)], timeout=1800)
            selected, _runtime = self.ready(bottle)
            applied = set(filter(None, selected.get("recipes", "").split(",")))
            if recipe_id not in applied:
                raise ValueError(f"Highball finished the {recipe_id} command but did not confirm that recipe in the bottle.")
            announce(f"{recipe_id} is ready in the {bottle} bottle.")
        if stop():
            raise InterruptedError("Highball setup stopped after a completed prerequisite; the bottle changes remain available.")
        return requested

    def command(self, arguments, timeout=30):
        if not self.home:
            raise ValueError("Highball readiness must be checked first.")
        with tempfile.TemporaryDirectory(prefix="game-installer-highball-") as cwd:
            result = self.runner([str(self.helper), *arguments], cwd=cwd,
                                 env=dict(os.environ, HIGHBALL_HOME=self.home, LC_ALL="C"),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, errors="replace", timeout=timeout, shell=False)
        if result.returncode:
            raise ValueError(f"Highball exited {result.returncode}: {(result.stderr or result.stdout)[-2000:]}")
        return result.stdout

    def dispatch(self, arguments):
        """Send a launch request without holding the UI until Steam/the game exits."""
        if not self.home:
            raise ValueError("Highball readiness must be checked first.")
        self.launch_directory.mkdir(parents=True, exist_ok=True)
        log_path = self.launch_directory / f"{time.time_ns()}.log"
        with log_path.open("wb") as log:
            process = self.launcher([str(self.helper), *arguments], cwd=str(self.launch_directory),
                                    env=dict(os.environ, HIGHBALL_HOME=self.home, LC_ALL="C"),
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True, shell=False)
            try:
                code = process.wait(timeout=2)
                if code:
                    raise ValueError(f"Highball launch request exited {code}. {log_path.read_text(errors='replace')[-2000:]} Log: {log_path}")
            except subprocess.TimeoutExpired:
                pass
        # Request accepted locally, not proof Wine/the game finished starting.
        # Child owns a file handle, not a pipe to this short-lived worker.
        return {"pid": process.pid, "logPath": str(log_path)}

    def _set_catalog_cover(self, bottle, name, target, thumbnail):
        """Save a catalog thumbnail using Highball 0.10.22's CoverStore layout."""
        if not thumbnail:
            return "unavailable"
        parsed = urlsplit(thumbnail)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("The catalog cover URL must use HTTPS.")
        if Path(bottle).name != bottle or bottle in {".", ".."}:
            raise ValueError("Invalid Highball bottle name for cover lookup.")

        config = Path(self.home) / "bottles" / bottle / "bottle.json"
        state = json.loads(config.read_text())
        pin = next((item for item in state.get("pins", [])
                    if item.get("name", "").casefold() == name.casefold()
                    and same_external_path(item.get("path", ""), target)), None)
        if not pin or not re.fullmatch(r"[0-9A-Fa-f-]{36}", pin.get("id", "")):
            raise ValueError("Highball did not expose the new pin identity needed for its cover.")

        pin_id = f"pin:{bottle}:{pin['id']}"
        filename = pin_id.replace(":", "_").replace("/", "_")
        covers = Path(self.home) / "covers"
        covers.mkdir(parents=True, exist_ok=True)
        # Respect a cover the user chose manually, including one associated with an
        # earlier bottle name. Highball resolves those files by the stable pin UUID.
        uuid = pin["id"]
        if any(path.suffix.lower() in {".png", ".jpg", ".jpeg", ".heic", ".webp"}
               and path.stem.startswith("pin_") and path.stem.endswith("_" + uuid)
               for path in covers.iterdir()):
            return "existing"
        if any((covers / f"{filename}{ext}").is_file()
               for ext in (".png", ".jpg", ".jpeg", ".heic", ".webp")):
            return "existing"

        request = Request(thumbnail, headers={"User-Agent": "Game-Downloader/1.0"})
        with urlopen(request, timeout=30) as response:
            if urlsplit(response.geturl()).scheme != "https":
                raise ValueError("The catalog cover redirected away from HTTPS.")
            data = response.read(20 * 1024 * 1024 + 1)
        if len(data) > 20 * 1024 * 1024:
            raise ValueError("The catalog cover image exceeds 20 MB.")
        jpeg = data.startswith(b"\xff\xd8\xff")
        png = data.startswith(b"\x89PNG\r\n\x1a\n")
        webp = data.startswith(b"RIFF") and data[8:12] == b"WEBP"
        if not (jpeg or png or webp):
            raise ValueError("The catalog cover URL did not return a supported image.")

        extension = ".jpg" if jpeg else ".png" if png else ".webp"
        destination = covers / f"{filename}{extension}"
        temporary = covers / f".{filename}.cover-part"
        try:
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return "set"

    def register(self, receipt, bottle, recipe=None, executable=None):
        selected, runtime = self.ready(bottle)
        if self.gui_running():
            raise ValueError("Close Highball's window/app before adding a program to avoid stale settings writes. Steam may remain running.")
        root = Path(receipt["installPath"]).resolve(strict=True)
        executable_source = "manual" if executable else None
        if recipe:
            environment = recipe.get("environment", {})
            arguments = recipe.get("arguments", [])
            renderer = recipe.get("renderer")
            renderer_fallback = None
            if (not isinstance(environment, dict) or set(environment) - {"WINEDLLOVERRIDES"} or
                    (renderer is not None and renderer not in {"wined3d", "dxmt", "d3dmetal", "dxvk"}) or
                    not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments)):
                raise ValueError("Unreviewed recipe setting; no pin was changed.")
            if receipt["mainHashes"] != recipe["mainHashes"] or receipt["repairHashes"] != recipe["repairHashes"]:
                raise ValueError("Files differ from the tested recipe; select an executable manually instead.")
            if selected["engine"] != recipe["engine"]:
                raise ValueError("This saved recipe was tested with a different Highball engine.")
            if not set(recipe["requiredRecipes"]).issubset(selected["recipes"].split(",")):
                raise ValueError("This recipe requires Steam and vcrun2022 setup in Highball first.")
            engine = next(e for e in runtime["engines"] if e["id"] == selected["engine"])
            if renderer and not engine["renderers"].get(renderer, {}).get("available"):
                bottle_renderer = selected.get("renderer")
                if bottle_renderer and engine["renderers"].get(bottle_renderer, {}).get("available"):
                    renderer_fallback = {"requested": renderer, "used": bottle_renderer}
                    renderer = bottle_renderer
                else:
                    raise ValueError("Recipe renderer is unavailable or needs its license accepted in Highball, and the bottle renderer is unavailable too.")
            executable = recipe["executable"]
            executable_source = "recorded recipe"
        if not executable:
            previous = receipt.get("detectedExecutable")
            if previous:
                try:
                    previous_path = (root / safe_path(previous)).resolve(strict=True)
                    if not previous_path.is_relative_to(root) or not previous_path.is_file() or previous_path.suffix.lower() != ".exe":
                        previous = None
                except (OSError, ValueError):
                    previous = None
            executable = previous or detect_game_executable(root, receipt.get("gameName", ""))
            if executable:
                executable_source = "automatic detection"
                receipt["detectedExecutable"] = executable
            else:
                raise ValueError("Could not identify one likely game .exe. Choose the game executable manually; no unverified compatibility fixes will be applied.")
        target = (root / safe_path(executable)).resolve(strict=True)
        if not target.is_relative_to(root) or not target.is_file() or target.suffix.lower() != ".exe":
            raise ValueError("Choose an .exe inside this game's installed folder.")
        # Apply this narrowly-scoped compatibility setting even for a manually
        # selected executable when no archive recipe exists. Only known Wine
        # builtin collisions that are actually present in this game are included.
        detected_dlls = native_first_dll_names(root)
        environment = dict(environment) if recipe else {}
        current_overrides = environment.get("WINEDLLOVERRIDES", "")
        override_names = set(detected_dlls)
        for entry in current_overrides.split(";"):
            name, separator, _value = entry.partition("=")
            if separator and name.casefold() in detected_dlls:
                override_names.add(name.casefold())
        native_first = ";".join(f"{name}=native,builtin" for name in sorted(override_names))
        if native_first:
            environment["WINEDLLOVERRIDES"] = native_first
        else:
            environment.pop("WINEDLLOVERRIDES", None)
        # New pins use only the game title. Preserve a pin already recorded by an
        # older receipt so retries keep targeting that exact existing entry.
        pins = parse_pins(self.command(["pin", "list", bottle]))
        registration = receipt.get("registration") or {}
        old_pin = registration.get("pin")
        name = old_pin if old_pin and old_pin.casefold() in pins else receipt["gameName"]
        if any(c in name for c in "\t\r\n"):
            raise ValueError("Invalid pin name.")
        existing = pins.get(name.casefold())
        if existing and not same_external_path(existing["path"], target):
            raise ValueError("A Highball pin with this name points elsewhere. Resolve it in Highball; installed files were kept.")
        registration = receipt.get("registration") or {}
        if existing and not (registration.get("bottle") == bottle and registration.get("pin") == name):
            raise ValueError("That pin already exists and is not owned by this install receipt. No settings were overwritten.")
        if existing and recipe is None and registration.get("settingsMode") == "recorded":
            raise ValueError("This pin already has recorded settings. Use its recorded recipe or adjust the pin in Highball; defaults were not silently substituted.")
        if not existing:
            self.command(["pin", "add", bottle, name, str(target)])
        # Record ownership immediately so retry can recover after a failed setting.
        receipt["registration"] = {"bottle": bottle, "pin": name, "executable": executable, "complete": False,
                                   "settingsMode": "recorded" if recipe else ("detected-dlls" if native_first else "default")}
        if executable_source:
            receipt["registration"]["executableSource"] = executable_source
        if recipe and renderer_fallback:
            receipt["registration"]["rendererFallback"] = renderer_fallback
        if native_first:
            receipt["registration"]["nativeFirstDlls"] = [
                entry.split("=", 1)[0]
                for entry in native_first.split(";") if "=" in entry
            ]
        if recipe:
            if renderer:
                self.command(["pin", "set", bottle, name, "renderer", renderer])
            if arguments:
                self.command(["pin", "set", bottle, name, "args", "--", *arguments])
        for key, value in environment.items():
            if key != "WINEDLLOVERRIDES":
                raise ValueError("Unreviewed recipe environment field.")
            self.command(["pin", "set", bottle, name, "env", f"{key}={value}"])
        saved = parse_pins(self.command(["pin", "list", bottle])).get(name.casefold())
        if not saved or not same_external_path(saved["path"], target):
            raise ValueError("Highball did not confirm the saved executable path. Retry registration.")
        expected = []
        if recipe:
            if renderer:
                expected.append(f"renderer={renderer}")
            if arguments:
                expected.append("args=" + " ".join(arguments))
        if environment:
            expected.append("env=" + ";".join(f"{key}={value}" for key, value in sorted(environment.items())))
        if expected:
            if not all(value in saved["extras"] for value in expected):
                raise ValueError("Highball did not confirm every recorded pin setting. Retry registration.")
        try:
            receipt["registration"]["coverStatus"] = self._set_catalog_cover(
                bottle, name, target, receipt.get("thumbnail"))
        except Exception as exc:
            # Image failure must not undo a verified pin or prevent the game launching.
            receipt["registration"]["coverStatus"] = "failed"
            receipt["registration"]["coverMessage"] = str(exc)[:500]
        receipt["registration"]["complete"] = True
        return receipt

    def launch(self, receipt, announce=lambda message: None):
        registration = receipt.get("registration") or {}
        if not registration.get("complete"):
            raise ValueError("Add this installation to Highball successfully before playing.")
        bottle, pin = registration["bottle"], registration["pin"]
        self.ready(bottle)
        stored = parse_pins(self.command(["pin", "list", bottle])).get(pin.casefold())
        target = Path(receipt["installPath"]) / safe_path(registration["executable"])
        if not stored or not target.is_file() or not same_external_path(stored["path"], target):
            raise ValueError("The registered executable is missing or its Highball pin changed.")
        setup = receipt.get("universalSetup") or {}
        if (setup.get("settings") or {}).get("requiresSteamFirst"):
            if not registration.get("steamReadyForNextLaunch"):
                pins = parse_pins(self.command(["pin", "list", bottle]))
                steam = pins.get("steam")
                if not steam or not steam["path"].replace("\\", "/").lower().endswith("/steam.exe"):
                    raise ValueError("Windows Steam is not pinned in this bottle. Open Highball and install/sign in to Steam first.")
                announce("Opening Windows Steam. Sign in there, then click Play in the installer again.")
                result = self.dispatch(["run", bottle, steam["name"]])
                registration["steamReadyForNextLaunch"] = True
                receipt["registration"] = registration
                return {**result, "steamStarted": True}
            # A second Play click is the user's confirmation that Steam is ready.
            registration["steamReadyForNextLaunch"] = False
            receipt["registration"] = registration
        announce("Sending launch request through Highball. Check the game's window for startup.")
        return self.dispatch(["run", bottle, pin])

    def steam_install(self, bottle, uri, announce=lambda message: None):
        launch_client = uri == "steam://open/main"
        if not launch_client and not re.fullmatch(r"steam://install/\d+", uri):
            raise ValueError("Only Steam installation URIs are accepted.")
        self.ready(bottle)
        pins = parse_pins(self.command(["pin", "list", bottle]))
        if "steam" not in pins or not pins["steam"]["path"].replace("\\", "/").lower().endswith("/steam.exe"):
            raise ValueError("Install Windows Steam and sign in inside the selected Highball bottle first.")
        if launch_client:
            announce("Opening Windows Steam in Highball.")
            return {**self.dispatch(["run", bottle, pins["steam"]["name"]]), "steamOpened": True}
        announce("Handing installation to Windows Steam in Highball. Steam handles the download; sign in there if asked.")
        return self.dispatch(["run", bottle, pins["steam"]["name"], "--", uri])
