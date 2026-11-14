"""Browser-assisted sources; the installer itself needs only the standard library."""
from pathlib import Path
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit


class ResolutionStopped(Exception):
    pass


def provider_for(url):
    host = (urlsplit(url).hostname or "").lower()
    if host in {"gofile.io", "www.gofile.io"} or host.endswith(".gofile.io"):
        return "gofile"
    if host == "filekeeper.net" or host.endswith(".filekeeper.net"):
        return "filekeeper"
    if host == "filesyard.com" or host.endswith(".filesyard.com"):
        return "filesyard"
    if any(host == d or host.endswith('.' + d) for d in ('bzzhr.to', 'bzzhr.co', 'buzzheavier.com')):
        return "bzzhr"
    if any(host == d or host.endswith('.' + d) for d in ('fileditchfiles.st', 'fileditchfiles.me', 'itsfileditchfiles.st')):
        return "fileditch"
    return None


def download_host_allowed(provider, url):
    host = (urlsplit(url).hostname or "").lower()
    domains = {"gofile": ("gofile.io",), "filekeeper": ("filekeeper.net", "fetchio.cc"),
               "filesyard": ("filesyard.com",),
               "bzzhr": ("bzzhr.to", "bzzhr.co", "buzzheavier.com"),
               "fileditch": ("fileditchfiles.st", "fileditchfiles.me", "itsfileditchfiles.st")}.get(provider, ())
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def validate_result(result, source):
    provider = provider_for(source)
    parsed = urlsplit(result.get("url", ""))
    if (not provider or not download_host_allowed(provider, result.get("url", ""))
            or parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in {None, 443}):
        raise ValueError("The download service returned an unsupported file address.")
    if provider == "gofile" and not parsed.path.startswith("/download/web/"):
        raise ValueError("GoFile did not return a normal file download address.")
    size = result.get("size")
    if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size <= 0):
        raise ValueError("The download service returned an invalid file size.")
    if not isinstance(result.get("content_id"), str) or not result["content_id"]:
        raise ValueError("The download service did not identify the file.")
    if (not isinstance(result.get("user_agent"), str) or not result["user_agent"]
            or any(ch in result["user_agent"] for ch in "\r\n")):
        raise ValueError("The download service returned an invalid session.")
    # Only cookie domains belonging to this provider can leave the helper.
    cookies = result.get("cookies", [])
    if not isinstance(cookies, list):
        raise ValueError("The download service returned invalid cookies.")
    for cookie in cookies:
        domain = cookie.get("domain", "").lstrip(".").lower()
        if not download_host_allowed(provider, "https://" + domain):
            raise ValueError("The download session contained an unrelated cookie.")
    return result


def helper_command():
    # Preserve mapped-drive aliases used by Windows builds. Resolving a SUBST
    # path expands it back to the runner's long checkout path and can push the
    # bundled Playwright browser beyond Windows' process-launch path limit.
    root = Path(__file__).absolute().parent
    name = "HostResolver.exe" if sys.platform == "win32" else "HostResolver"
    bundled = root / "HostResolver" / name
    if bundled.is_file():
        return [str(bundled)]
    if getattr(sys, "frozen", False):
        raise ValueError("The download browser helper is missing. Reinstall the app.")
    script = root / "host_resolver.py"
    if not script.is_file():
        raise ValueError("The download browser helper is missing. Rebuild the app.")
    # Development only: the Mac backend may use system Python while Playwright
    # is installed in the project's venv. Releases always use the bundled helper.
    python = Path(sys.executable)
    candidate = root / "venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if candidate.is_file():
        python = candidate
    return [str(python), "-u", str(script)]


def kill_helper_tree(process):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=5)
        return
    # The browser may create its own process group. Follow parent IDs rather
    # than assuming every descendant stayed in the helper's process group.
    children = {}
    try:
        table = subprocess.check_output(["/bin/ps", "-axo", "pid=,ppid="], text=True, timeout=5)
        for line in table.splitlines():
            pid, parent = map(int, line.split())
            children.setdefault(parent, []).append(pid)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    descendants = []
    pending = [process.pid]
    while pending:
        parent = pending.pop()
        descendants.append(parent)
        pending.extend(children.get(parent, []))
    for pid in reversed(descendants):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def resolve_source(source, stop=lambda: False, status=lambda message: None, command=None):
    parsed = urlsplit(source)
    if (not provider_for(source) or parsed.scheme != "https" or parsed.username
            or parsed.password or parsed.port not in {None, 443}):
        raise ValueError("Use a supported public HTTPS file-host link.")
    if stop():
        raise ResolutionStopped()
    process = subprocess.Popen(
        command or helper_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
        start_new_session=os.name != "nt",
    )
    messages = queue.Queue()

    def reader():
        try:
            for line in process.stdout:
                if len(line) > 1024 * 1024:
                    messages.put({"event": "failed", "message": "Invalid helper response."})
                    break
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            messages.put(None)

    threading.Thread(target=reader, daemon=True).start()
    try:
        process.stdin.write(json.dumps({"url": source}) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if stop():
                raise ResolutionStopped()
            try:
                message = messages.get(timeout=0.1)
            except queue.Empty:
                continue
            if message is None:
                raise ValueError("The download browser helper stopped unexpectedly.")
            if not isinstance(message, dict):
                raise ValueError("The download browser helper returned invalid data.")
            if message.get("event") == "status":
                status(str(message.get("message", "Preparing download…")))
            elif message.get("event") == "resolved":
                return validate_result(message["file"], source)
            elif message.get("event") == "failed":
                raise ValueError(str(message.get("message", "Could not prepare the download.")))
        raise ValueError("Preparing the download timed out. Retry later.")
    finally:
        # EOF requests graceful cancellation, including browser/context cleanup.
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kill_helper_tree(process)
            process.wait(timeout=5)
        process.stdout.close()
