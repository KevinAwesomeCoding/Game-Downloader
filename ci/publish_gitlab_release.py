"""Upload public installer assets and create a GitLab release with a job token."""

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
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
windows_files = [Path("dist/GameInstaller.zip"), Path("dist/GameInstaller-windows-x64.exe")]
macos_files = [Path("dist/GameInstaller-macOS-arm64.zip")]
windows_built = all(p.is_file() for p in windows_files)
macos_built = all(p.is_file() for p in macos_files)
if not (windows_built or macos_built):
    raise SystemExit('No platform build artifacts were supplied.')
if not (windows_built and macos_built) and os.environ.get('PLATFORM_SPLIT_ENABLED') != 'true':
    raise SystemExit('Migration release requires both platform builds.')
previous = None
if not (windows_built and macos_built):
    previous_url = os.environ['CI_PROJECT_URL'].rstrip('/') + '/-/releases/permalink/latest/downloads/release.json'
    try:
        with urlopen(Request(previous_url, headers={'JOB-TOKEN': os.environ['CI_JOB_TOKEN']}), timeout=30) as response:
            previous = json.load(response)
    except Exception as exc:
        raise SystemExit(f'Cannot safely carry forward the previous platform release: {exc}')
files = (windows_files if windows_built else []) + (macos_files if macos_built else []) + [Path('games.json')]
for path in files:
    if not path.is_file():
        raise SystemExit(f"Missing release file: {path}")
hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
manifest = Path("dist/checksums.json")
manifest.write_text(json.dumps(hashes, indent=2) + "\n", encoding="utf-8")
files.append(manifest)

description = f"""**Version:** {version}
**Commit:** `{os.environ['CI_COMMIT_SHA']}`
**Change:** {os.environ.get('CI_COMMIT_TITLE', 'Change details unavailable')}

### Windows x64
Download `GameInstaller-windows-x64.exe` for the single-file app. `GameInstaller.zip` contains the folder build for Windows or Highball.

### macOS Apple silicon
Download `GameInstaller-macOS-arm64.zip`, extract it, and open Game Installer.app. The app targets macOS 14 or later. macOS may ask you to approve this ad-hoc signed app in Privacy & Security. The archive includes the Highball helper's GPL-3.0 license and matching source.

SHA-256 checksums are in `checksums.json`.

BZZHR and FileDitch host-page support is experimental. Live SteamRIP and host
download flows have not yet been verified and may fail on browser challenges.
"""
project_url = os.environ["CI_PROJECT_URL"].rstrip("/")
release_url = f"{project_url}/-/releases/{quote(tag)}"
release_manifest = Path("dist/release.json")
def git_output(*args):
    try:
        return subprocess.check_output(['git', *args], text=True, encoding='utf-8', errors='replace').splitlines()
    except (OSError, subprocess.CalledProcessError):
        return []

# First-parent history keeps release notes aligned with the published main line.
history = []
for line in git_output('log', '--first-parent', '-200', '--format=%H%x1f%s', os.environ['CI_COMMIT_SHA']):
    if '\x1f' in line:
        sha, title = line.split('\x1f', 1)
        history.append({'sha': sha, 'title': title})
tag_commits = {}
for line in git_output('for-each-ref', '--format=%(refname:short)%09%(*objectname)%09%(objectname)', 'refs/tags'):
    fields = line.split('\t')
    if len(fields) == 3 and fields[0].startswith('v'):
        tag_commits[fields[0]] = fields[1] or fields[2]
release_data = {
    "tag_name": tag,
    "commit_title": os.environ.get("CI_COMMIT_TITLE", ""),
    "commit_sha": os.environ["CI_COMMIT_SHA"],
    "created_at": datetime.now(timezone.utc).isoformat(),
    "commit_history": history,
    "tag_commits": tag_commits,
    "html_url": release_url,
    "body": description,
    "assets": [{
        "name": path.name,
        "browser_download_url": f"{release_url}/downloads/{quote(path.name)}",
        "digest": f"sha256:{hashes[path.name]}",
    } for path in files if path.name in hashes],
}
# Initial migration release builds both platforms. Preserve the legacy fields
# so older installations can still locate their installer.
release_data['platforms'] = {}
carried_assets = []
for platform, names in {
    'windows': {'GameInstaller.zip', 'GameInstaller-windows-x64.exe'},
    'macos': {'GameInstaller-macOS-arm64.zip'},
}.items():
    built = windows_built if platform == 'windows' else macos_built
    if not built:
        prior = (previous.get('platforms') or {}).get(platform)
        if not isinstance(prior, dict) or not prior.get('assets'):
            raise SystemExit(f'Previous {platform} release metadata is missing; build both platforms first.')
        release_data['platforms'][platform] = prior
        carried_assets.extend(prior['assets'])
        continue
    release_data['platforms'][platform] = {
        **{key: release_data[key] for key in ('tag_name', 'html_url', 'body',
                                               'commit_title', 'commit_sha', 'created_at',
                                               'commit_history', 'tag_commits')},
        'assets': [asset for asset in release_data['assets'] if asset['name'] in names],
    }
# Older clients read only top-level assets. Keep the previous installer links
# available to them even though their shared version may trigger a redundant
# update notice until they install the migration release.
release_data['assets'].extend(carried_assets)
for asset in carried_assets:
    digest = str(asset.get('digest') or '')
    if not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest):
        raise SystemExit(f"Previous asset {asset.get('name', '')} has no valid SHA-256 digest.")
    hashes[asset['name']] = digest.removeprefix('sha256:')
manifest.write_text(json.dumps(hashes, indent=2) + "\n", encoding="utf-8")
release_manifest.write_text(json.dumps(release_data, indent=2) + "\n", encoding="utf-8")
files.append(release_manifest)

links = []
for asset in carried_assets:
    links.append({
        'name': asset['name'],
        'url': asset['browser_download_url'],
        'direct_asset_path': '/' + asset['name'],
        'link_type': 'package',
    })
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
