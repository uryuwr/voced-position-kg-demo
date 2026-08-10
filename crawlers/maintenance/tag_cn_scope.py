"""
按 docs/职业教育数据范围.md 给已有 CN 节点打标签（可重复执行）。

- major.attrs.education_type: general | vocational
- occupation.attrs.recommend_tier: primary | secondary | low
  规则（收紧版）：
  - low: 1/7/8 大类；或名称命中生活服务降权词表（保安/保洁等）
  - secondary: 3 大类
  - primary: 2/4/5/6 大类且未命中降权词表
  - 若岗位已有 prepares_for 入边 → 至少升为 primary（证据优先）

Usage:
  python -m crawlers.maintenance.tag_cn_scope
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

from backend.kg.graph_store import connect
from backend.kg.paths import REPORTS

# 默认推荐降权：与院校主培养链弱相关的生活服务/简单劳务等
# 命中即 recommend_tier=low（仍可在大典底库检索）
LOW_NAME_PATTERNS = [
    r"保安",
    r"安保",
    r"保洁",
    r"清洁",
    r"环卫",
    r"保姆",
    r"家政",
    r"护工",  # 养老护理另有技能标准时可被 prepares_for 抬升
    r"门卫",
    r"看门",
    r"传达室",
    r"废品回收",
    r"垃圾清运",
    r"公厕",
    r"洗衣",
    r"熨烫",
    r"搬运工",
    r"装卸工",
    r"勤杂",
    r"杂务",
    r"传菜",
    r"洗碗",
    r"锅炉司炉",  # 边界；可后续白名单抬升
    r"安检员",
    r"消防员",  # 有特殊培养链时可被 prepares_for 抬升
]


def _load_attrs(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        return json.loads(raw)
    except Exception:
        return {}


def tag_majors(conn) -> int:
    rows = conn.execute(
        "SELECT id, attrs FROM nodes WHERE region='CN' AND type='major'"
    ).fetchall()
    n = 0
    for row in rows:
        attrs = _load_attrs(row["attrs"])
        level = attrs.get("level") or ""
        if level == "ug_bachelor":
            et = "general"
        elif level in ("voc_secondary", "voc_associate", "voc_bachelor"):
            et = "vocational"
        else:
            et = "other"
        if attrs.get("education_type") == et:
            continue
        attrs["education_type"] = et
        conn.execute(
            "UPDATE nodes SET attrs=? WHERE id=?",
            (json.dumps(attrs, ensure_ascii=False), row["id"]),
        )
        n += 1
    return n


def name_is_low_priority(name: str) -> bool:
    n = name or ""
    for pat in LOW_NAME_PATTERNS:
        if re.search(pat, n):
            return True
    return False


def recommend_tier(source_id: str, name: str, has_prepares_for: bool) -> str:
    # 证据优先：已被专业培养对口 → primary
    if has_prepares_for:
        return "primary"
    if name_is_low_priority(name):
        return "low"
    head = (source_id or "")[:1]
    if head in ("1", "7", "8"):
        return "low"
    if head == "3":
        return "secondary"
    if head in ("2", "4", "5", "6"):
        return "primary"
    return "low"


def tag_occupations(conn) -> int:
    # 有 prepares_for 入边的岗位
    linked = {
        r[0]
        for r in conn.execute(
            """
            SELECT DISTINCT dst_id FROM edges
            WHERE rel_type='prepares_for' AND region='CN'
            """
        ).fetchall()
    }
    rows = conn.execute(
        """
        SELECT id, source_id, name, attrs FROM nodes
        WHERE region='CN' AND type='occupation' AND source_system='MOHRSS_CN'
        """
    ).fetchall()
    n = 0
    for row in rows:
        attrs = _load_attrs(row["attrs"])
        has_pf = row["id"] in linked
        tier = recommend_tier(row["source_id"] or "", row["name"] or "", has_pf)
        if (
            attrs.get("recommend_tier") == tier
            and attrs.get("in_project_scope") is True
            and attrs.get("has_prepares_for") == has_pf
        ):
            continue
        attrs["recommend_tier"] = tier
        attrs["in_project_scope"] = True
        attrs["has_prepares_for"] = has_pf
        if name_is_low_priority(row["name"] or ""):
            attrs["demote_reason"] = "lifestyle_service_lexicon"
        elif (row["source_id"] or "")[:1] in ("1", "7", "8"):
            attrs["demote_reason"] = "major_class_1_7_8"
        else:
            attrs.pop("demote_reason", None)
        conn.execute(
            "UPDATE nodes SET attrs=? WHERE id=?",
            (json.dumps(attrs, ensure_ascii=False), row["id"]),
        )
        n += 1
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description="Tag CN major/occupation scope attrs")
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Print hint to run neo4j migrate",
    )
    args = parser.parse_args()

    conn = connect()
    try:
        n_maj = tag_majors(conn)
        n_occ = tag_occupations(conn)
        conn.commit()
        gen = conn.execute(
            """
            SELECT COUNT(*) FROM nodes
            WHERE region='CN' AND type='major'
              AND attrs LIKE '%"education_type": "general"%'
            """
        ).fetchone()[0]
        voc = conn.execute(
            """
            SELECT COUNT(*) FROM nodes
            WHERE region='CN' AND type='major'
              AND attrs LIKE '%"education_type": "vocational"%'
            """
        ).fetchone()[0]
        counts = {}
        for tier in ("primary", "secondary", "low"):
            counts[tier] = conn.execute(
                f"""
                SELECT COUNT(*) FROM nodes
                WHERE region='CN' AND type='occupation'
                  AND attrs LIKE '%"recommend_tier": "{tier}"%'
                """
            ).fetchone()[0]
        demoted = conn.execute(
            """
            SELECT COUNT(*) FROM nodes
            WHERE region='CN' AND type='occupation'
              AND attrs LIKE '%lifestyle_service_lexicon%'
            """
        ).fetchone()[0]
    finally:
        conn.close()

    report = {
        "majors_updated": n_maj,
        "occupations_updated": n_occ,
        "major_education_type_general": gen,
        "major_education_type_vocational": voc,
        "occupation_by_tier": counts,
        "occupation_demoted_by_lexicon": demoted,
        "scope_doc": "docs/职业教育数据范围.md",
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "tag_cn_scope.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.migrate:
        print("Run: python -m backend.kg.neo4j_store.migrate")


if __name__ == "__main__":
    main()
