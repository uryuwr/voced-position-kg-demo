"""
Download O*NET official bulk database (no page scraping).

Source: https://www.onetcenter.org/database.html
License: https://www.onetcenter.org/license_db.html
Default version: 30.3 (override with --version)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crawlers.common.download import download_file
from backend.kg.paths import RAW, REPORTS, ensure_dirs
from backend.kg.provenance import utc_now_iso

ONET_BASE = "https://www.onetcenter.org/dl_files/database"
LICENSE = "O*NET Database License (Creative Commons; see onetcenter.org/license_db.html)"
HOME = "https://www.onetcenter.org/database.html"


def main() -> None:
    parser = argparse.ArgumentParser(description="Download O*NET official database ZIP")
    parser.add_argument("--version", default="30_3", help="e.g. 30_3 for O*NET 30.3")
    parser.add_argument(
        "--format",
        choices=("text", "excel", "both"),
        default="text",
        help="text = tab-delimited (recommended for ingest)",
    )
    args = parser.parse_args()
    ensure_dirs()

    out_dir = RAW / "US" / "onet" / f"db_{args.version}"
    out_dir.mkdir(parents=True, exist_ok=True)

    formats = ["text", "excel"] if args.format == "both" else [args.format]
    results = []
    for fmt in formats:
        url = f"{ONET_BASE}/db_{args.version}_{fmt}.zip"
        dest = out_dir / f"db_{args.version}_{fmt}.zip"
        print(f"→ {url}")
        try:
            meta = download_file(url, dest)
            meta["format"] = fmt
            results.append(meta)
            print(f"  {meta['status']}  bytes={meta['bytes']}  sha256={meta['sha256'][:12]}...")
        except Exception as e:
            print(f"  FAILED: {e}")
            results.append({"status": "error", "url": url, "error": str(e), "format": fmt})

    report = {
        "source_system": "ONET",
        "version": args.version,
        "home": HOME,
        "license": LICENSE,
        "fetched_at": utc_now_iso(),
        "results": results,
    }
    report_path = REPORTS / f"download_onet_{args.version}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report → {report_path}")

    if not any(r.get("status") in ("downloaded", "skipped_exists") for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
