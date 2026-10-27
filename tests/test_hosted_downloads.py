"""Transfer/session regressions shared by the native Mac and Windows adapters."""
import ast
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, AsyncMock, patch

from download_support import Downloader, Stopped, atomic_json, download_host_name, fetch_with_backup
from host_resolver import ResolutionError, single_file
from host_resolver_client import ResolutionStopped, resolve_source, validate_result


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status, self.headers = status, headers or {}

    def geturl(self):
        return "https://store.gofile.io/download/web/file/game.rar"


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        return self.responses.pop(0)


class HostedTests(unittest.TestCase):
    source = "https://gofile.io/d/fixture"
    payload = b"Rar!" + b"a" * 190000

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "archive"
        self.info = {"url": "https://store.gofile.io/download/web/file/game.rar",
                     "name": "game.rar", "content_id": "file", "size": len(self.payload),
                     "cookies": [{"domain": ".gofile.io", "path": "/", "name": "accountToken",
                                  "value": "secret-test-token", "secure": True}],
                     "user_agent": "FixtureBrowser"}
        self.resolver = Mock(return_value=self.info)

    def response(self, data=None, start=0):
        data = self.payload if data is None else data
        headers = {"Content-Length": str(len(data)), "Last-Modified": "fixture-date"}
        if start:
            headers["Content-Range"] = f"bytes {start}-{len(self.payload)-1}/{len(self.payload)}"
        return Response(data, 206 if start else 200, headers)

    def test_resolver_session_headers_and_original_cache_identity(self):
        opener = Opener(self.response())
        Downloader(opener, resolver=self.resolver).fetch(self.source, self.destination)
        self.assertEqual(opener.requests[0].get_header("User-agent"), "FixtureBrowser")
        self.assertEqual(opener.requests[0].get_header("Referer"), self.source)
        record = self.destination.with_suffix(".json").read_text()
        self.assertNotIn("secret-test-token", record)
        self.assertNotIn("/download/web", record)
        self.assertEqual(json.loads(record)["source"], self.source)
        self.resolver.reset_mock()
        Downloader(opener, resolver=self.resolver).fetch(self.source, self.destination)
        self.resolver.assert_not_called()

    def partial(self):
        stopped = False
        def progress(done, total):
            nonlocal stopped
            stopped = True
        with self.assertRaises(Stopped):
            Downloader(Opener(self.response()), stop=lambda: stopped, progress=progress,
                       resolver=self.resolver).fetch(self.source, self.destination)
        return self.root / "archive.part"

    def test_fresh_session_resumes_with_validator_and_exact_range(self):
        partial = self.partial()
        offset = partial.stat().st_size
        self.info["cookies"][0]["value"] = "fresh-session"
        opener = Opener(self.response(self.payload[offset:], offset))
        Downloader(opener, resolver=self.resolver).fetch(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assertEqual(opener.requests[0].get_header("Range"), f"bytes={offset}-")
        self.assertEqual(opener.requests[0].get_header("If-range"), "fixture-date")

    def test_changed_file_preserves_old_partial_and_starts_from_zero(self):
        partial = self.partial()
        old = partial.read_bytes()
        self.info["content_id"] = "different-file"
        opener = Opener(self.response())
        Downloader(opener, resolver=self.resolver).fetch(self.source, self.destination)
        self.assertIsNone(opener.requests[0].get_header("Range"))
        self.assertEqual(next(self.root.glob("*.previous-*")).read_bytes(), old)

    def test_html_refreshes_once_and_never_saves_html(self):
        opener = Opener(Response(b"<html>session expired</html>", headers={"Content-Type": "text/html"}), self.response())
        Downloader(opener, resolver=self.resolver).fetch(self.source, self.destination)
        self.assertEqual(self.resolver.call_count, 2)
        self.assertEqual(self.destination.read_bytes(), self.payload)

    def test_wrong_length_and_html_do_not_complete(self):
        with self.assertRaises(ValueError):
            Downloader(Opener(self.response(b"short")), resolver=self.resolver).fetch(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        with self.assertRaises(ValueError):
            Downloader(Opener(Response(b"<html>error</html>"), Response(b"<html>error</html>")),
                       resolver=self.resolver).fetch(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_completed_backup_reused_without_resolving_primary(self):
        backup = "https://filekeeper.net/fixture/game.rar"
        self.destination.write_bytes(self.payload)
        atomic_json(self.root / "archive.json", {"source": backup, "complete": True,
                    "sha256": hashlib.sha256(self.payload).hexdigest()})
        downloader = Downloader(Opener(), resolver=self.resolver)
        fetch_with_backup(downloader, self.source, backup, self.destination)
        self.resolver.assert_not_called()

    def test_single_file_rule_and_output_domain_validation(self):
        file = {"type": "file", "canAccess": True}
        self.assertIs(single_file({"children": {"a": file}, "childrenCount": 1}), file)
        for data in [{"children": {"a": file, "b": file}}, {"children": {"a": file}, "childrenCount": 2},
                     {"type": "file", "canAccess": False}]:
            with self.assertRaises(ResolutionError):
                single_file(data)
        invalid = {**self.info, "url": "https://gofile.io.attacker.example/file"}
        with self.assertRaises(ValueError):
            validate_result(invalid, self.source)
        cdn = {**self.info, "url": "https://edge01.fetchio.cc/file", "cookies": []}
        validate_result(cdn, "https://filekeeper.net/fixture")

    def test_host_labels_name_services_without_exposing_urls(self):
        for url, expected in [
            ("https://store-na-phx-5.gofile.io/download/web/file/game.rar", "GoFile"),
            ("https://filekeeper.net/file/game.rar", "FileKeeper"),
            ("https://edge01.fetchio.cc/file?token=secret", "FileKeeper"),
            ("https://drive.usercontent.google.com/download?id=file", "Google Drive"),
            ("https://drive.google.com/file/d/file/view", "Google Drive"),
            ("https://archive.org/download/item/file.zip", "Internet Archive"),
            ("https://ia800100.us.archive.org/file.zip", "Internet Archive"),
            ("https://pixeldrain.com/u/file", "Pixeldrain"),
            ("https://files.catbox.moe/file.zip", "Catbox"),
            ("https://fileditchfiles.me/alpha/file.rar", "FileDitch"),
            ("https://fileditchfiles.st/alpha/file.rar", "FileDitch"),
            ("https://itsfileditchfiles.st/alpha/file.rar", "FileDitch"),
            ("https://rootz.so/d/file", "Rootz"),
            ("https://vikingfile.com/f/file", "VikingFile"),
            ("https://user:secret@downloads.example.test/file?token=secret", "Custom link"),
            ("https://gofile.io.attacker.example/file", "Custom link"),
            ("", "Custom link"),
        ]:
            with self.subTest(url=url):
                self.assertEqual(download_host_name(url), expected)

    def test_helper_cancellation_closes_process_and_its_input(self):
        marker = self.root / "stopped"
        script = ("import sys,json,pathlib;sys.stdin.readline();"
                  "print(json.dumps({'event':'status','message':'ready'}),flush=True);"
                  f"sys.stdin.readline();pathlib.Path({str(marker)!r}).write_text('closed')")
        cancelled = threading.Event()
        with self.assertRaises(ResolutionStopped):
            resolve_source(self.source, stop=cancelled.is_set,
                           status=lambda message: cancelled.set(), command=[sys.executable, "-u", "-c", script])
        self.assertEqual(marker.read_text(), "closed")

    def test_link_preparation_retries_transient_failures_three_times(self):
        import host_resolver
        contexts = [types.SimpleNamespace(close=AsyncMock()) for _ in range(3)]
        browser = types.SimpleNamespace(new_context=AsyncMock(side_effect=contexts), close=AsyncMock())
        manager = types.SimpleNamespace(chromium=types.SimpleNamespace(launch=AsyncMock(return_value=browser)))
        scope = Mock()
        scope.__aenter__ = AsyncMock(return_value=manager)
        scope.__aexit__ = AsyncMock(return_value=False)
        module = types.ModuleType("playwright.async_api")
        module.async_playwright = Mock(return_value=scope)
        with patch.dict(sys.modules, {"playwright.async_api": module}), \
                patch.object(host_resolver, "gofile", new=AsyncMock(side_effect=[TimeoutError(), TimeoutError(), self.info])) as get, \
                patch.object(host_resolver, "emit"), patch.object(host_resolver.asyncio, "sleep", new=AsyncMock()):
            result = asyncio.run(host_resolver.resolve(self.source))
        self.assertEqual(result["content_id"], "file")
        self.assertEqual(get.await_count, 3)
        for context in contexts:
            context.close.assert_awaited_once()
        browser.close.assert_awaited_once()

    def test_windows_adapter_uses_shared_transfer_and_preserves_progress(self):
        # Execute the actual Windows worker method without importing winreg/Qt
        # on the Mac CI runner. The built Windows app has a separate smoke check.
        tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "InstallWorker")
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_download")
        context = {"os": os, "time": time, "_log": Mock(), "provider_for": lambda url: "gofile",
                   "DOWNLOAD_CHUNK": 65536, "DownloadStopped": Stopped,
                   "download_host_name": download_host_name}
        context["HostedDownloader"] = lambda **kwargs: Downloader(Opener(self.response()), resolver=self.resolver, **kwargs)
        exec(compile(ast.Module(body=[method], type_ignores=[]), "main.py", "exec"), context)
        worker = Mock()
        worker._cancel = threading.Event()
        worker._stage_progress = [0, 0]
        signal = Mock()
        self.assertTrue(context["_download"](worker, self.source, str(self.destination), "Downloading", signal))
        self.assertEqual(self.destination.read_bytes(), self.payload)
        signal.emit.assert_called_with(100)
        worker.speed_text.emit.assert_called()
        self.assertTrue(all("via GoFile" in call.args[0] for call in worker._emit_status.call_args_list))


if __name__ == "__main__":
    unittest.main()
