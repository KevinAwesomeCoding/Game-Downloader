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
        if self.inspector:
            report = self.inspector()
        else:
            resource_scripts = Path(__file__).resolve().parent.parent
            source_scripts = Path(__file__).resolve().parents[2] / "spikes/highball"
            sys.path.insert(0, str(resource_scripts if (resource_scripts / "readiness.py").exists() else source_scripts))
            from readiness import check
            report = check(Path("/Applications/Highball.app"), self.helper, self.manifest)
        runtime = report.get("readiness") or {}
        self.home = runtime.get("home")
        selected = next((b for b in runtime.get("bottles", []) if b["name"] == bottle), None)
        if not selected or not selected.get("ready") or not self.home or runtime.get("warning"):
            raise ValueError("Highball/bottle is not ready. Open Highball, finish setup, and refresh Settings.")
        return selected, runtime

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
        if recipe:
            if (set(recipe["environment"]) - {"WINEDLLOVERRIDES"} or
                    recipe["renderer"] not in {"wined3d", "dxmt", "d3dmetal", "dxvk"} or
                    not all(isinstance(arg, str) for arg in recipe["arguments"])):
                raise ValueError("Unreviewed recipe setting; no pin was changed.")
            if receipt["mainHashes"] != recipe["mainHashes"] or receipt["repairHashes"] != recipe["repairHashes"]:
                raise ValueError("Files differ from the tested recipe; select an executable manually instead.")
            if selected["engine"] != recipe["engine"]:
                raise ValueError("This saved recipe was tested with a different Highball engine.")
            if not set(recipe["requiredRecipes"]).issubset(selected["recipes"].split(",")):
                raise ValueError("This recipe requires Steam and vcrun2022 setup in Highball first.")
            engine = next(e for e in runtime["engines"] if e["id"] == selected["engine"])
            if not engine["renderers"].get(recipe["renderer"], {}).get("available"):
                raise ValueError("Recipe renderer is unavailable or needs its license accepted in Highball.")
            executable = recipe["executable"]
        if not executable:
            raise ValueError("No verified recipe is available. Choose the actual game .exe; default Highball settings will be used.")
        target = (root / safe_path(executable)).resolve(strict=True)
        if not target.is_relative_to(root) or not target.is_file() or target.suffix.lower() != ".exe":
            raise ValueError("Choose an .exe inside this game's installed folder.")
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
                                   "settingsMode": "recorded" if recipe else "default"}
        if recipe:
            self.command(["pin", "set", bottle, name, "renderer", recipe["renderer"]])
            self.command(["pin", "set", bottle, name, "args", "--", *recipe["arguments"]])
            for key, value in recipe["environment"].items():
                if key != "WINEDLLOVERRIDES":
                    raise ValueError("Unreviewed recipe environment field.")
                self.command(["pin", "set", bottle, name, "env", f"{key}={value}"])
        saved = parse_pins(self.command(["pin", "list", bottle])).get(name.casefold())
        if not saved or not same_external_path(saved["path"], target):
            raise ValueError("Highball did not confirm the saved executable path. Retry registration.")
        if recipe:
            expected = [f"renderer={recipe['renderer']}", "args=" + " ".join(recipe["arguments"]),
                        "env=" + ";".join(f"{key}={value}" for key, value in sorted(recipe["environment"].items()))]
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
        announce("Sending launch request through Highball. Check the game's window for startup.")
        return self.dispatch(["run", bottle, pin])

    def steam_install(self, bottle, uri, announce=lambda message: None):
        if not re.fullmatch(r"steam://install/\d+", uri):
            raise ValueError("Only Steam installation URIs are accepted.")
        self.ready(bottle)
        pins = parse_pins(self.command(["pin", "list", bottle]))
        if "steam" not in pins or not pins["steam"]["path"].replace("\\", "/").lower().endswith("/steam.exe"):
            raise ValueError("Install Windows Steam and sign in inside the selected Highball bottle first.")
        announce("Handing installation to Windows Steam in Highball. Steam handles the download; sign in there if asked.")
        return self.dispatch(["run", bottle, pins["steam"]["name"], "--", uri])
