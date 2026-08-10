"""P1-5：专业→行业 belongs_to 启发式补边（门类词典 + 关键词）。

Usage:
  python scripts/link_major_industry_heuristic.py --dry-run --limit 100
  python scripts/link_major_industry_heuristic.py --limit 2000
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.write import create_edge

# 教育部门类/专业关键词 → Boss 行业名子串（命中 industry.name 即可）
CATEGORY_TO_INDUSTRY_KEYS: list[tuple[str, list[str]]] = [
    ("农业", ["农林", "农业", "畜牧", "渔业"]),
    ("植物生产", ["农林", "农业"]),
    ("林业", ["农林", "林业"]),
    ("畜牧", ["农林", "畜牧"]),
    ("水产", ["农林", "渔业"]),
    ("计算机", ["互联网", "计算机", "软件", "IT", "信息"]),
    ("软件", ["互联网", "软件", "计算机"]),
    ("电子", ["电子", "半导体", "通信", "硬件"]),
    ("通信", ["通信", "运营商", "互联网"]),
    ("自动化", ["智能制造", "机械", "电气", "工业"]),
    ("机械", ["机械", "装备", "制造"]),
    ("汽车", ["汽车", "新能源车", "汽车后市场"]),
    ("土木", ["建筑", "土木", "工程"]),
    ("建筑", ["建筑", "房地产", "装修"]),
    ("医学", ["医疗", "医院", "健康", "医药"]),
    ("护理", ["医疗", "健康", "护理"]),
    ("药学", ["医药", "医疗"]),
    ("会计", ["会计", "审计", "财税", "金融"]),
    ("金融", ["金融", "银行", "证券", "保险"]),
    ("经济", ["金融", "咨询", "商务"]),
    ("贸易", ["贸易", "进出口", "批发", "零售"]),
    ("物流", ["物流", "运输", "供应链", "仓储"]),
    ("旅游", ["旅游", "酒店", "餐饮"]),
    ("酒店", ["酒店", "餐饮", "旅游"]),
    ("教育", ["教育", "培训", "学校"]),
    ("艺术", ["文娱", "传媒", "广告", "设计"]),
    ("设计", ["设计", "广告", "文化"]),
    ("新闻", ["传媒", "广告", "互联网"]),
    ("法学", ["法律", "咨询"]),
    ("公安", ["政府", "公共"]),
    ("能源", ["能源", "电力", "石油"]),
    ("化工", ["化工", "材料"]),
    ("材料", ["材料", "化工", "制造"]),
    ("环境", ["环保", "环境", "公用事业"]),
    ("食品", ["食品", "餐饮", "消费品"]),
    ("服装", ["服装", "纺织", "消费品"]),
    ("体育", ["体育", "健身", "文娱"]),
    ("管理", ["企业服务", "咨询", "人力"]),
    ("营销", ["广告", "电商", "互联网", "零售"]),
    ("电商", ["电商", "互联网", "零售"]),
    ("人工智能", ["互联网", "人工智能", "软件"]),
    ("大数据", ["互联网", "软件", "信息"]),
    ("智能制造", ["智能制造", "制造", "工业"]),
]


def _parse_attrs(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def score_industry(ind_name: str, keys: list[str]) -> int:
    score = 0
    for k in keys:
        if k in ind_name:
            score += len(k)
    return score


def pick_industry(major_name: str, category: str, industries: list[dict]) -> str | None:
    text = f"{category} {major_name}"
    best_id, best = None, 0
    for cat_key, ind_keys in CATEGORY_TO_INDUSTRY_KEYS:
        if cat_key not in text:
            continue
        for ind in industries:
            sc = score_industry(ind["name"], ind_keys)
            if sc > best:
                best, best_id = sc, ind["id"]
    # 专业名直接撞行业名
    if not best_id:
        for ind in industries:
            n = ind["name"]
            short = n.split("/")[0]
            if len(short) >= 2 and short in major_name:
                return ind["id"]
            if len(short) >= 2 and short in category:
                return ind["id"]
    return best_id


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--force", action="store_true", help="已有 belongs_to 也再补（默认跳过）")
    args = ap.parse_args()

    with connect() as conn:
        industries = [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, name FROM kg_node
                WHERE type='industry' AND region='CN'
                  AND COALESCE(status,'published')='published'
                """
            ).fetchall()
        ]
        majors = [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, name, attrs FROM kg_node
                WHERE type='major' AND region='CN'
                  AND COALESCE(status,'published')='published'
                ORDER BY name
                """
            ).fetchall()
        ]

    created = 0
    skipped = 0
    unmatched = 0
    samples = []
    for m in majors:
        if created >= args.limit:
            break
        if not args.force:
            with connect() as conn:
                exists = conn.execute(
                    """
                    SELECT 1 FROM kg_edge
                    WHERE src_id=%s AND rel_type='belongs_to' LIMIT 1
                    """,
                    (m["id"],),
                ).fetchone()
            if exists:
                skipped += 1
                continue
        attrs = _parse_attrs(m.get("attrs"))
        cat = str(attrs.get("category") or attrs.get("catalog") or "")
        hit = pick_industry(str(m.get("name") or ""), cat, industries)
        if not hit:
            unmatched += 1
            continue
        samples.append(
            {
                "major": m["name"],
                "category": cat[:40],
                "industry_id": hit,
            }
        )
        if args.dry_run:
            created += 1
            continue
        try:
            create_edge(
                {
                    "src_id": m["id"],
                    "dst_id": hit,
                    "rel_type": "belongs_to",
                    "region": "CN",
                    "weight": 0.55,
                    "confidence": "derived",
                    "status": "published",
                    "source_system": "HEURISTIC",
                    "source_url": "manual://major-industry-dict",
                    "evidence": f"门类词典匹配 category={cat[:60]}",
                },
                user_id="system",
                user_name="heuristic",
            )
            created += 1
        except Exception as e:
            samples.append({"error": str(e)[:80], "major": m["name"]})

    out = {
        "created": created,
        "skipped_has_edge": skipped,
        "unmatched": unmatched,
        "dry_run": args.dry_run,
        "samples": samples[:20],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    rep = ROOT / "reports" / "link_major_industry_heuristic.json"
    rep.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
