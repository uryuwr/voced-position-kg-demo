"""
批量下载教育部职业教育专业教学标准 PDF（公开门户）。

Usage:
  python -m crawlers.cn.batch_download_moe_standards
  python -m crawlers.cn.batch_download_moe_standards --limit 50
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.paths import RAW, REPORTS, ensure_dirs

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EducationalKG/1.0"
URL_LIST = RAW / "CN" / "course" / "moe_2025" / "pdf_urls.txt"
DEST = RAW / "CN" / "course" / "moe_2025" / "pdfs"


def safe_name(url: str, idx: int) -> str:
    # .../510203_xxx/P020....pdf or end filename
    m = re.search(r"/(\d{6})[^/]*/([^/]+\.pdf)$", url, re.I)
    if m:
        return f"{m.group(1)}_{m.group(2)}"
    m2 = re.search(r"/([^/]+\.pdf)$", url, re.I)
    if m2:
        return f"{idx:04d}_{m2.group(1)}"
    return f"{idx:04d}.pdf"


def download_one(url: str, path: Path, timeout: int = 60) -> tuple[bool, str]:
    if path.exists() and path.stat().st_size > 5000:
        head = path.read_bytes()[:5]
        if head.startswith(b"%PDF"):
            return True, "skip"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "http://www.moe.gov.cn/"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except Exception as e:
        return False, str(e)
    if not data.startswith(b"%PDF"):
        return False, "not_pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True, f"ok:{len(data)}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="0=all")
    parser.add_argument("--sleep", type=float, default=0.15)
    args = parser.parse_args()
    ensure_dirs()
    DEST.mkdir(parents=True, exist_ok=True)
    if not URL_LIST.exists():
        print(f"missing {URL_LIST}")
        sys.exit(1)
    urls = [u.strip() for u in URL_LIST.read_text(encoding="utf-8").splitlines() if u.strip()]
    if args.limit:
        urls = urls[: args.limit]
    ok = skip = fail = 0
    fails: list[str] = []
    for i, url in enumerate(urls, 1):
        name = safe_name(url, i)
        path = DEST / name
        good, msg = download_one(url, path)
        if good and msg == "skip":
            skip += 1
        elif good:
            ok += 1
        else:
            fail += 1
            fails.append(f"{url}\t{msg}")
        if i % 50 == 0:
            print(f"progress {i}/{len(urls)} ok={ok} skip={skip} fail={fail}")
        time.sleep(args.sleep)
    report = {
        "total_urls": len(urls),
        "downloaded": ok,
        "skipped_existing": skip,
        "failed": fail,
        "dest": str(DEST),
        "fail_sample": fails[:30],
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    import json

    (REPORTS / "batch_download_moe_standards.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
