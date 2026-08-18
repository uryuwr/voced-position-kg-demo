"""BOSS 市场化岗位 → 行业 belongs_to 边。

为什么需要映射表而不是直接连
----------------------------
BOSS 有两棵**互不关联**的树：

    position.json  职位类别：技术 > 后端开发 > Java
    industry.json  公司行业：互联网/AI > 云计算

接口不提供两者的对应关系，且岗位↔行业天然是 **N:M**（Java 岗位在互联网、金融、
制造业都有）。所以这里用「职位一级门类 → 行业一级分类」的人工映射表推导，
`confidence=derived`（规则推导，非 LLM 猜测），evidence 写明依据。

粒度取舍
--------
按**一级门类**映射（21 → 15），而不是按 851 个岗位逐个判断：
- 优点：确定、可复算、零 LLM 成本，821 个岗位全覆盖
- 代价：同门类岗位挂到同一批行业（所有技术岗位都归「互联网/AI + 电子/通信/半导体」）
- 后续细化方向：按二级方向（115 个）映射到二级行业（134 个），或用 LLM 逐岗位判断

`weight` 表达主次：主行业 1.0，相关行业 0.6。

用法::

    python -m crawlers.cn.link_boss_position_industry --dry-run
    python -m crawlers.cn.link_boss_position_industry
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, upsert_edges
from backend.kg.paths import REPORTS
from backend.kg.provenance import make_edge_id, utc_now_iso

REGION = "CN"
SOURCE_SYSTEM = "LINK_CN_RULE"
SOURCE_URL = "https://www.zhipin.com/wapi/zpCommon/data/position.json"
LICENSE = "规则推导；职位与行业分类均来自 BOSS 公开分类树"

# 职位一级门类 → (主行业 1.0, 相关行业 0.6)
# 行业名必须与 industry 节点的 name 完全一致（level=1 的 15 个）
POSITION_TO_INDUSTRY: dict[str, tuple[list[str], list[str]]] = {
    "技术": (["互联网/AI"], ["电子/通信/半导体"]),
    "产品": (["互联网/AI"], []),
    "设计": (["互联网/AI"], ["广告/传媒/文化/体育"]),
    "客服/运营": (["互联网/AI"], ["服务业", "消费品/批发/零售"]),
    "市场/公关/广告": (["广告/传媒/文化/体育"], ["互联网/AI"]),
    "人力/财务/行政": (["专业服务"], []),
    "高级管理": (["专业服务"], []),
    "销售": (["消费品/批发/零售"], ["专业服务"]),
    "直播/影视/传媒": (["广告/传媒/文化/体育"], ["互联网/AI"]),
    "金融": (["金融"], []),
    "教育培训": (["教育培训"], []),
    "医疗健康": (["制药/医疗"], []),
    "采购/贸易": (["消费品/批发/零售"], ["交通运输/物流"]),
    "供应链/物流": (["交通运输/物流"], []),
    "房地产/建筑": (["房地产/建筑"], []),
    "农/林/牧/渔": (["制造业"], []),  # industry 一级无农林牧渔，二级有；暂挂制造业
    "咨询/翻译/法律": (["专业服务"], []),
    "旅游": (["服务业"], []),
    "服务业": (["服务业"], []),
    "生产制造": (["制造业"], ["汽车", "能源/化工/环保"]),
    "其他": (["政府/非营利组织/其他"], []),
}


def load_industry_ids(conn) -> dict[str, str]:
    """行业名 → 节点 id（只取 level=1 的一级行业）。"""
    out = {}
    for row in conn.execute(
        "SELECT id, name, attrs FROM nodes WHERE type='industry' AND source_system='BOSS_ZHIPIN'"
    ):
        nid, name, attrs = row[0], row[1], row[2]
        try:
            lv = (json.loads(attrs or "{}")).get("level")
        except Exception:
            lv = None
        if lv == 1:
            out[name] = nid
    return out


def load_boss_occupations(conn) -> list[dict]:
    out = []
    for row in conn.execute(
        "SELECT id, name, attrs FROM nodes WHERE type='occupation' AND source_system='BOSS'"
    ):
        try:
            a = json.loads(row[2] or "{}")
        except Exception:
            a = {}
        out.append({"id": row[0], "name": row[1], "l1": a.get("boss_l1"), "l2": a.get("boss_l2")})
    return out


def build(*, dry_run: bool = False) -> dict:
    fetched_at = utc_now_iso()
    conn = connect()
    try:
        ind = load_industry_ids(conn)
        occs = load_boss_occupations(conn)

        report = {
            "fetched_at": fetched_at,
            "dry_run": dry_run,
            "industry_level1_found": len(ind),
            "boss_occupations": len(occs),
        }

        unmapped_ind = sorted(
            {n for main, rel in POSITION_TO_INDUSTRY.values() for n in main + rel} - set(ind)
        )
        if unmapped_ind:
            report["missing_industry_nodes"] = unmapped_ind

        edges = []
        no_rule = set()
        for o in occs:
            rule = POSITION_TO_INDUSTRY.get(o["l1"] or "")
            if not rule:
                no_rule.add(o["l1"])
                continue
            main, rel = rule
            for names, w in ((main, 1.0), (rel, 0.6)):
                for iname in names:
                    iid = ind.get(iname)
                    if not iid:
                        continue
                    eid = make_edge_id(o["id"], "belongs_to", iid)
                    edges.append(
                        {
                            "id": eid,
                            "src_id": o["id"],
                            "dst_id": iid,
                            "rel_type": "belongs_to",
                            "region": REGION,
                            "weight": w,
                            "evidence": (
                                f"BOSS 职位门类「{o['l1']}」→ 行业「{iname}」"
                                f"（{'主' if w == 1.0 else '相关'}行业，门类级规则映射）"
                            ),
                            "attrs": json.dumps(
                                {
                                    "match_method": "boss_position_l1_to_industry_l1",
                                    "boss_l1": o["l1"],
                                    "boss_l2": o["l2"],
                                    "industry_name": iname,
                                    "granularity": "position_l1",
                                },
                                ensure_ascii=False,
                            ),
                            "source_system": SOURCE_SYSTEM,
                            "source_id": f"{o['id']}->{iid}",
                            "source_url": SOURCE_URL,
                            "license": LICENSE,
                            "fetched_at": fetched_at,
                            "confidence": "derived",
                        }
                    )

        edges = list({e["id"]: e for e in edges}.values())
        report["edges_built"] = len(edges)
        report["occupations_covered"] = len({e["src_id"] for e in edges})
        if no_rule:
            report["position_l1_without_rule"] = sorted(x for x in no_rule if x)

        if not dry_run:
            report["upserted"] = upsert_edges(conn, edges)
            conn.commit()
    finally:
        conn.close()

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"boss_position_industry_{'dryrun' if dry_run else 'applied'}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(out)
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print(json.dumps(build(dry_run=args.dry_run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
