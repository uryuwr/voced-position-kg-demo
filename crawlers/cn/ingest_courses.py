"""
Ingest 职业教育专业教学标准（试点）→ course 节点 + major-related_to-course 边。

读取 data/raw/CN/course/*专业教学标准*.pdf：
  - 专业代码/名称/层次
  - 专业基础/核心/拓展课程（领域名作为课名，课标未给独立课号时用领域名）
  - 可选：从「职业面向」解析大典代码 → 补 prepares_for（official）

Usage:
  python -m crawlers.cn.ingest_courses --dry-run
  python -m crawlers.cn.ingest_courses
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

from backend.kg.graph_store import connect, stats, upsert_edges, upsert_nodes
from backend.kg.paths import RAW, REPORTS, ensure_dirs
from backend.kg.provenance import make_edge_id, make_node_id, utc_now_iso

SOURCE_SYSTEM = "MOE_CN"
REGION = "CN"
LICENSE = "教育部职业教育专业教学标准（公开）；使用时请注明出处（moe.gov.cn）"
CATALOG = "职业教育专业教学标准（2025年修（制）订）"
HOME = (
    "http://www.moe.gov.cn/s78/A07/zcs_ztzl/2017_zt06/17zt06_bznr/bznr_zyjyzyjxbz/"
)

# 文件名：高职专科_510203_软件技术_专业教学标准_2025.pdf
FNAME_RE = re.compile(
    r"(中职|高职专科|职业本科|高职本科)?_?(\d{6})[_＿](.+?)_专业教学标准",
    re.I,
)
CODE_IN_TEXT = re.compile(
    r"专业名称[（(]?专业代码[）)]?\s*[\n\s]*([^\n（(]{2,40})[（(]\s*(\d{6})\s*[）)]"
)
CODE_IN_TEXT2 = re.compile(r"([^\n]{2,40})[（(]\s*(\d{6})\s*[）)]\s*")
OCC_CODE_RE = re.compile(r"(\d-\d{2}(?:-\d{2}){1,2})")

LEVEL_MAP = {
    "中职": "voc_secondary",
    "高职专科": "voc_associate",
    "高职本科": "voc_bachelor",
    "职业本科": "voc_bachelor",
}


def course_dir() -> Path:
    return RAW / "CN" / "course"


def pdf_text(path: Path) -> str:
    import fitz

    doc = fitz.open(path)
    try:
        text = "\n".join(doc[i].get_text("text") for i in range(doc.page_count))
    finally:
        doc.close()
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return text


def parse_meta(path: Path, text: str) -> dict:
    level_zh, code, name = None, None, None
    m = FNAME_RE.search(path.name)
    if m:
        level_zh, code, name = m.group(1), m.group(2), m.group(3)
        name = name.replace("_", "").strip()
    m2 = CODE_IN_TEXT.search(text[:2500])
    if m2:
        name = re.sub(r"\s+", "", m2.group(1))
        code = m2.group(2)
    if not code:
        # 专业名称（专业代码）\nxxx（510203）
        m3 = re.search(
            r"专业名称[^\n]{0,20}\n\s*([^\n]{2,40})[（(](\d{6})[）)]",
            text[:3000],
        )
        if m3:
            name = re.sub(r"\s+", "", m3.group(1))
            code = m3.group(2)
    if not level_zh:
        if "高等职业教育专科" in text[:800] or "高职专科" in path.name:
            level_zh = "高职专科"
        elif "中等职业教育" in text[:800]:
            level_zh = "中职"
        elif "本科" in text[:800]:
            level_zh = "职业本科"
        else:
            level_zh = "高职专科"
    level = LEVEL_MAP.get(level_zh, "voc_associate")
    return {
        "major_code": code,
        "major_name": name,
        "level": level,
        "level_zh": level_zh,
    }


def split_course_list(blob: str) -> list[str]:
    """「A、B、C等领域」→ 课名列表。"""
    blob = blob.replace("\n", "")
    blob = re.sub(r"等领域的内容.*$", "", blob)
    blob = re.sub(r"等领域.*$", "", blob)
    blob = re.sub(r"^主要包括[：:]", "", blob)
    parts = re.split(r"[、，,;；]", blob)
    out = []
    for p in parts:
        p = re.sub(r"\s+", "", p)
        p = re.sub(r"^[①②③④⑤⑥⑦⑧⑨⑩\d\.．、]+", "", p)
        if 2 <= len(p) <= 40 and re.search(r"[\u4e00-\u9fffA-Za-z]", p):
            if p in ("等内容", "具体课程", "学校根据"):
                continue
            out.append(p)
    return out


def parse_courses(text: str) -> list[dict]:
    """返回 {name, course_kind}。"""
    courses: list[dict] = []
    seen: set[str] = set()

    def add(name: str, kind: str):
        name = re.sub(r"\s+", "", name)
        # 去掉序号/续表噪声
        name = re.sub(r"^续表序号", "", name)
        name = re.sub(r"续表$", "", name)
        if not name or name in seen:
            return
        if name in ("续表序号", "序号", "续表"):
            return
        # 表解析误吸入「领域①任务…」
        if "①" in name or "②" in name or "③" in name:
            name = re.split(r"[①②③④⑤]", name)[0]
        if re.search(r"根据|进行|完成|使用工具|业务", name) and len(name) > 12:
            return
        if len(name) < 2 or len(name) > 24:
            return
        if not re.search(r"[\u4e00-\u9fff]", name):
            return
        seen.add(name)
        courses.append({"name": name, "course_kind": kind})

    # 基础/拓展：主要包括：…
    for kind, label in (
        ("foundation", r"专业基础课程"),
        ("elective", r"专业拓展课程"),
    ):
        m = re.search(
            label + r"[^\n]{0,80}\n\s*主要包括[：:](.+?)(?=\n\s*[（(]\d|\n\s*8\.|\n\s*专业核心|\n\s*实践性|$)",
            text,
            re.S,
        )
        if m:
            for c in split_course_list(m.group(1)):
                add(c, kind)

    # 核心：表中「课程涉及的主要领域」或 主要包括
    m_core = re.search(
        r"专业核心课程\s*\n\s*主要包括[：:](.+?)(?=专业核心课程主要|具体课程|（3）|8\.1\.3)",
        text,
        re.S,
    )
    if m_core:
        for c in split_course_list(m_core.group(1)):
            add(c, "core")

    # 表序号 + 领域名（跨行）
    # 1 \n 面向对象程序\n设计
    block = text
    i0 = text.find("专业核心课程主要教学内容")
    if i0 < 0:
        i0 = text.find("专业核心课程")
    if i0 >= 0:
        chunk = text[i0 : i0 + 4500]
        # 去掉表头后找：数字 + 中文领域
        for m in re.finditer(
            r"(?:^|\n)\s*(\d{1,2})\s*\n\s*([^\n0-9]{2,20}(?:\n[^\n0-9]{1,20})?)\s*\n",
            chunk,
        ):
            domain = re.sub(r"\s+", "", m.group(2))
            if domain in ("课程涉及的主要领域", "典型工作任务描述", "主要教学内容与要求", "序号", "续表序号"):
                continue
            if "工作任务" in domain or "教学内容" in domain or "经验" in domain:
                continue
            if domain.startswith("续表") or "①" in domain:
                continue
            add(domain, "core")

    return courses


def parse_occupation_codes(text: str) -> list[str]:
    m = re.search(r"主要职业类别[（(]代码[）)]?\s*(.+?)(?=主要岗位|职业类证书|6\s*培养目标)", text, re.S)
    if not m:
        return []
    blob = m.group(1)
    codes = []
    for c in OCC_CODE_RE.findall(blob):
        # normalize to fine class if 2-02-10 only → skip short mid
        parts = c.split("-")
        if len(parts) >= 3:
            codes.append(c)
    # unique keep order
    seen = set()
    out = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def make_course_node(
    *,
    major_code: str,
    major_name: str,
    level: str,
    course_name: str,
    course_kind: str,
    source_url: str,
    source_file: str,
    fetched_at: str,
) -> dict:
    sid = f"{level}:{major_code}:{course_name}"
    if len(sid) > 160:
        sid = f"{level}:{major_code}:{course_name[:60]}"
    kind_zh = {
        "foundation": "专业基础课程",
        "core": "专业核心课程",
        "elective": "专业拓展课程",
    }.get(course_kind, course_kind)
    return {
        "id": make_node_id(REGION, "course", SOURCE_SYSTEM, sid),
        "region": REGION,
        "type": "course",
        "name": course_name,
        "name_en": None,
        "name_zh": course_name,
        "description": f"{major_name}（{major_code}）· {kind_zh}",
        "attrs": {
            "role": "curriculum_catalog",
            "playable": False,
            "major_code": major_code,
            "major_name": major_name,
            "major_level": level,
            "course_kind": course_kind,
            "course_kind_zh": kind_zh,
            "catalog": CATALOG,
            "source_file": source_file,
            "standard_year": "2025",
        },
        "source_system": SOURCE_SYSTEM,
        "source_id": sid,
        "source_url": source_url,
        "license": LICENSE,
        "fetched_at": fetched_at,
        "confidence": "official",
    }


def resolve_major_id(conn, level: str, code: str) -> str | None:
    sid = f"{level}:{code}"
    row = conn.execute(
        "SELECT id FROM nodes WHERE region=? AND type='major' AND source_id=?",
        (REGION, sid),
    ).fetchone()
    return row["id"] if row else None


def resolve_occ_id(conn, code: str) -> str | None:
    # exact fine code
    row = conn.execute(
        """
        SELECT id FROM nodes WHERE region=? AND type='occupation'
          AND source_system='MOHRSS_CN' AND source_id=?
        """,
        (REGION, code),
    ).fetchone()
    if row:
        return row["id"]
    return None


def resolve_occ_ids(conn, code: str, limit: int = 8) -> list[str]:
    """
    细类码精确匹配；若课标只给到小类/中类（3 段或前缀），展开为下属细类（最多 limit）。
    """
    exact = resolve_occ_id(conn, code)
    if exact:
        return [exact]
    parts = code.split("-")
    if len(parts) < 3:
        return []
    prefix = code if code.endswith("-") else code
    # 小类 2-02-10 → 匹配 2-02-10-%
    if len(parts) == 3:
        like = f"{code}-%"
    elif len(parts) == 4:
        # 精确已失败，不再模糊
        return []
    else:
        like = f"{code}%"
    rows = conn.execute(
        """
        SELECT id FROM nodes WHERE region=? AND type='occupation'
          AND source_system='MOHRSS_CN' AND source_id LIKE ?
        ORDER BY source_id LIMIT ?
        """,
        (REGION, like, limit),
    ).fetchall()
    return [r["id"] for r in rows]


def parse_one(path: Path, fetched_at: str) -> tuple[list[dict], list[dict], dict]:
    text = pdf_text(path)
    meta = parse_meta(path, text)
    courses = parse_courses(text)
    occ_codes = parse_occupation_codes(text)
    # source url: reconstruct from known pattern when possible
    source_url = HOME
    murl = re.search(r"(https?://[^\s]+\.pdf)", path.read_text(encoding="utf-8", errors="ignore") if False else "")
    # use directory README later; put official portal
    source_url = (
        "http://www.moe.gov.cn/s78/A07/zcs_ztzl/2017_zt06/17zt06_bznr/bznr_zyjyzyjxbz/"
    )

    nodes = []
    if meta["major_code"] and meta["major_name"]:
        for c in courses:
            nodes.append(
                make_course_node(
                    major_code=meta["major_code"],
                    major_name=meta["major_name"],
                    level=meta["level"],
                    course_name=c["name"],
                    course_kind=c["course_kind"],
                    source_url=source_url,
                    source_file=path.name,
                    fetched_at=fetched_at,
                )
            )
    report = {
        "file": path.name,
        "meta": meta,
        "courses": len(nodes),
        "course_names": [n["name"] for n in nodes],
        "occupation_codes_in_standard": occ_codes,
    }
    return nodes, occ_codes, report


def ingest(dry_run: bool = False, db_path: Path | None = None) -> dict:
    ensure_dirs()
    fetched_at = utc_now_iso()
    d = course_dir()
    # 根目录 + moe_2025/pdfs 批量下载目录
    pdfs = list(d.rglob("*.pdf"))
    # unique by content-ish name
    seen_p = set()
    files = []
    for p in sorted(pdfs, key=lambda x: str(x)):
        if p.name in seen_p:
            continue
        seen_p.add(p.name)
        files.append(p)

    result: dict = {
        "source_system": SOURCE_SYSTEM,
        "fetched_at": fetched_at,
        "dry_run": dry_run,
        "files": [],
    }
    if not files:
        result["error"] = "no teaching standard PDFs in data/raw/CN/course/"
        return result

    all_nodes: list[dict] = []
    # (meta-level, major_code, course_node) for edges
    course_pairs: list[tuple[str, str, dict]] = []
    occ_pairs: list[tuple[str, str, list[str], str]] = []  # level, code, occ_codes, evidence

    for pdf in files:
        nodes, occ_codes, rep = parse_one(pdf, fetched_at)
        result["files"].append(rep)
        meta = rep["meta"]
        all_nodes.extend(nodes)
        for n in nodes:
            course_pairs.append((meta["level"], meta["major_code"], n))
        if meta["major_code"] and occ_codes:
            occ_pairs.append(
                (
                    meta["level"],
                    meta["major_code"],
                    occ_codes,
                    f"课标职业面向：{meta['major_name']}（{meta['major_code']}）",
                )
            )

    by_id = {n["id"]: n for n in all_nodes}
    all_nodes = list(by_id.values())
    result["nodes_parsed"] = len(all_nodes)
    result["sample"] = [
        {"id": n["id"], "name": n["name"], "kind": n["attrs"]["course_kind"]}
        for n in all_nodes[:12]
    ]

    if dry_run:
        return result

    conn = connect(db_path)
    edges: list[dict] = []
    try:
        for level, mcode, n in course_pairs:
            maj_id = resolve_major_id(conn, level, mcode)
            if not maj_id:
                continue
            # related_to: major —课程（课标）— course
            eid = make_edge_id(maj_id, "related_to", n["id"])
            edges.append(
                {
                    "id": eid,
                    "src_id": maj_id,
                    "dst_id": n["id"],
                    "rel_type": "related_to",
                    "region": REGION,
                    "weight": 1.0 if n["attrs"]["course_kind"] == "core" else 0.8,
                    "evidence": f"{CATALOG} · {n['attrs']['course_kind_zh']} · {n['name']}",
                    "attrs": {
                        "link_basis": "teaching_standard_curriculum",
                        "course_kind": n["attrs"]["course_kind"],
                    },
                    "source_system": SOURCE_SYSTEM,
                    "source_id": f"{level}:{mcode}->{n['name']}",
                    "source_url": n["source_url"],
                    "license": LICENSE,
                    "fetched_at": fetched_at,
                    "confidence": "official",
                }
            )

        # official prepares_for from 职业面向 codes（细类精确；小类前缀展开）
        for level, mcode, codes, evidence in occ_pairs:
            maj_id = resolve_major_id(conn, level, mcode)
            if not maj_id:
                continue
            for code in codes:
                if not re.match(r"^\d-\d{2}(?:-\d{2}){1,2}$", code):
                    continue
                occ_ids = resolve_occ_ids(conn, code, limit=6)
                for occ_id in occ_ids:
                    eid = make_edge_id(maj_id, "prepares_for", occ_id)
                    edges.append(
                        {
                            "id": eid,
                            "src_id": maj_id,
                            "dst_id": occ_id,
                            "rel_type": "prepares_for",
                            "region": REGION,
                            "weight": 1.0 if len(code.split("-")) == 4 else 0.85,
                            "evidence": evidence + f" · 职业类别代码 {code}",
                            "attrs": {
                                "match_method": "teaching_standard_occupation_codes",
                                "occupation_code": code,
                                "review_status": "official_text",
                            },
                            "source_system": SOURCE_SYSTEM,
                            "source_id": f"{level}:{mcode}->{code}->{occ_id[-12:]}",
                            "source_url": HOME,
                            "license": LICENSE,
                            "fetched_at": fetched_at,
                            "confidence": "official",
                        }
                    )

        e_by = {e["id"]: e for e in edges}
        edges = list(e_by.values())

        n_up = upsert_nodes(conn, all_nodes)
        e_up = upsert_edges(conn, edges)
        conn.commit()
        s = stats(conn)
    finally:
        conn.close()

    result["nodes_upserted"] = n_up
    result["edges_upserted"] = e_up
    result["edges_by_rel"] = {}
    for e in edges:
        result["edges_by_rel"][e["rel_type"]] = result["edges_by_rel"].get(e["rel_type"], 0) + 1
    result["db_stats"] = s
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "ingest_cn_courses.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest CN vocational teaching standards → courses")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    out = ingest(dry_run=args.dry_run, db_path=Path(args.db) if args.db else None)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
