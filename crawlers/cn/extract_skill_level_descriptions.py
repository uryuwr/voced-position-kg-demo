"""P1-1 BR-04：从国家职业技能标准 PDF 抽「专业能力要求」按档写入描述（试点）。

Usage:
  python -m crawlers.cn.extract_skill_level_descriptions --dry-run
  python -m crawlers.cn.extract_skill_level_descriptions --limit 20

说明：解析启发式；失败则跳过。入库需另跑属性回填或管理台编辑。
产出 reports/extract_skill_level_descriptions.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.kg.paths import RAW, REPORTS, ensure_dirs

LEVEL_MARKERS = [
    ("五级", "L1"),
    ("四级", "L2"),
    ("三级", "L3"),
    ("二级", "L4"),
    ("一级", "L5"),
]


def extract_from_text(text: str) -> dict[str, str]:
    """粗抽：在「专业能力要求」附近按等级切段。"""
    out: dict[str, str] = {}
    if not text:
        return out
    # 找专业能力要求章节
    m = re.search(r"专业能力要求", text)
    chunk = text[m.start() : m.start() + 8000] if m else text[:8000]
    for zh, code in LEVEL_MARKERS:
        # 等级标题后取一段
        pat = re.compile(
            rf"{zh}\s*/?\s*(?:初级工|中级工|高级工|技师|高级技师)?[^\n]{{0,40}}\n(.{{40,600}})",
            re.S,
        )
        mm = pat.search(chunk)
        if mm:
            desc = re.sub(r"\s+", " ", mm.group(1)).strip()[:400]
            if desc:
                out[code] = desc
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()
    ensure_dirs()
    pdf_dir = RAW / "CN" / "skill_standards"
    if not pdf_dir.exists():
        print(json.dumps({"error": "no skill_standards dir", "path": str(pdf_dir)}))
        return 1
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            print(json.dumps({"error": "pip install pypdf"}))
            return 1

    results = []
    pdfs = sorted(pdf_dir.rglob("*.pdf"))[: args.limit]
    for pdf in pdfs:
        try:
            reader = PdfReader(str(pdf))
            text = ""
            for page in reader.pages[:25]:
                text += page.extract_text() or ""
            descs = extract_from_text(text)
            results.append(
                {
                    "file": pdf.name,
                    "levels_found": list(descs.keys()),
                    "descriptions": descs if not args.dry_run else {k: v[:80] for k, v in descs.items()},
                }
            )
        except Exception as e:
            results.append({"file": pdf.name, "error": str(e)[:200]})

    out_path = REPORTS / "extract_skill_level_descriptions.json"
    out_path.write_text(
        json.dumps(
            {"count": len(results), "dry_run": args.dry_run, "items": results},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"wrote": str(out_path), "files": len(results)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
