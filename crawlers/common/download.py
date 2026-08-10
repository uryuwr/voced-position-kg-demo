"""Safe official file downloader (rate-limited, resumable-ish)."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

import requests

DEFAULT_UA = (
    "VocEdKG-Bot/0.1 (+research; respectful official bulk download; contact: local-dev)"
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(
    url: str,
    dest: Path,
    *,
    timeout: int = 120,
    min_interval_sec: float = 1.0,
    session: requests.Session | None = None,
) -> dict:
    """Download a file from an official URL. Skips if dest exists and non-empty."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return {
            "status": "skipped_exists",
            "path": str(dest),
            "bytes": dest.stat().st_size,
            "sha256": sha256_file(dest),
            "url": url,
        }

    sess = session or requests.Session()
    headers = {"User-Agent": DEFAULT_UA}
    time.sleep(min_interval_sec)
    with sess.get(url, headers=headers, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        total = 0
        with tmp.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
        tmp.replace(dest)

    return {
        "status": "downloaded",
        "path": str(dest),
        "bytes": total,
        "sha256": sha256_file(dest),
        "url": url,
    }
