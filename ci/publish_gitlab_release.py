"""Upload public installer assets and create a GitLab release with a job token."""

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen


def request(url: str, *, data: bytes, content_type: str, method: str = "POST") -> dict:
    headers = {
        "JOB-TOKEN": os.environ["CI_JOB_TOKEN"],
        "Content-Type": content_type,
    }
    with urlopen(Request(url, data=data, headers=headers, method=method), timeout=600) as response:
        body = response.read()
        return json.loads(body) if body else {}


tag = os.environ["RELEASE_TAG"]
version = os.environ["RELEASE_VERSION"]
api = os.environ["CI_API_V4_URL"].rstrip("/")
project_id = os.environ["CI_PROJECT_ID"]
package_root = f"{api}/projects/{project_id}/packages/generic/GameInstaller/{quote(tag)}"
files = [
    Path("dist/GameInstaller.zip"),
    Path("dist/GameInstaller-windows-x64.exe"),
    Path("dist/GameInstaller-macOS-arm64.zip"),
    Path("games.json"),
]
for path in files:
    if not path.is_file():
        raise SystemExit(f"Missing release file: {path}")
hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
manifest = Path("dist/checksums.json")
manifest.write_text(json.dumps(hashes, indent=2) + "\n", encoding="utf-8")
files.append(manifest)

links = []
for path in files:
    package_url = f"{package_root}/{quote(path.name)}"
    request(package_url, data=path.read_bytes(), content_type="application/octet-stream", method="PUT")
    links.append({
        "name": path.name,
        "url": package_url,
        "direct_asset_path": f"/{path.name}",
        "link_type": "package",
    })
    print(f"Uploaded {path.name}")

description = f"""**Version:** {version}
**Commit:** `{os.environ['CI_COMMIT_SHA']}`

### Windows x64
Download `GameInstaller-windows-x64.exe` for the single-file app. `GameInstaller.zip` contains the folder build for Windows or Highball.

### macOS Apple silicon
Download `GameInstaller-macOS-arm64.zip`, extract it, and open Game Installer.app. The app targets macOS 14 or later. macOS may ask you to approve this ad-hoc signed app in Privacy & Security. The archive includes the Highball helper's GPL-3.0 license and matching source.

SHA-256 checksums are in `checksums.json`.
"""
payload = {
    "tag_name": tag,
    "ref": os.environ["CI_COMMIT_SHA"],
    "name": f"Game Installer {tag}",
    "description": description,
    "assets": {"links": links},
}
release = request(
    f"{api}/projects/{project_id}/releases",
    data=json.dumps(payload).encode("utf-8"),
    content_type="application/json",
)
print(f"Published release {release['tag_name']}")
