"""Generate one tag shared by all jobs in a GitLab pipeline."""

from datetime import datetime, timezone
from pathlib import Path
import os

version = Path("VERSION").read_text(encoding="utf-8").strip()
pipeline = os.environ["CI_PIPELINE_IID"]
tag = f"v{version}-{datetime.now(timezone.utc):%Y%m%d}-{pipeline}"
Path("release.env").write_text(
    f"RELEASE_TAG={tag}\nRELEASE_VERSION={version}\n", encoding="utf-8"
)
print(f"Release tag: {tag}")
