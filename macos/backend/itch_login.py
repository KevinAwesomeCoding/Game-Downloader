"""Itch OAuth sign-in for a clean Among Us profile; never prints credentials."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
import webbrowser


def token_destination(highball, receipt):
    registration = receipt.get("registration") or {}
    bottle = registration.get("bottle")
    if receipt.get("gameID") != "among-us" or not registration.get("complete") or not bottle:
        raise ValueError("Add Among Us to a Highball environment first.")
    highball.ready(bottle)
    if Path(bottle).name != bottle or bottle in {".", ".."}:
        raise ValueError("Invalid Highball environment name.")
    drive = (Path(highball.home) / "bottles" / bottle / "drive_c").resolve(strict=True)
    users = drive / "users"
    candidates = [p for p in users.iterdir() if p.is_dir() and not p.is_symlink()
                  and p.name.casefold() not in {"public", "all users", "default", "default user"}]
    if len(candidates) != 1:
        raise ValueError("Could not identify one Windows user in this environment; no login was changed.")
    root = candidates[0].resolve(strict=True)
    target = root / "AppData/LocalLow/Innersloth/Among Us/itch"
    if not target.resolve().is_relative_to(root):
        raise ValueError("The Among Us data folder points outside its Highball user; no login was changed.")
    return target


def sign_in(client_id, destination, notify=lambda message: None, opener=webbrowser.open,
            exchange=urlopen, timeout=300, authorization_url=None):
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", client_id or ""):
        raise ValueError("Set your own itch.io OAuth client ID in Settings first.")
    destination = Path(destination)
    user_root = destination.parents[4].resolve(strict=True)
    if not destination.parent.resolve().is_relative_to(user_root):
        raise ValueError("The game data folder points outside the Highball user; no login was changed.")
    if destination.exists() or destination.is_symlink():
        raise ValueError("An Among Us itch login already exists. It was kept; use a clean Highball environment to test a new login.")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            params = parse_qs(parsed.query)
            if parsed.path != "/callback" or params.get("state") != [state]:
                self.send_error(400, "Invalid callback")
                return
            self.server.result = params
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"Return to Game Installer to see the result.")

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 1
        server.result = None
        redirect = f"http://127.0.0.1:{server.server_port}/callback"
        query = urlencode({"client_id": client_id, "scope": "profile:me", "redirect_uri": redirect,
                           "response_type": "code", "code_challenge": challenge,
                           "code_challenge_method": "S256", "state": state})
        notify("Approve Game Installer in your browser; waiting for itch.io…")
        url = "https://itch.io/user/oauth?" + query
        if authorization_url is not None:
            authorization_url(url)
        try:
            opened = opener(url)
        except Exception:
            opened = False
        if not opened and authorization_url is None:
            raise ValueError("Could not open the browser for itch.io sign-in.")
        deadline = time.monotonic() + timeout
        while server.result is None and time.monotonic() < deadline:
            server.handle_request()
        if server.result is None:
            raise ValueError("Itch.io sign-in timed out. Retry and complete authorization in the browser.")
        codes = server.result.get("code", [])
        if len(codes) != 1 or not codes[0]:
            raise ValueError("Itch.io sign-in was declined or did not return an authorization code.")
        form = urlencode({"grant_type": "authorization_code", "code": codes[0],
                          "code_verifier": verifier, "redirect_uri": redirect,
                          "client_id": client_id}).encode()
        try:
            with exchange(Request("https://api.itch.io/oauth/token", data=form, method="POST"), timeout=20) as response:
                token = json.load(response).get("access_token")
        except Exception as exc:
            raise ValueError("Itch.io could not exchange the authorization code. Check the registered callback and retry.") from exc
        if not isinstance(token, str) or not token or len(token) > 16384 or any(c in token for c in "\r\n\x00"):
            raise ValueError("Itch.io did not return a usable login token.")
        if not destination.parent.resolve().is_relative_to(user_root):
            raise ValueError("The game data folder changed during sign-in; no login was saved.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.parent.resolve().is_relative_to(user_root):
            raise ValueError("The game data folder changed during sign-in; no login was saved.")
        try:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w") as output:
                output.write(token)
        except FileExistsError as exc:
            raise ValueError("A login file appeared during sign-in. It was kept unchanged.") from exc
        notify("Login saved. Launch Among Us to check whether this OAuth application is accepted; game sign-in is not yet verified.")
