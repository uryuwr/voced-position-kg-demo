"""
Ingest 教育部专业目录 → major nodes (CN).

Sources under data/raw/CN/moe/ (actual filenames may vary; resolved by glob):
  - 本科专业目录（2026）.pdf  → 普通本科
  - 职业教育专业目录（2021年）.docx → 中职 / 高职专科 / 职业本科
  - 2025年职业教育专业目录增补清单.doc|.txt → 增补专业

## 重复如何处理（跨文件 / 跨层次）

1. **主键**：`source_id = "{level}:{code}"`  
   - level ∈ ug_bachelor | voc_secondary | voc_associate | voc_bachelor  
   - code = 官方专业代码（含 T/K 后缀，原样保留）  
   - 图节点 id = `CN:major:MOE_CN:{level}:{code}`

2. **不算重复（各建一条）**  
   - 同名不同代码（如本科「计算机科学与技术」vs 高职「计算机应用技术」）  
   - 同名不同层次（极少见但按代码/层次分开）  
   - 本科代码与职教代码体系不同，即使数字巧合也不合并

3. **算同一专业（覆盖更新）**  
   - 同一 level + 同一 code：后写入覆盖名称/描述（增补清单覆盖 2021 底稿同码项）  
   - confidence 均为 official

4. **增补清单**  
   - 按代码推断层次（61=中职, 41/43/44/46/49/50/51=高职专科, 21/22/25/26/29/30=职业本科 等）  
   - 若无法推断层次，用清单中的「层次」字段

Usage:
  python -m crawlers.cn.ingest_majors
  python -m crawlers.cn.ingest_majors --dry-run
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

SOURCE_SYSTEM = "MOE_CN"
REGION = "CN"
LICENSE = "教育部公开发布专业目录；使用时请注明出处（moe.gov.cn）"
SOURCE_HOME = "http://www.moe.gov.cn/"

# 专业代码：6 位数字 + 可选 T/K/TK
CODE_RE = re.compile(r"^(\d{6}(?:TK|T|K)?)$", re.I)
# PDF 行：代码 + 名称
LINE_CODE_NAME = re.compile(
    r"^(\d{6}(?:TK|T|K)?)\s+(.+)$", re.I
)
LINE_CODE_ONLY = re.compile(r"^(\d{6}(?:TK|T|K)?)\s*$", re.I)
# 增补文本中任意位置的代码
ANY_CODE = re.compile(r"(\d{6}(?:TK|T|K)?)", re.I)

LEVEL_LABEL = {
    "ug_bachelor": "普通本科",
    "voc_secondary": "中等职业教育",
    "voc_associate": "高等职业教育专科",
    "voc_bachelor": "高等职业教育本科",
}

# 口径见 docs/职业教育数据范围.md：普本属普通教育类型，职教三层属职业教育类型；图谱均收录
def education_type_for_level(level: str) -> str:
    if level == "ug_bachelor":
        return "general"  # 普通教育
    if level in ("voc_secondary", "voc_associate", "voc_bachelor"):
        return "vocational"  # 职业教育
    return "other"


def moe_dir() -> Path:
    return RAW / "CN" / "moe"


def find_one(patterns: list[str]) -> Path | None:
    d = moe_dir()
    for pat in patterns:
        hits = sorted(d.glob(pat))
        if hits:
            return hits[0]
    return None


def infer_voc_level_from_code(code: str) -> str | None:
    """Infer vocational hierarchy from leading digits of major code."""
    digits = re.match(r"(\d+)", code)
    if not digits:
        return None
    head = digits.group(1)
    # 中职 61xxxx（2021 目录）
    if head.startswith("61"):
        return "voc_secondary"
    # 高职专科 4xxxxx（41–51 等）
    if head.startswith("4"):
        return "voc_associate"
    # 职业本科 2xxxxx（21/22/25/26…）
    if head.startswith("2"):
        return "voc_bachelor"
    return None


def layer_from_zh(text: str) -> str | None:
    t = text.replace(" ", "")
    if "中职" in t:
        return "voc_secondary"
    if "职业本科" in t or "高职本科" in t:
        return "voc_bachelor"
    if "高职专科" in t or "高职" in t:
        return "voc_associate"
    return None


def clean_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"\s+", "", name)
    # 去掉尾注说明，保留主名
    name = re.sub(r"（注：.*?）", "", name)
    name = re.sub(r"\(注：.*?\)", "", name)
    return name.strip()


def make_major_node(
    *,
    code: str,
    name: str,
    level: str,
    category: str | None,
    catalog: str,
    source_url: str,
    fetched_at: str,
    extra: dict | None = None,
) -> dict:
    code = code.strip().upper()
    name = clean_name(name)
    sid = f"{level}:{code}"
    attrs = {
        "code": code,
        "level": level,
        "level_zh": LEVEL_LABEL.get(level, level),
        "education_type": education_type_for_level(level),
        "category": category,
        "catalog": catalog,
    }
    if extra:
        attrs.update(extra)
    return {
        "id": make_node_id(REGION, "major", SOURCE_SYSTEM, sid),
        "region": REGION,
        "type": "major",
        "name": name,
        "name_en": None,
        "name_zh": name,
        "description": f"{LEVEL_LABEL.get(level, level)} · 代码 {code}"
        + (f" · {category}" if category else ""),
        "attrs": attrs,
        "source_system": SOURCE_SYSTEM,
        "source_id": sid,
        "source_url": source_url,
        "license": LICENSE,
        "fetched_at": fetched_at,
        "confidence": "official",
    }


def parse_vocational_docx(path: Path, fetched_at: str) -> list[dict]:
    from docx import Document

    doc = Document(str(path))
    # 表 0 中职(61)、表 1 高职专科(41)、表 2 职业本科(21)
    table_levels = ["voc_secondary", "voc_associate", "voc_bachelor"]
    nodes: list[dict] = []
    source_url = (
        "http://www.moe.gov.cn/srcsite/A07/moe_953/202103/t20210319_521135.html"
    )
    catalog = "职业教育专业目录（2021年）"

    for ti, table in enumerate(doc.tables):
        level = table_levels[ti] if ti < len(table_levels) else None
        category = None
        for row in table.rows:
            cells = [c.text.strip().replace("\n", "") for c in row.cells]
            if not cells or len(cells) < 3:
                continue
            seq, code, name = cells[0], cells[1], cells[2]
            # 大类/类 标题行：三列相同
            if code == name and not CODE_RE.match(code or ""):
                category = code
                continue
            if seq in ("序号",) or code in ("专业代码",):
                continue
            if not CODE_RE.match(code or ""):
                continue
            if not name or name == code:
                continue
            # 用表序号为主，代码再校验
            lvl = level or infer_voc_level_from_code(code) or "voc_associate"
            nodes.append(
                make_major_node(
                    code=code,
                    name=name,
                    level=lvl,
                    category=category,
                    catalog=catalog,
                    source_url=source_url,
                    fetched_at=fetched_at,
                    extra={"source_file": path.name, "table_index": ti},
                )
            )
    return nodes


def parse_undergraduate_pdf(path: Path, fetched_at: str) -> list[dict]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    lines: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            # 页码装饰
            if re.match(r"^[—\-–]\s*\d+\s*[—\-–]$", line):
                continue
            if line in ("附件",) or "普通高等学校本科专业目录" in line:
                continue
            if line.startswith("教") and "部" in line and len(line) < 20:
                continue
            lines.append(line)

    nodes: list[dict] = []
    source_url = (
        "http://www.moe.gov.cn/srcsite/A08/moe_1034/s3882/202604/t20260427_1434931.html"
    )
    catalog = "普通高等学校本科专业目录（2026年）"
    category = None
    discipline = None
    pending_codes: list[str] = []

    def flush_pending(name: str) -> None:
        nonlocal pending_codes
        if not pending_codes:
            return
        # 多代码对应多行名称：通常 1:1 顺序；若名称行数不足则最后一个名字填剩余
        # 这里 pending 在「只有代码」后紧跟名称行；一对一
        # 若一行一个代码后多行名称，只消费一个
        code = pending_codes.pop(0)
        nodes.append(
            make_major_node(
                code=code,
                name=name,
                level="ug_bachelor",
                category=category,
                catalog=catalog,
                source_url=source_url,
                fetched_at=fetched_at,
                extra={
                    "source_file": path.name,
                    "discipline": discipline,
                },
            )
        )

    i = 0
    while i < len(lines):
        line = lines[i]
        # 学科门类
        m_disc = re.match(r"^(\d{2})\s*学科门类[：:]\s*(.+)$", line)
        if m_disc:
            discipline = m_disc.group(2).strip()
            category = None
            i += 1
            continue
        m_disc2 = re.match(r"^学科门类[：:]\s*(.+)$", line)
        if m_disc2:
            discipline = m_disc2.group(1).strip()
            i += 1
            continue
        # 专业类 0101 哲学类
        m_cat = re.match(r"^(\d{4})\s+(.+类)$", line)
        if m_cat and not CODE_RE.match(m_cat.group(1) + "00"):  # 4 digit class
            category = f"{m_cat.group(1)} {m_cat.group(2)}"
            i += 1
            continue
        m_cat2 = re.match(r"^(\d{4})\s+(.+)$", line)
        if m_cat2 and len(m_cat2.group(1)) == 4 and not re.match(
            r"^\d{6}", line
        ):
            # 4 位代码 + 类名
            if "类" in m_cat2.group(2) or len(m_cat2.group(2)) <= 20:
                category = f"{m_cat2.group(1)} {m_cat2.group(2)}"
                i += 1
                continue

        m_cn = LINE_CODE_NAME.match(line)
        if m_cn:
            # 若有堆积的无名称代码，先不混用
            pending_codes.clear()
            nodes.append(
                make_major_node(
                    code=m_cn.group(1),
                    name=m_cn.group(2),
                    level="ug_bachelor",
                    category=category,
                    catalog=catalog,
                    source_url=source_url,
                    fetched_at=fetched_at,
                    extra={"source_file": path.name, "discipline": discipline},
                )
            )
            i += 1
            continue

        m_co = LINE_CODE_ONLY.match(line)
        if m_co:
            pending_codes.append(m_co.group(1))
            i += 1
            # 吸后续纯名称行
            while i < len(lines) and pending_codes:
                nxt = lines[i]
                if LINE_CODE_ONLY.match(nxt) or LINE_CODE_NAME.match(nxt):
                    break
                if re.match(r"^\d{2}\s", nxt) or "学科门类" in nxt:
                    break
                if re.match(r"^\d{4}\s", nxt) and not re.match(r"^\d{6}", nxt):
                    break
                # 名称行
                if re.search(r"[\u4e00-\u9fff]", nxt):
                    flush_pending(nxt)
                    i += 1
                else:
                    break
            continue

        i += 1

    # 剩余 pending 丢弃（无法配对）
    return nodes


def parse_supplement_txt(path: Path, fetched_at: str) -> list[dict]:
    raw = path.read_bytes()
    text = None
    for enc in ("gbk", "utf-8", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        text = raw.decode("gbk", errors="replace")

    source_url = (
        "http://www.moe.gov.cn/srcsite/A07/moe_737/s3876_qt/202601/t20260105_1425685.html"
    )
    catalog = "2025年职业教育专业目录增补清单"
    nodes: list[dict] = []

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("附件") or "增补清单" in line:
            continue
        if "专业代码" in line and "专业名称" in line:
            continue
        m = ANY_CODE.search(line)
        if not m:
            continue
        code = m.group(1).upper()
        # 名称 = 代码之后
        name = line[m.end() :].strip()
        if not name:
            continue
        # 层次
        level = layer_from_zh(line) or infer_voc_level_from_code(code)
        if not level:
            level = "voc_associate"
        # 大类粗提：代码前文本去序号
        head = line[: m.start()]
        head = re.sub(r"^\d+", "", head)
        category = head if len(head) >= 2 else None
        nodes.append(
            make_major_node(
                code=code,
                name=name,
                level=level,
                category=category,
                catalog=catalog,
                source_url=source_url,
                fetched_at=fetched_at,
                extra={"source_file": path.name, "is_supplement": True},
            )
        )
    return nodes


def ensure_supplement_txt() -> Path | None:
    """Prefer pre-converted txt; if only .doc exists, try simple path note."""
    txt = find_one(["*增补*.txt", "*补充*.txt"])
    if txt:
        return txt
    doc = find_one(["*增补*.doc", "*补充*.doc"])
    if doc:
        # optional auto convert via temp already done by user/session
        sibling = doc.with_name("职业教育专业目录增补_2025.txt")
        if sibling.exists():
            return sibling
    return None


def collect_all(fetched_at: str) -> tuple[list[dict], dict]:
    report: dict = {"files": {}, "dedupe": {}}
    by_sid: dict[str, dict] = {}
    order: list[str] = []

    def absorb(nodes: list[dict], label: str) -> None:
        report["files"][label] = {
            "count_raw": len(nodes),
            "sample": [
                {"code": n["attrs"]["code"], "name": n["name"], "level": n["attrs"]["level"]}
                for n in nodes[:3]
            ],
        }
        overwrites = 0
        for n in nodes:
            sid = n["source_id"]
            if sid in by_sid:
                overwrites += 1
            else:
                order.append(sid)
            by_sid[sid] = n  # later wins
        report["files"][label]["overwrites"] = overwrites

    # 1) 职教 2021 底稿
    voc = find_one(["*职业教育专业目录*2021*.docx", "*职教*2021*.docx", "*专业目录（2021*.docx"])
    if not voc:
        # fallback any 2021 docx
        voc = find_one(["*2021*.docx"])
    if voc:
        absorb(parse_vocational_docx(voc, fetched_at), voc.name)
        report["files"][voc.name]["path"] = str(voc)
    else:
        report["files"]["vocational_2021"] = {"error": "file not found"}

    # 2) 增补覆盖同码
    supp = ensure_supplement_txt() or find_one(["*增补*.txt"])
    if supp:
        absorb(parse_supplement_txt(supp, fetched_at), supp.name)
        report["files"][supp.name]["path"] = str(supp)
    else:
        report["files"]["supplement_2025"] = {
            "error": "no .txt (convert .doc to txt first)",
            "hint": "已有会话可生成 职业教育专业目录增补_2025.txt",
        }

    # 3) 本科 2026
    ug = find_one(["*本科*2026*.pdf", "*2026*.pdf", "*本科专业目录*.pdf"])
    if ug:
        absorb(parse_undergraduate_pdf(ug, fetched_at), ug.name)
        report["files"][ug.name]["path"] = str(ug)
    else:
        report["files"]["undergraduate_2026"] = {"error": "file not found"}

    merged = [by_sid[s] for s in order if s in by_sid]
    # also any sid only in by_sid
    for sid, n in by_sid.items():
        if sid not in order:
            merged.append(n)

    report["dedupe"] = {
        "unique_source_ids": len(by_sid),
        "strategy": "level:code primary key; later file overwrites same key",
        "by_level": {},
    }
    for n in by_sid.values():
        lv = n["attrs"]["level"]
        report["dedupe"]["by_level"][lv] = report["dedupe"]["by_level"].get(lv, 0) + 1

    return merged, report


def ingest(dry_run: bool = False, db_path: Path | None = None) -> dict:
    ensure_dirs()
    fetched_at = utc_now_iso()
    nodes, parse_report = collect_all(fetched_at)

    result = {
        "source_system": SOURCE_SYSTEM,
        "fetched_at": fetched_at,
        "nodes_parsed": len(nodes),
        "parse": parse_report,
        "dry_run": dry_run,
    }

    if dry_run:
        result["sample"] = [
            {"id": n["id"], "name": n["name"], "source_id": n["source_id"]}
            for n in nodes[:8]
        ]
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
    (REPORTS / "ingest_cn_majors.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest MOE CN major catalogs")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    out = ingest(dry_run=args.dry_run, db_path=Path(args.db) if args.db else None)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
