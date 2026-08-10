"""
Ingest 人社部《职业分类大典（2022）》分类体系表 → occupation nodes (CN).

Source: data/raw/CN/mohrss/*分类体系表*.pdf
  - 细类（职业）代码 N-NN-NN-NN → occupation 节点
  - 主键 source_id = 细类代码（如 2-02-38-01）
  - 节点 id = CN:occupation:MOHRSS_CN:{code}
  - 保留中类/小类路径、GBM 码、绿色 L / 数字 S 标识

Usage:
  python -m crawlers.cn.ingest_occupations --dry-run
  python -m crawlers.cn.ingest_occupations
  python -m backend.kg.neo4j_store.migrate
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, stats, upsert_nodes
from backend.kg.paths import RAW, REPORTS, ensure_dirs
from backend.kg.provenance import make_node_id, utc_now_iso

SOURCE_SYSTEM = "MOHRSS_CN"
REGION = "CN"
LICENSE = "人社部公开发布《职业分类大典》公示材料；使用时请注明出处（mohrss.gov.cn）"
SOURCE_HOME = "https://www.mohrss.gov.cn/"
SOURCE_URL = (
    "https://www.mohrss.gov.cn/SYrlzyhshbzb/zcfg/SYzhengqiuyijian/"
    "202207/t20220714_457833.html"
)
CATALOG = "中华人民共和国职业分类大典（2022年版）· 公示稿分类体系表"
EDITION = "2022"

FINE_RE = re.compile(r"^(\d-\d{2}-\d{2}-\d{2})\s*(.*)$")
MINOR_RE = re.compile(
    r"^(\d-\d{2}-\d{2})\s*(?:\(\s*GBM\s*(\d+)\s*\))?\s*(.*)$"
)
MID_RE = re.compile(r"^(\d-\d{2})\s*(?:\(\s*GBM\s*(\d+)\s*\))?\s*(.*)$")
MAJOR_RE = re.compile(
    r"^第([一二三四五六七八])大类\s*(\d)\s*[（(]\s*GBM\s*(\d+)\s*[）)]\s*(.*)$"
)
FLAG_ONLY = re.compile(r"^(L/S|L|S)$")
CODE_START = re.compile(r"^(\d-\d{2}(?:-\d{2}){0,2})\b|^第[一二三四五六七八]大类")

NOISE = {
    "中类",
    "小类",
    "细类(职业)",
    "分类体系表",
    "续表",
    "中华人民共和国",
    "职业分类大典（2022年版）",
}


def mohrss_dir() -> Path:
    return RAW / "CN" / "mohrss"


def find_taxonomy_pdf() -> Path | None:
    d = mohrss_dir()
    for pat in ("*分类体系表*.pdf", "*体系表*.pdf", "*大典*2022*.pdf"):
        hits = sorted(d.glob(pat))
        # prefer 体系表 over 正文
        for h in hits:
            if "正文" in h.name:
                continue
            if "分类体系" in h.name or "体系表" in h.name:
                return h
        if hits:
            return hits[0]
    return None


def extract_pdf_lines(path: Path) -> list[str]:
    import fitz

    doc = fitz.open(path)
    try:
        raw = "\n".join(doc[i].get_text("text") for i in range(doc.page_count))
    finally:
        doc.close()

    # (GBM\n\n10100) → (GBM 10100)
    raw = re.sub(r"\(GBM\s*\n+\s*", "(GBM ", raw)
    raw = re.sub(r"\n+", "\n", raw)

    lines: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s in NOISE:
            continue
        if re.fullmatch(r"\d+", s):
            continue
        lines.append(s)

    # 续行合并：无代码开头的行接到上一行；独立 L/S 标记并入上一行
    joined: list[str] = []
    for s in lines:
        if FLAG_ONLY.match(s):
            if joined:
                joined[-1] = f"{joined[-1]} {s}"
            continue
        if CODE_START.match(s):
            joined.append(s)
        elif joined:
            joined[-1] = joined[-1] + s
        else:
            joined.append(s)
    return joined


def parse_flags(tail: str) -> tuple[str, list[str]]:
    """Strip trailing L / S / L/S markers; return (name, flags)."""
    flags: list[str] = []
    name = tail.strip()
    # 可能末尾 "xxx L/S" 或 "xxx L" "xxx S"
    while True:
        m = re.search(r"\s+(L/S|L|S)\s*$", name)
        if not m:
            break
        flags.append(m.group(1))
        name = name[: m.start()].strip()
    # 去重保序
    seen: set[str] = set()
    ordered: list[str] = []
    for f in flags:
        if f not in seen:
            seen.add(f)
            ordered.append(f)
    return name, ordered


def clean_name(name: str) -> str:
    name = re.sub(r"\s+", "", name.strip())
    return name


def make_occ_node(
    *,
    code: str,
    name: str,
    major_code: str | None,
    major_name: str | None,
    mid_code: str | None,
    mid_name: str | None,
    minor_code: str | None,
    minor_name: str | None,
    gbm: str | None,
    flags: list[str],
    source_file: str,
    fetched_at: str,
) -> dict:
    name = clean_name(name)
    green = "L" in flags or "L/S" in flags
    digital = "S" in flags or "L/S" in flags
    attrs = {
        "code": code,
        "edition": EDITION,
        "catalog": CATALOG,
        "major_code": major_code,
        "major_name": major_name,
        "mid_code": mid_code,
        "mid_name": mid_name,
        "minor_code": minor_code,
        "minor_name": minor_name,
        "gbm": gbm,
        "flags": flags,
        "is_green": green,
        "is_digital": digital,
        "source_file": source_file,
    }
    desc_parts = [f"大典细类 {code}"]
    if mid_name:
        desc_parts.append(mid_name)
    if minor_name:
        desc_parts.append(minor_name)
    if green:
        desc_parts.append("绿色职业")
    if digital:
        desc_parts.append("数字职业")
    return {
        "id": make_node_id(REGION, "occupation", SOURCE_SYSTEM, code),
        "region": REGION,
        "type": "occupation",
        "name": name,
        "name_en": None,
        "name_zh": name,
        "description": " · ".join(desc_parts),
        "attrs": attrs,
        "source_system": SOURCE_SYSTEM,
        "source_id": code,
        "source_url": SOURCE_URL,
        "license": LICENSE,
        "fetched_at": fetched_at,
        "confidence": "official",
    }


def parse_taxonomy(path: Path, fetched_at: str) -> tuple[list[dict], dict]:
    lines = extract_pdf_lines(path)
    nodes: list[dict] = []
    by_code: dict[str, dict] = {}

    major_code: str | None = None
    major_name: str | None = None
    mid_code: str | None = None
    mid_name: str | None = None
    mid_gbm: str | None = None
    minor_code: str | None = None
    minor_name: str | None = None
    minor_gbm: str | None = None

    skipped: list[str] = []

    for line in lines:
        m_major = MAJOR_RE.match(line)
        if m_major:
            major_code = m_major.group(2)
            major_name = clean_name(m_major.group(4))
            mid_code = mid_name = mid_gbm = None
            minor_code = minor_name = minor_gbm = None
            continue

        # 细类优先（最长匹配）
        m_fine = FINE_RE.match(line)
        if m_fine:
            code = m_fine.group(1)
            name_raw, flags = parse_flags(m_fine.group(2) or "")
            name = clean_name(name_raw)
            if not name:
                skipped.append(line)
                continue
            # 从细类码回填层次
            parts = code.split("-")
            if len(parts) == 4:
                major_code = major_code or parts[0]
                if not mid_code:
                    mid_code = f"{parts[0]}-{parts[1]}"
                if not minor_code:
                    minor_code = f"{parts[0]}-{parts[1]}-{parts[2]}"
            gbm = minor_gbm or mid_gbm
            node = make_occ_node(
                code=code,
                name=name,
                major_code=major_code,
                major_name=major_name,
                mid_code=mid_code,
                mid_name=mid_name,
                minor_code=minor_code,
                minor_name=minor_name,
                gbm=gbm,
                flags=flags,
                source_file=path.name,
                fetched_at=fetched_at,
            )
            by_code[code] = node
            continue

        m_minor = MINOR_RE.match(line)
        if m_minor and not FINE_RE.match(line):
            minor_code = m_minor.group(1)
            minor_gbm = m_minor.group(2)
            name_raw, _ = parse_flags(m_minor.group(3) or "")
            minor_name = clean_name(name_raw) or minor_name
            continue

        m_mid = MID_RE.match(line)
        if m_mid and not MINOR_RE.match(line) and not FINE_RE.match(line):
            mid_code = m_mid.group(1)
            mid_gbm = m_mid.group(2)
            name_raw, _ = parse_flags(m_mid.group(3) or "")
            mid_name = clean_name(name_raw) or mid_name
            minor_code = minor_name = minor_gbm = None
            continue

        skipped.append(line)

    nodes = list(by_code.values())
    green_n = sum(1 for n in nodes if n["attrs"]["is_green"])
    digital_n = sum(1 for n in nodes if n["attrs"]["is_digital"])
    by_major: dict[str, int] = {}
    for n in nodes:
        mc = n["attrs"].get("major_code") or "?"
        by_major[mc] = by_major.get(mc, 0) + 1

    report = {
        "source_file": path.name,
        "source_path": str(path),
        "joined_lines": len(lines),
        "fine_occupations": len(nodes),
        "unique_codes": len(by_code),
        "green_count": green_n,
        "digital_count": digital_n,
        "by_major_code": dict(sorted(by_major.items())),
        "skipped_lines": len(skipped),
        "skipped_sample": skipped[:15],
        "edition": EDITION,
        "catalog": CATALOG,
    }
    return nodes, report


def ingest(dry_run: bool = False, db_path: Path | None = None) -> dict:
    ensure_dirs()
    fetched_at = utc_now_iso()
    pdf = find_taxonomy_pdf()
    result: dict = {
        "source_system": SOURCE_SYSTEM,
        "fetched_at": fetched_at,
        "dry_run": dry_run,
        "source_home": SOURCE_HOME,
    }
    if not pdf:
        result["error"] = "taxonomy PDF not found under data/raw/CN/mohrss/"
        return result

    nodes, parse_report = parse_taxonomy(pdf, fetched_at)
    result["nodes_parsed"] = len(nodes)
    result["parse"] = parse_report
    result["sample"] = [
        {
            "id": n["id"],
            "name": n["name"],
            "source_id": n["source_id"],
            "flags": n["attrs"]["flags"],
            "minor_name": n["attrs"]["minor_name"],
        }
        for n in nodes[:8]
    ]

    if dry_run:
        return result

    conn = connect(db_path)
    try:
        n = upsert_nodes(conn, nodes)
        conn.commit()
        s = stats(conn)
    finally:
        conn.close()

    result["nodes_upserted"] = n
    result["db_stats"] = s
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "ingest_cn_occupations.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest MOHRSS CN occupation taxonomy (大典 2022)"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    out = ingest(dry_run=args.dry_run, db_path=Path(args.db) if args.db else None)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
