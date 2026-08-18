"""
Ingest 国家职业技能标准（试点）→ skill_level + occupation-requires 边 (CN).

读取 data/raw/CN/skill_standards/*.pdf：
  - 职业名称 / 编码 / 技能等级
  - 技能要求权重表中的「职业功能」× 等级（有权重则建 skill_level）
  - 大典细类 source_id = 职业编码 → requires 边

Usage:
  python -m crawlers.cn.ingest_skill_standards --dry-run
  python -m crawlers.cn.ingest_skill_standards
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
from backend.kg.level_scale import normalize_skill_level_node
from backend.kg.paths import RAW, REPORTS, ensure_dirs
from backend.kg.provenance import make_edge_id, make_node_id, utc_now_iso

SOURCE_SYSTEM = "MOHRSS_CN"
REGION = "CN"
LICENSE = "人社部/工信部颁布国家职业技能标准；使用时请注明出处"
SCALE = "cn_skill_grade"  # 五级…一级

# 等级规范名 → 代码（含技能等级五级制 + 专业技术初级/中级/高级）
LEVEL_CANON = [
    ("五级/初级工", "L5"),
    ("四级/中级工", "L4"),
    ("三级/高级工", "L3"),
    ("二级/技师", "L2"),
    ("一级/高级技师", "L1"),
    ("初级", "T1"),
    ("中级", "T2"),
    ("高级", "T3"),
]
LEVEL_PAT = re.compile(
    r"(五级\s*/\s*初级工|四级\s*/\s*中级工|三级\s*/\s*高级工|"
    r"二级\s*/\s*技师|一级\s*/\s*高级技师|"
    r"初级|中级|高级)"
)

# 刻度归一由 backend/kg/level_scale.py 统一负责（映射表曾在三处各存一份并漂移）。
# 这里只管把国标原样采下来，`normalize_skill_level_node` 负责转成产品档并剥掉
# 国标文案，与 pg_store/migrate.py 灌库时走的是同一份逻辑。

# 2022 大典与部分 2021 标准旧码对照（名称对齐优先，此为辅助）
CODE_ALIASES = {
    "2-02-10-12": "2-02-38-04",  # 云计算工程技术人员
    "2-02-10-11": "2-02-38-03",  # 大数据
    "2-02-10-15": "2-02-38-08",  # 区块链
    "2-02-10-13": "2-02-38-06",  # 工业互联网
    "2-02-10-14": "2-02-38-07",  # 虚拟现实
    "2-02-10-10": "2-02-38-02",  # 物联网
    "2-02-10-09": "2-02-38-01",  # 人工智能工程技术人员
    "2-02-07-13": "2-02-38-05",  # 智能制造
}

OCC_CODE_RE = re.compile(
    r"职业编码\s*[:：]?\s*(\d-\d{2}-\d{2}-\d{2})", re.I
)
OCC_NAME_RE = re.compile(
    r"职业名称\s*[:：]?\s*\n?\s*([^\n]{2,40})", re.M
)
# 标题式：单独一行的职业名 + 下一处「国家职业技能标准」
TITLE_NAME_RE = re.compile(
    r"(?:^|\n)\s*([^\n]{2,30})\s*\n\s*国家职业技能标准", re.M
)

# 优先入库的完整版文件名关键字
PREFERRED = [
    "人工智能训练师_国家职业技能标准_2021_mirror.pdf",
    "计算机程序设计员_国家职业技能标准_2022.pdf",
]


def standards_dir() -> Path:
    return RAW / "CN" / "skill_standards"


def pdf_text(path: Path) -> str:
    import fitz

    doc = fitz.open(path)
    try:
        parts = [doc[i].get_text("text") for i in range(doc.page_count)]
    finally:
        doc.close()
    text = "\n".join(parts)
    # 全角空格、多余空白
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return text


def norm_level(raw: str) -> tuple[str, str] | None:
    s = re.sub(r"\s+", "", raw)
    # 先匹配「初级工」等长词，避免「初级」误伤
    if "初级工" in s or "五级" in s:
        return "五级/初级工", "L5"
    if "中级工" in s or "四级" in s:
        return "四级/中级工", "L4"
    if "高级工" in s or "三级" in s:
        return "三级/高级工", "L3"
    if "高级技师" in s or ("一级" in s and "技师" in s):
        return "一级/高级技师", "L1"
    if "技师" in s and "高级技师" not in s:
        return "二级/技师", "L2"
    if s in ("初级",) or s.endswith("初级"):
        return "初级", "T1"
    if s in ("中级",) or s.endswith("中级"):
        return "中级", "T2"
    if s in ("高级",) or (s.endswith("高级") and "高级工" not in s and "高级技师" not in s):
        return "高级", "T3"
    for zh, code in LEVEL_CANON:
        if re.sub(r"\s+", "", zh) == s:
            return zh, code
    return None


def parse_levels_declared(text: str) -> list[tuple[str, str]]:
    """从 1.4 职业技能等级 / 专业技术等级段解析。"""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    m = re.search(
        r"1\s*[\.．]?\s*4\s*(?:职业技能等级|专业技术等级)\s*(.{0,400})",
        text,
        re.S,
    )
    if not m:
        m = re.search(
            r"(?:职业技能等级|专业技术等级)\s*(本职业.{0,280})",
            text,
            re.S,
        )
    chunk = m.group(1) if m else ""
    chunk = re.split(r"1\s*[\.．]?\s*5", chunk)[0]
    for raw in LEVEL_PAT.findall(chunk):
        n = norm_level(raw)
        if n and n[1] not in seen:
            # 专业技术三段：初级/中级/高级 优先于误匹配
            seen.add(n[1])
            found.append(n)
    # 若声明里是「初级、中级、高级三个等级」
    if not found and re.search(r"初级[、,]\s*中级[、,]\s*高级", text[:4000]):
        found = [("初级", "T1"), ("中级", "T2"), ("高级", "T3")]
    return found

def parse_meta(text: str, filename: str) -> dict:
    code = None
    m = OCC_CODE_RE.search(text)
    if m:
        code = m.group(1)
    # 页眉也可能写 职业编码: x
    if not code:
        m2 = re.search(r"(\d-\d{2}-\d{2}-\d{2})", text[:2500])
        if m2:
            code = m2.group(1)

    name = None
    m = OCC_NAME_RE.search(text)
    if m:
        name = re.sub(r"\s+", "", m.group(1))
    if not name:
        m = TITLE_NAME_RE.search(text)
        if m:
            cand = re.sub(r"\s+", "", m.group(1))
            if "国家" not in cand and "说明" not in cand:
                name = cand
    if not name:
        # 文件名：xxx_国家职业...
        mfn = re.match(r"(.+?)_国家", filename)
        if mfn:
            name = mfn.group(1).strip()

    edition = None
    me = re.search(r"（\s*(20\d{2})\s*年版\s*）|\(\s*(20\d{2})\s*年版\s*\)", text[:2000])
    if me:
        edition = me.group(1) or me.group(2)

    levels = parse_levels_declared(text)
    return {
        "occupation_code": code,
        "occupation_name": name,
        "edition": edition,
        "levels": levels,
    }


def parse_skill_weight_functions(
    text: str, levels: list[tuple[str, str]]
) -> list[dict]:
    """
    从「技能要求权重表」按行流解析（PDF 常把功能名拆行）。
    返回 {function, level_zh, level_code, weight_pct}
    """
    m = re.search(r"技能要求权重表", text)
    if not m:
        return []

    chunk = text[m.start() : m.start() + 2000]
    lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]

    # 表头等级列顺序（按出现）
    header_order: list[tuple[str, str]] = []
    buf = ""
    for ln in lines[:40]:
        buf += re.sub(r"\s+", "", ln)
        for zh, code in LEVEL_CANON:
            key = re.sub(r"\s+", "", zh)
            if key in buf and (zh, code) not in header_order:
                # 仅保留本标准声明过的等级；若声明为空则全收
                if not levels or (zh, code) in levels:
                    header_order.append((zh, code))
    if not header_order:
        header_order = list(levels) if levels else list(LEVEL_CANON)
    n_cols = len(header_order)
    if n_cols == 0:
        return []

    # 定位表体：跳过表头，从「技能/要求」之后开始
    start = 0
    for i, ln in enumerate(lines):
        if ln in ("要求", "技能要求") or (
            i > 0 and lines[i - 1] == "技能" and ln == "要求"
        ):
            start = i + 1
            break
    body = lines[start:]

    val_re = re.compile(r"^(\d{1,3}|[—–\-－])$")
    skip_name = {
        "技能等级",
        "项目",
        "技能",
        "要求",
        "技能要求",
        "合计",
        "基本要求",
        "相关知识要求",
        "职业编码",
    }

    results: list[dict] = []
    name_parts: list[str] = []
    vals: list[str] = []

    def flush() -> None:
        nonlocal name_parts, vals
        if not name_parts or len(vals) != n_cols:
            name_parts, vals = [], []
            return
        name = re.sub(r"\s+", "", "".join(name_parts))
        name = re.sub(r"[（(].*?[）)]", "", name)
        name = re.sub(r"[%％:：\d]+$", "", name)
        if (
            len(name) >= 2
            and len(name) <= 24
            and re.search(r"[\u4e00-\u9fff]", name)
            and name not in skip_name
            and not name.startswith("职业编码")
        ):
            for (zh, code), tok in zip(header_order, vals):
                if tok in ("—", "–", "-", "－"):
                    continue
                try:
                    w = int(tok)
                except ValueError:
                    continue
                if 0 < w <= 100:
                    results.append(
                        {
                            "function": name,
                            "level_zh": zh,
                            "level_code": code,
                            "weight_pct": w,
                        }
                    )
        name_parts, vals = [], []

    for ln in body:
        compact = re.sub(r"\s+", "", ln)
        if compact.startswith("合计"):
            flush()
            break
        if compact in skip_name or compact in ("（%）", "(%)", "%"):
            continue
        if val_re.match(compact):
            # 值开始前若无功能名，丢弃孤儿值
            if not name_parts and not vals:
                continue
            vals.append(compact)
            if len(vals) == n_cols:
                flush()
            continue
        # 新功能名（若上一段值未满则丢弃残缺行）
        if vals and len(vals) != n_cols:
            name_parts, vals = [], []
        if vals and len(vals) == n_cols:
            flush()
        # 纯中文/标点功能名碎片
        if re.fullmatch(r"[\u4e00-\u9fffA-Za-z与和及/·\-—]+", compact):
            name_parts.append(compact)
        elif re.search(r"[\u4e00-\u9fff]", compact) and len(compact) <= 24:
            name_parts.append(re.sub(r"[\d%％]+", "", compact))

    flush()
    return results

def _split_func_list(blob: str) -> list[str]:
    """「A、B、C」或「A，B和C」→ 功能名列表。"""
    blob = re.sub(r"\s+", "", blob)
    blob = re.split(r"[；;。]", blob)[0]
    parts = re.split(r"[、，,及和]", blob)
    out = []
    for p in parts:
        p = re.sub(r"^[的与]", "", p)
        p = re.sub(r"(方向的职业功能包括|职业功能包括|包括)$", "", p)
        if 2 <= len(p) <= 20 and re.search(r"[\u4e00-\u9fff]", p):
            if p not in ("工作内容", "专业能力要求", "相关知识要求", "职业功能"):
                out.append(p)
    return out


def fallback_function_levels(
    text: str, levels: list[tuple[str, str]]
) -> list[dict]:
    """
    权重表失败时：
    1) 技能五级制：3.x + 五级/初级工… 节
    2) 专业技术三级：3.1 初级 / 3.2 中级 / 3.3 高级 +「职业功能包括…」
    3) 工作内容小标题 1.1 功能名
    """
    results: list[dict] = []

    # —— 五级制 ——
    parts = re.split(
        r"(?:^|\n)\s*3\.\d+\s+(五级\s*/\s*初级工|四级\s*/\s*中级工|"
        r"三级\s*/\s*高级工|二级\s*/\s*技师|一级\s*/\s*高级技师)",
        text,
    )
    i = 1
    while i + 1 < len(parts):
        lv_raw, body = parts[i], parts[i + 1]
        n = norm_level(lv_raw)
        i += 2
        if not n:
            continue
        zh, code = n
        for fm in re.finditer(
            r"(?:^|\n)\s*(\d)\s*[\.、．]\s*([^\n\d]{2,20})", body[:2000]
        ):
            func = re.sub(r"\s+", "", fm.group(2))
            func = re.sub(r"[（(].*", "", func)
            if len(func) < 2:
                continue
            if func in ("职业功能", "工作内容", "技能要求", "相关知识要求"):
                continue
            results.append(
                {
                    "function": func,
                    "level_zh": zh,
                    "level_code": code,
                    "weight_pct": None,
                }
            )

    if results:
        return results

    # —— 专业技术三级（工程技术人员标准）——
    eng_parts = re.split(
        r"(?:^|\n)\s*3\s*[\.．]?\s*[123]\s*\n?\s*(初级|中级|高级)\b",
        text,
    )
    # 也兼容同行：3.1 初级
    if len(eng_parts) < 3:
        eng_parts = re.split(
            r"(?:^|\n)\s*3\s*[\.．]\s*[123]\s*(初级|中级|高级)\b",
            text,
        )
    i = 1
    while i + 1 < len(eng_parts):
        lv_raw, body = eng_parts[i], eng_parts[i + 1]
        n = norm_level(lv_raw)
        i += 2
        if not n:
            continue
        zh, code = n
        body = body[:4000]
        funcs: list[str] = []
        # 优先：职业功能包括 A、B、C（跨行）
        for m in re.finditer(
            r"职业功能包括(.{4,220}?)(?:；|。|职业\s*\n\s*功能|工作内容)",
            body,
            re.S,
        ):
            funcs.extend(_split_func_list(m.group(1)))
        # 补充：仅一级功能标题「1.xxx / 2.xxx」（不要 1.1.1 细项）
        body_flat = re.sub(r"\s+", "", body)
        if len(funcs) < 3:
            for m in re.finditer(
                r"(?<![\d.])([1-9])[\.、．]([\u4e00-\u9fff]{2,14})(?=[\d1-9][\.、．]|$|能|应|相关)",
                body_flat,
            ):
                cand = m.group(2)
                if cand not in ("工作内容", "专业能力要求", "相关知识要求", "职业功能"):
                    funcs.append(cand)
        # 去重 + 每级最多 12 个功能（防噪声爆炸）
        seen: set[str] = set()
        level_funcs: list[str] = []
        for f in funcs:
            f = re.sub(r"\s+", "", f)
            f = re.sub(r"[0-9.．]+$", "", f)
            if f in seen or not (2 <= len(f) <= 16):
                continue
            if re.fullmatch(r"[\u4e00-\u9fff]+", f) is None and not re.search(
                r"[\u4e00-\u9fff]{2,}", f
            ):
                continue
            seen.add(f)
            level_funcs.append(f)
            if len(level_funcs) >= 12:
                break
        for f in level_funcs:
            results.append(
                {
                    "function": f,
                    "level_zh": zh,
                    "level_code": code,
                    "weight_pct": None,
                }
            )

    if results:
        return results

    # —— 嵌套：3.1.1 初级 / 3.1.2 中级（如人工智能工程技术人员）——
    nest_parts = re.split(
        r"(?:^|\n)\s*\d+\s*[\.．]\s*\d+\s*[\.．]\s*\d+\s*\n?\s*(初级|中级|高级)\b",
        text,
    )
    i = 1
    while i + 1 < len(nest_parts):
        lv_raw, body = nest_parts[i], nest_parts[i + 1]
        n = norm_level(lv_raw)
        i += 2
        if not n:
            continue
        zh, code = n
        body_flat = re.sub(r"\s+", "", body[:2500])
        funcs: list[str] = []
        for m in re.finditer(
            r"(?<![\d.])([1-9])[\.、．]([\u4e00-\u9fff]{2,14})",
            body_flat,
        ):
            cand = m.group(2)
            if cand not in ("工作内容", "专业能力要求", "相关知识要求", "职业功能"):
                funcs.append(cand)
        seen: set[str] = set()
        for f in funcs:
            if f in seen or not (2 <= len(f) <= 16):
                continue
            seen.add(f)
            results.append(
                {
                    "function": f,
                    "level_zh": zh,
                    "level_code": code,
                    "weight_pct": None,
                }
            )
            if len(seen) >= 12:
                break

    if results:
        return results

    # —— 方向标题 3.1 某某实现 作为功能 × 声明等级 ——
    if levels:
        domains = re.findall(
            r"(?:^|\n)\s*3\s*[\.．]\s*\d+\s*\n?\s*([\u4e00-\u9fff]{4,24})\s*(?:\n|$)",
            text,
        )
        domains = [
            d
            for d in domains
            if d not in ("工作要求", "初级", "中级", "高级", "基本要求")
            and "职业" not in d[:2]
        ]
        for d in domains[:10]:
            for zh, code in levels:
                results.append(
                    {
                        "function": d.strip(),
                        "level_zh": zh,
                        "level_code": code,
                        "weight_pct": None,
                    }
                )

    if results:
        return results

    # —— 全局「职业功能包括」+ 声明等级笛卡尔积 ——
    if levels:
        gfuncs: list[str] = []
        for m in re.finditer(r"职业功能包括(.{4,200}?)(?:；|。)", text, re.S):
            gfuncs.extend(_split_func_list(m.group(1)))
        seen2: set[str] = set()
        for f in gfuncs:
            if f in seen2:
                continue
            seen2.add(f)
            for zh, code in levels:
                results.append(
                    {
                        "function": f,
                        "level_zh": zh,
                        "level_code": code,
                        "weight_pct": None,
                    }
                )
    return results


def make_skill_node(
    *,
    occ_code: str,
    occ_name: str,
    function: str,
    level_zh: str,
    level_code: str,
    weight_pct: int | None,
    edition: str | None,
    source_url: str,
    source_file: str,
    fetched_at: str,
) -> dict:
    # source_id 沿用国标原码：它是源系统标识，须稳定，否则重跑会产生重复节点
    sid = f"{occ_code}|{function}|{level_code}"
    name = f"{function} · {level_zh}"
    attrs = {
        "skill_name": function,
        # 国标原样，下面交给 normalize_skill_level_node 转成 attrs.level 并剥掉
        "level_zh": level_zh,
        "level_code": level_code,
        "scale": SCALE,
        "occupation_code": occ_code,
        "occupation_name": occ_name,
        "weight_pct": weight_pct,
        "edition": edition,
        "source_file": source_file,
        "standard_type": "national_skill_standard",
    }
    node = {
        "id": make_node_id(REGION, "skill_level", SOURCE_SYSTEM, sid),
        "region": REGION,
        "type": "skill_level",
        "name": name,
        "name_en": None,
        "name_zh": name,
        "description": f"{occ_name}（{occ_code}）· {function} · {level_zh}"
        + (f" · 权重{weight_pct}%" if weight_pct is not None else ""),
        "attrs": attrs,
        "source_system": SOURCE_SYSTEM,
        "source_id": sid,
        "source_url": source_url,
        "license": LICENSE,
        "fetched_at": fetched_at,
        "confidence": "official",
    }
    # 采集端与灌库端走同一份归一，落地形态才一致；少了这一步，采下来的节点
    # 留着 level_zh、name 还是「制备 · 三级/高级工」，与迁移后的存量对不上。
    normalize_skill_level_node(node)
    return node


def make_requires_edge(
    *,
    occ_node_id: str,
    skill_id: str,
    evidence: str,
    source_url: str,
    source_id: str,
    weight: float | None,
    fetched_at: str,
) -> dict:
    return {
        "id": make_edge_id(occ_node_id, "requires", skill_id),
        "src_id": occ_node_id,
        "dst_id": skill_id,
        "rel_type": "requires",
        "region": REGION,
        "weight": weight,
        "evidence": evidence,
        "attrs": {"link_basis": "national_skill_standard_weight_or_work_req"},
        "source_system": SOURCE_SYSTEM,
        "source_id": source_id,
        "source_url": source_url,
        "license": LICENSE,
        "fetched_at": fetched_at,
        "confidence": "official",
    }


def resolve_source_url(filename: str) -> str:
    if "人工智能" in filename:
        if "mirror" in filename:
            return (
                "http://114.255.111.180/xxgk2020/fdzdgknr/rcrs_4225/jnrc/"
                "202112/W020211227626977039770.pdf"
            )
        return (
            "https://www.mohrss.gov.cn/SYrlzyhshbzb/zcfg/SYzhengqiuyijian/"
            "202106/W020210617509883457681.pdf"
        )
    if "程序设计" in filename:
        return "https://chinajob.mohrss.gov.cn/h5/c/2022-08-05/357338.shtml"
    return "https://www.mohrss.gov.cn/"


def find_occ_id(conn, code: str, name: str | None) -> str | None:
    codes = [code]
    if code in CODE_ALIASES:
        codes.append(CODE_ALIASES[code])
    # reverse alias
    for old, new in CODE_ALIASES.items():
        if code == new:
            codes.append(old)
    for c in codes:
        row = conn.execute(
            "SELECT id FROM nodes WHERE region=? AND type='occupation' AND source_system='MOHRSS_CN' AND source_id=?",
            (REGION, c),
        ).fetchone()
        if row:
            return row["id"] if hasattr(row, "keys") else row[0]
    if name:
        name_clean = re.sub(r"[LS\s]", "", name)
        row = conn.execute(
            """
            SELECT id, name FROM nodes
            WHERE region=? AND type='occupation' AND source_system='MOHRSS_CN'
              AND (name=? OR replace(replace(name,'S',''),' ','')=?)
            LIMIT 1
            """,
            (REGION, name, name_clean),
        ).fetchone()
        if row:
            return row["id"] if hasattr(row, "keys") else row[0]
        # 模糊：名称包含
        row = conn.execute(
            """
            SELECT id FROM nodes
            WHERE region=? AND type='occupation' AND source_system='MOHRSS_CN'
              AND name LIKE ?
            LIMIT 1
            """,
            (REGION, f"%{name[:8]}%"),
        ).fetchone()
        if row:
            return row["id"] if hasattr(row, "keys") else row[0]
    return None


def collect_pdfs() -> list[Path]:
    d = standards_dir()
    if not d.is_dir():
        return []
    chosen: list[Path] = []
    for name in PREFERRED:
        p = d / name
        if p.is_file():
            chosen.append(p)
    # 含 osta_pdfs 批量目录与根目录散落 PDF
    for p in sorted(d.rglob("*.pdf")):
        if p in chosen:
            continue
        if "人工智能训练师" in p.name and "mirror" not in p.name and any(
            "人工智能训练师" in c.name and "mirror" in c.name for c in chosen
        ):
            continue
        # 跳过明显非标准的编制规程等
        if "编制技术规程" in p.name:
            continue
        chosen.append(p)
    return chosen


def parse_one(path: Path, fetched_at: str) -> tuple[list[dict], list[dict], dict]:
    text = pdf_text(path)
    meta = parse_meta(text, path.name)
    levels = meta["levels"]
    funcs = parse_skill_weight_functions(text, levels)
    method = "weight_table"
    if not funcs:
        funcs = fallback_function_levels(text, levels)
        method = "work_req_headers"
    # 去重
    uniq: dict[tuple, dict] = {}
    for f in funcs:
        key = (f["function"], f["level_code"])
        uniq[key] = f
    funcs = list(uniq.values())

    source_url = resolve_source_url(path.name)
    nodes: list[dict] = []
    for f in funcs:
        if not meta["occupation_code"] or not meta["occupation_name"]:
            break
        nodes.append(
            make_skill_node(
                occ_code=meta["occupation_code"],
                occ_name=meta["occupation_name"],
                function=f["function"],
                level_zh=f["level_zh"],
                level_code=f["level_code"],
                weight_pct=f.get("weight_pct"),
                edition=meta.get("edition"),
                source_url=source_url,
                source_file=path.name,
                fetched_at=fetched_at,
            )
        )

    report = {
        "file": path.name,
        "meta": {
            "occupation_code": meta["occupation_code"],
            "occupation_name": meta["occupation_name"],
            "edition": meta.get("edition"),
            "levels": [{"zh": z, "code": c} for z, c in levels],
        },
        "parse_method": method,
        "skill_rows": len(funcs),
        "skill_nodes": len(nodes),
        "sample_functions": sorted({f["function"] for f in funcs})[:12],
    }
    # edges filled later when occ id known — return skill nodes only + meta
    return nodes, meta, report


def ingest(dry_run: bool = False, db_path: Path | None = None) -> dict:
    ensure_dirs()
    fetched_at = utc_now_iso()
    pdfs = collect_pdfs()
    result: dict = {
        "source_system": SOURCE_SYSTEM,
        "fetched_at": fetched_at,
        "dry_run": dry_run,
        "files": [],
    }
    if not pdfs:
        result["error"] = "no PDFs in data/raw/CN/skill_standards/"
        return result

    all_nodes: list[dict] = []
    pending_edges: list[tuple[dict, dict]] = []  # (meta, skill_node)

    for pdf in pdfs:
        nodes, meta, rep = parse_one(pdf, fetched_at)
        result["files"].append(rep)
        all_nodes.extend(nodes)
        for n in nodes:
            pending_edges.append((meta, n))

    # 去重 skill nodes by id
    by_id = {n["id"]: n for n in all_nodes}
    all_nodes = list(by_id.values())
    result["nodes_parsed"] = len(all_nodes)
    result["sample"] = [
        {"id": n["id"], "name": n["name"], "source_id": n["source_id"]}
        for n in all_nodes[:10]
    ]

    if dry_run:
        return result

    conn = connect(db_path)
    edges: list[dict] = []
    edge_miss: list[str] = []
    try:
        for meta, n in pending_edges:
            code = meta.get("occupation_code")
            name = meta.get("occupation_name")
            if not code:
                continue
            occ_id = find_occ_id(conn, code, name)
            if not occ_id:
                edge_miss.append(f"{code}:{name}")
                continue
            w = n["attrs"].get("weight_pct")
            edges.append(
                make_requires_edge(
                    occ_node_id=occ_id,
                    skill_id=n["id"],
                    evidence=(
                        f"《{name}国家职业技能标准》技能要求："
                        f"{n['attrs']['skill_name']} @ {n['attrs']['level_zh']}"
                        + (f"（权重{w}%）" if w is not None else "")
                    ),
                    source_url=n["source_url"],
                    source_id=n["source_id"],
                    weight=(float(w) / 100.0) if w is not None else None,
                    fetched_at=fetched_at,
                )
            )
        # edge dedupe
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
    result["edge_occupation_miss"] = sorted(set(edge_miss))
    result["db_stats"] = s
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "ingest_cn_skill_standards.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest CN national skill standards (pilot)"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    out = ingest(dry_run=args.dry_run, db_path=Path(args.db) if args.db else None)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
