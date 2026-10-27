"""Resumable HTTPS transfers. No catalog content is executed."""
import hashlib
import http.cookiejar
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from host_resolver_client import ResolutionStopped, provider_for, resolve_source, download_host_allowed


class Stopped(Exception):
    pass


def checkpoint(stop):
    if stop():
        raise Stopped("Stopped; downloaded files have been kept.")


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def sha256(path, stop=lambda: False):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            checkpoint(stop)
            digest.update(data)
    return digest.hexdigest()


def download_host_name(url):
    """User-facing service name; never display a signed URL or session token."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    services = (
        (("gofile.io",), "GoFile"),
        (("filekeeper.net", "fetchio.cc"), "FileKeeper"),
        (("drive.google.com", "drive.usercontent.google.com", "docs.google.com",
          "googleusercontent.com"), "Google Drive"),
        (("archive.org",), "Internet Archive"),
        (("pixeldrain.com",), "Pixeldrain"),
        (("catbox.moe",), "Catbox"),
    )
    for domains, name in services:
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return name
    return "Custom link"


def https_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTPS download links are supported.")
    return url


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        https_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


class HostedRedirect(HTTPSRedirect):
    def __init__(self, provider):
        self.provider = provider

    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not download_host_allowed(self.provider, newurl):
            raise ValueError("The file host redirected to an unrelated service.")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def add_session_cookies(jar, cookies):
    for value in cookies:
        domain = value["domain"]
        expires = value.get("expires", -1)
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=value["name"], value=value["value"], port=None,
            port_specified=False, domain=domain, domain_specified=domain.startswith("."),
            domain_initial_dot=domain.startswith("."), path=value.get("path", "/"),
            path_specified=True, secure=value.get("secure", True),
            expires=int(expires) if expires > 0 else None, discard=expires <= 0,
            comment=None, comment_url=None, rest={}, rfc2109=False,
        ))


class DownloadForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None
        self.fields = {}
        self.in_form = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form" and self.action is None:
            self.action = attributes.get("action", "")
            self.in_form = True
        if tag == "input" and self.in_form and attributes.get("name"):
            self.fields[attributes["name"]] = attributes.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_form = False


def drive_confirmation(html, current):
    low = html.lower()
    if any(word in low for word in ("download quota", "quota exceeded", "too many users", "download limit")):
        raise ValueError("Google Drive download quota reached. Retry later; partial files are preserved.")
    parser = DownloadForm()
    parser.feed(html)
    action = urllib.parse.urljoin(current, parser.action or "")
    if (not parser.action or urllib.parse.urlsplit(action).hostname not in
            {"drive.google.com", "drive.usercontent.google.com", "docs.google.com"}):
        raise ValueError("Google Drive returned a sign-in/error page, not a public download confirmation.")
    https_url(action)
    fields = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(action).query))
    fields.update(parser.fields)
    fields.setdefault("confirm", "t")
    return urllib.parse.urlunsplit(urllib.parse.urlsplit(action)._replace(query=urllib.parse.urlencode(fields)))


def canonical_source(url):
    https_url(url)
    parts = urllib.parse.urlsplit(url)
    if parts.hostname == "pixeldrain.com" and re.fullmatch(r"/u/[A-Za-z0-9]+/?", parts.path):
        return "https://pixeldrain.com/api/file/" + parts.path.strip("/").split("/")[1] + "?download"
    if parts.hostname == "drive.google.com":
        match = re.search(r"/file/d/([^/]+)", parts.path)
        file_id = match.group(1) if match else dict(urllib.parse.parse_qsl(parts.query)).get("id")
        if file_id:
            return "https://drive.usercontent.google.com/download?" + urllib.parse.urlencode(
                {"id": file_id, "export": "download", "confirm": "t"})
    return url


class Downloader:
    def __init__(self, opener=None, stop=lambda: False, progress=lambda done, total: None,
                 status=lambda message: None, resolver=resolve_source):
        self.injected_opener = opener is not None
        self.opener = opener or urllib.request.build_opener(
            HTTPSRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.stop = stop
        self.progress = progress
        self.status = status
        self.resolver = resolver

    def fetch(self, source, destination, expected_hash=None, _restart=False, _refresh=False):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(destination.name + ".part")
        record = destination.with_name(destination.name + ".json")
        source = canonical_source(source)
        metadata = {}
        try:
            metadata = json.loads(record.read_text())
            if metadata.get("source") != source:
                metadata = {}
        except (OSError, ValueError):
            pass
        if destination.exists():
            digest = sha256(destination, self.stop)
            if not metadata.get("complete") or digest != metadata.get("sha256"):
                raise ValueError("Cached archive is not verified; choose a new job/cache rather than reusing it.")
            if expected_hash and digest != expected_hash:
                raise ValueError("Archive differs from the recorded tested build; no recipe was applied.")
            return digest

        provider = provider_for(source)
        resolved = None
        opener = self.opener
        if provider:
            checkpoint(self.stop)
            try:
                resolved = self.resolver(source, stop=self.stop, status=self.status)
            except ResolutionStopped as exc:
                raise Stopped("Stopped while preparing the download.") from exc
            checkpoint(self.stop)
            if not self.injected_opener:
                jar = http.cookiejar.CookieJar()
                add_session_cookies(jar, resolved["cookies"])
                opener = urllib.request.build_opener(HostedRedirect(provider),
                                                     urllib.request.HTTPCookieProcessor(jar))

        def preserve_partial():
            if part.exists():
                part.rename(part.with_name(part.name + f".previous-{time.time_ns()}"))

        offset = part.stat().st_size if part.exists() else 0
        if offset and resolved and (metadata.get("content_id") != resolved["content_id"]
                                   or metadata.get("expected_size") != resolved.get("size")):
            preserve_partial()
            offset = 0
        validator = metadata.get("etag") or metadata.get("modified")
        if validator and validator.startswith("W/"):
            validator = metadata.get("modified")  # If-Range does not accept a weak entity tag.
        if offset and not (validator or expected_hash):
            preserve_partial()  # Cannot safely append an unidentified representation.
            offset = 0
        headers = {"User-Agent": "GameInstallerMacPreview/0.2", "Accept-Encoding": "identity"}
        if resolved:
            headers.update({"User-Agent": resolved["user_agent"], "Referer": source})
        if offset:
            headers["Range"] = f"bytes={offset}-"
            if validator:
                headers["If-Range"] = validator
        url = resolved["url"] if resolved else source
        response = None
        try:
            for attempt in range(3):
                checkpoint(self.stop)
                try:
                    response = opener.open(urllib.request.Request(url, headers=headers), timeout=15)
                except urllib.error.HTTPError as exc:
                    if provider and exc.code in (401, 403) and not _refresh:
                        exc.close()
                        return self.fetch(source, destination, expected_hash, _restart, _refresh=True)
                    if exc.code == 416 and offset and not _restart:
                        # Do not promote an unproven partial just because the server says 416.
                        exc.close()
                        preserve_partial()
                        return self.fetch(source, destination, expected_hash, _restart=True, _refresh=_refresh)
                    detail = exc.read(128 * 1024).decode("utf-8", "replace")
                    exc.close()
                    if "quota" in detail.lower() or "too many users" in detail.lower():
                        raise ValueError("Download quota or traffic limit reached; retry later. Partial files are preserved.")
                    raise ValueError(f"Download server returned HTTP {exc.code}. Check sharing/access or retry later.")
                first = response.read(64 * 1024)
                is_html = ("text/html" in response.headers.get("Content-Type", "").lower() or
                           first.lstrip().lower().startswith((b"<!doctype html", b"<html")))
                if not is_html:
                    break
                html = (first + response.read(512 * 1024)).decode("utf-8", "replace")
                current = response.geturl()
                response.close()
                if provider and not _refresh:
                    return self.fetch(source, destination, expected_hash, _restart, _refresh=True)
                if urllib.parse.urlsplit(source).hostname not in {"drive.google.com", "drive.usercontent.google.com"}:
                    raise ValueError("The link returned an HTML page instead of an archive.")
                url = drive_confirmation(html, current)
            else:
                raise ValueError("Google Drive confirmation did not yield an archive. Retry later.")

            status = response.status
            if status not in (200, 206):
                raise ValueError(f"Unsupported download response HTTP {status}.")
            length = int(response.headers.get("Content-Length", "0"))
            total = length
            if status == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                if not match or int(match[1]) != offset or int(match[2]) < offset:
                    raise ValueError("Server returned an invalid resume range; partial files were preserved.")
                total = int(match[3])
                if int(match[2]) + 1 != total or (length and length != total - offset):
                    raise ValueError("Server returned an incomplete or inconsistent resume range.")
                returned_validator = response.headers.get("Last-Modified") if validator == metadata.get("modified") else response.headers.get("ETag")
                if validator and returned_validator and validator != returned_validator:
                    raise ValueError("Download changed during resume; partial files were preserved.")
            elif offset:
                preserve_partial()  # A 200 means If-Range failed or Range was ignored.
                offset = 0
            expected_size = resolved.get("size") if resolved else None
            if expected_size:
                if total and total != expected_size:
                    raise ValueError("The download size changed; partial files were preserved.")
                total = expected_size
            identity = {"content_id": resolved["content_id"], "expected_size": expected_size} if resolved else {}
            atomic_json(record, {"source": source, "etag": response.headers.get("ETag"),
                                 "modified": response.headers.get("Last-Modified"), "total": total, **identity})
            done = offset
            last_report = 0
            with part.open("ab" if offset else "wb") as stream:
                chunk = first
                while chunk:
                    checkpoint(self.stop)
                    stream.write(chunk)
                    done += len(chunk)
                    if total and done > total:
                        raise ValueError("The download exceeded its expected length; installation stopped.")
                    now = time.monotonic()
                    if now - last_report >= 0.2 or (total and done == total):
                        self.progress(done, total)
                        last_report = now
                    chunk = response.read(64 * 1024)
            checkpoint(self.stop)
            if total and done != total:
                raise ValueError("Transfer ended before its expected length; retry to resume.")
            digest = sha256(part, self.stop)
            if expected_hash and expected_hash != digest:
                raise ValueError("SHA-256 mismatch for the recorded tested build; files kept and installation stopped.")
            part.replace(destination)
            atomic_json(record, {"source": source, "complete": True, "sha256": digest, "total": done, **identity})
            return digest
        finally:
            if response is not None:
                response.close()


def fetch_with_backup(downloader, source, backup_source, destination, on_fallback=None):
    """Try the primary URL first, then an independent backup URL if it fails.

    Each source has its own partial file and resume metadata. This prevents
    bytes from different hosts from being combined if one transfer stops part
    way through.
    """
    if not backup_source:
        return downloader.fetch(source, destination)
    # A completed backup was promoted to the final destination on an earlier
    # run. Verify and reuse it without requesting either host again.
    destination = Path(destination)
    try:
        record = json.loads(destination.with_name(destination.name + ".json").read_text())
    except (OSError, ValueError):
        record = {}
    if destination.exists() and record.get("complete") and record.get("source") in {
            canonical_source(source), canonical_source(backup_source)}:
        return downloader.fetch(record["source"], destination)
    try:
        return downloader.fetch(source, destination)
    except Stopped:
        raise
    except Exception as primary_error:
        if on_fallback:
            on_fallback(primary_error)
        destination = Path(destination)
        backup_destination = destination.with_name(destination.name + ".backup")
        try:
            digest = downloader.fetch(backup_source, backup_destination)
            backup_destination.replace(destination)
            backup_record = backup_destination.with_name(backup_destination.name + ".json")
            destination_record = destination.with_name(destination.name + ".json")
            if backup_record.exists():
                backup_record.replace(destination_record)
            return digest
        except Stopped:
            raise
        except Exception as backup_error:
            raise ValueError(
                "The primary download failed and the backup link also failed. "
                f"Primary: {primary_error}; backup: {backup_error}"
            ) from backup_error


def parts(value):
    if not value:
        return []
    if isinstance(value, str):
        value = re.split(r"[,\n]", value)
    result = []
    for item in value:
        url = item.get("url", "") if isinstance(item, dict) else item
        if url.strip():
            result.append({"url": url.strip(), "name": item.get("name", "") if isinstance(item, dict) else ""})
    def order(entry):
        match = re.search(r"\.part0*(\d+)\.", entry["name"], re.IGNORECASE)
        return (0, int(match[1])) if match else (1, 0)
    return sorted(result, key=order)
