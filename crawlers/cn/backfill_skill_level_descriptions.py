"""
从技能标准 PDF 抽取等级能力描述，回写 kg_node.description / attrs。

Usage:
  python -m crawlers.cn.backfill_skill_level_descriptions --dry-run --limit 50
  python -m crawlers.cn.backfill_skill_level_descriptions --limit 200
  python -m crawlers.cn.backfill_skill_level_descriptions --apply-all
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
from backend.kg.pg_store.client import connect
from backend.kg.pg_store.level_map import product_level_int_from_attrs

# 国标等级词 → 产品 L（了解→专家）
LEVEL_MARKERS = [
    ("五级/初级工", "L1"),
    ("四级/中级工", "L2"),
    ("三级/高级工", "L3"),
    ("二级/技师", "L4"),
    ("一级/高级技师", "L5"),
    ("五级", "L1"),
    ("四级", "L2"),
    ("三级", "L3"),
    ("二级", "L4"),
    ("一级", "L5"),
]


def _pdf_reader():
    try:
        from pypdf import PdfReader  # type: ignore

        return PdfReader
    except ImportError:
        from PyPDF2 import PdfReader  # type: ignore

        return PdfReader


def extract_from_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not text:
        return out
    m = re.search(r"专业能力要求|技能要求|工作要求", text)
    chunk = text[m.start() : m.start() + 12000] if m else text[:12000]
    for zh, code in LEVEL_MARKERS:
        if code in out:
            continue
        pat = re.compile(
            rf"{re.escape(zh)}[^\n]{{0,60}}\n(.{{30,800}}?)(?=\n[一二三四五]级|\n\d+\.|$)",
            re.S,
        )
        mm = pat.search(chunk)
        if not mm:
            pat2 = re.compile(
                rf"{re.escape(zh)}[^\n]{{0,40}}\n(.{{40,500}})",
                re.S,
            )
            mm = pat2.search(chunk)
        if mm:
            desc = re.sub(r"\s+", " ", mm.group(1)).strip()[:500]
            # 过滤纯噪音
            if len(desc) >= 20 and "权重" not in desc[:10]:
                out[code] = desc
    return out


def pdf_text(path: Path, max_pages: int = 30) -> str:
    Reader = _pdf_reader()
    reader = Reader(str(path))
    parts = []
    for page in reader.pages[:max_pages]:
        parts.append(page.extract_text() or "")
    return "\n".join(parts)


def guess_occ_tokens(filename: str) -> list[str]:
    """文件名 → 职业名片段，用于匹配节点 occupation_name / source_file。"""
    stem = Path(filename).stem
    stem = re.sub(r"_?国家职业技能标准.*$", "", stem)
    stem = re.sub(r"_?\d{4}.*$", "", stem)
    stem = stem.replace("_mirror", "").replace("_", " ").strip()
    tokens = [stem]
    if len(stem) > 4:
        tokens.append(stem[:4])
        tokens.append(stem[-4:])
    return [t for t in tokens if t]


def apply_to_db(by_file: list[dict], *, dry_run: bool) -> dict:
    updated = 0
    matched_files = 0
    with connect() as conn:
        for item in by_file:
            descs = item.get("descriptions") or {}
            if not descs:
                continue
            fname = item.get("file") or ""
            tokens = guess_occ_tokens(fname)
            # 找该标准相关 skill_level
            rows = []
            for tok in tokens:
                like = f"%{tok}%"
                found = conn.execute(
                    """
                    SELECT id, name, attrs, description FROM kg_node
                    WHERE type='skill_level' AND region='CN'
                      AND (
                        attrs LIKE %s OR name LIKE %s OR description LIKE %s
                      )
                    LIMIT 200
                    """,
                    (like, like, like),
                ).fetchall()
                if found:
                    rows = found
                    break
            if not rows:
                continue
            matched_files += 1
            for r in rows:
                attrs = r["attrs"]
                if isinstance(attrs, str):
                    try:
                        attrs = json.loads(attrs)
                    except json.JSONDecodeError:
                        attrs = {}
                attrs = dict(attrs or {})
                # 产品档
                pi = product_level_int_from_attrs(attrs, r["name"])
                code = f"L{pi}" if pi else (attrs.get("level_code") or "").upper()
                if code not in descs:
                    # 管道历史 L 码反转时再试
                    raw = str(attrs.get("level_code") or "").upper()
                    if raw in descs:
                        code = raw
                    else:
                        continue
                new_desc = descs[code]
                ld = attrs.get("level_descriptions")
                if not isinstance(ld, dict):
                    ld = {}
                ld[code] = new_desc
                attrs["level_descriptions"] = ld
                attrs["product_level_int"] = pi or int(code[1]) if code.startswith("L") else None
                attrs["desc_source"] = "pdf_professional_ability"
                attrs["desc_source_file"] = fname
                # 占位短描述才覆盖
                old = r.get("description") or ""
                should_write_desc = (
                    len(old) < 40
                    or "权重" in old
                    or "·" in old[:30]
                    or old.count("·") >= 2
                )
                if dry_run:
                    updated += 1
                    continue
                conn.execute(
                    """
                    UPDATE kg_node SET
                      description = CASE WHEN %s THEN %s ELSE description END,
                      attrs = %s,
                      confidence = CASE
                        WHEN confidence IN ('official','derived') THEN confidence
                        ELSE 'derived'
                      END
                    WHERE id = %s
                    """,
                    (
                        should_write_desc,
                        new_desc,
                        json.dumps(attrs, ensure_ascii=False),
                        r["id"],
                    ),
                )
                updated += 1
        if not dry_run:
            conn.commit()
    return {"matched_files": matched_files, "nodes_updated": updated, "dry_run": dry_run}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--apply-all", action="store_true", help="不限制 PDF 数量")
    args = ap.parse_args()
    ensure_dirs()
    pdf_dir = RAW / "CN" / "skill_standards"
    if not pdf_dir.exists():
        print(json.dumps({"error": "no pdf dir", "path": str(pdf_dir)}))
        return 1

    pdfs = sorted(pdf_dir.rglob("*.pdf"))
    if not args.apply_all:
        pdfs = pdfs[: args.limit]

    extracted = []
    for pdf in pdfs:
        try:
            text = pdf_text(pdf)
            descs = extract_from_text(text)
            extracted.append(
                {
                    "file": pdf.name,
                    "path": str(pdf.relative_to(ROOT)) if str(pdf).startswith(str(ROOT)) else pdf.name,
                    "levels_found": list(descs.keys()),
                    "descriptions": descs,
                }
            )
        except Exception as e:
            extracted.append({"file": pdf.name, "error": str(e)[:200]})

    stats = apply_to_db(extracted, dry_run=args.dry_run)
    out = {
        "pdfs": len(pdfs),
        "with_desc": sum(1 for x in extracted if x.get("descriptions")),
        **stats,
        "samples": [
            {k: x[k] for k in ("file", "levels_found", "error") if k in x}
            for x in extracted[:15]
        ],
    }
    path = REPORTS / "backfill_skill_level_descriptions.json"
    # 完整抽取结果过大时只存摘要+带描述文件
    path.write_text(
        json.dumps(
            {
                **out,
                "items_with_desc": [
                    {
                        "file": x["file"],
                        "levels_found": x.get("levels_found"),
                        "descriptions": x.get("descriptions"),
                    }
                    for x in extracted
                    if x.get("descriptions")
                ][:200],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print("report", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
