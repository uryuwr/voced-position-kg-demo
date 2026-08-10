"""
行业 → 岗位 belongs_to 边（关键词；招聘站行业名 ↔ 大典岗位名）。

confidence=ai_inferred，evidence 标明规则。
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

from backend.kg.graph_store import connect, stats, upsert_edges
from backend.kg.paths import REPORTS, ensure_dirs
from backend.kg.provenance import make_edge_id, utc_now_iso

REGION = "CN"
SOURCE = "LINK_CN_AI"

# 行业名关键词 → 岗位名关键词（可扩展）
INDUSTRY_OCC_KW = [
    (["互联网", "AI", "软件", "电子商务", "企业服务", "游戏"], ["软件", "程序", "网络", "互联网", "数据", "人工智能", "电商", "信息"]),
    (["电子", "通信", "半导体", "芯片", "硬件"], ["电子", "半导体", "通信", "芯片", "硬件", "集成电路"]),
    (["餐饮", "酒店", "美容", "美发", "休闲"], ["餐", "厨", "酒店", "旅游", "美容", "美发", "宾"]),
    (["零售", "批发", "服装", "食品", "贸易"], ["销售", "营业", "导购", "采购", "贸易", "收银"]),
    (["房地产", "建筑", "装修", "物业", "土木"], ["建筑", "造价", "施工", "物业", "装修", "土木", "工程管理"]),
    (["教育", "培训", "学前", "学校", "学术"], ["教师", "教育", "培训", "幼教", "保育", "教务"]),
    (["广告", "传媒", "文化", "体育", "影视", "出版"], ["广告", "设计", "传媒", "编辑", "记者", "体育", "文化"]),
    (["制造", "机械", "电气", "设备", "金属"], ["机", "钳", "焊", "数控", "电气", "装配", "制造"]),
    (["咨询", "财务", "审计", "法律", "人力", "检测"], ["会计", "审计", "律师", "咨询", "人力", "财务", "检测"]),
    (["医疗", "医美", "器械", "制药", "生物"], ["医", "护", "药", "临床", "康复", "检验"]),
    (["汽车", "新能源车", "车联网"], ["汽车", "机动车", "汽修", "新能源汽车"]),
    (["物流", "快递", "货运", "配送", "运输"], ["物流", "快递", "仓储", "配送", "货运", "司机"]),
    (["能源", "化工", "环保", "光伏", "储能", "风电", "电池"], ["电", "能源", "化工", "环保", "光伏", "电池"]),
    (["金融", "银行", "证券", "基金", "投资"], ["银行", "证券", "金融", "保险", "基金", "信贷"]),
    (["农", "林", "牧", "渔", "政府", "公共"], ["农", "林", "牧", "渔", "园艺", "公共", "社区"]),
]


def tokens(name: str) -> list[str]:
    return re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", name or "")


def match_kws(ind_name: str) -> list[str]:
    hits = []
    for iks, oks in INDUSTRY_OCC_KW:
        if any(k in ind_name for k in iks):
            hits.extend(oks)
    # 行业自身分词
    hits.extend([t for t in tokens(ind_name) if len(t) >= 2])
    seen = set()
    out = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def ingest(dry_run: bool = False, per_industry: int = 12) -> dict:
    ensure_dirs()
    fetched_at = utc_now_iso()
    conn = connect()
    report = {"industries": 0, "edges": 0, "sample": []}
    edges = []
    try:
        industries = conn.execute(
            """
            SELECT id, name, attrs FROM nodes
            WHERE region=? AND type='industry' AND source_system='BOSS_ZHIPIN'
            ORDER BY name
            """,
            (REGION,),
        ).fetchall()
        occs = conn.execute(
            """
            SELECT id, name, attrs FROM nodes
            WHERE region=? AND type='occupation' AND source_system='MOHRSS_CN'
            """,
            (REGION,),
        ).fetchall()
        occ_list = []
        for o in occs:
            try:
                a = json.loads(o["attrs"] or "{}")
            except Exception:
                a = {}
            if a.get("recommend_tier") == "low":
                continue
            occ_list.append(o)

        for ind in industries:
            report["industries"] += 1
            # 优先用叶子行业（level>=2）挂岗，门类太宽
            try:
                ia = json.loads(ind["attrs"] or "{}")
            except Exception:
                ia = {}
            if int(ia.get("level") or 1) < 2:
                continue
            kws = match_kws(ind["name"] or "")
            if not kws:
                continue
            scored = []
            for o in occ_list:
                on = o["name"] or ""
                sc = sum(1 for k in kws if k in on)
                if sc:
                    scored.append((sc, o))
            scored.sort(key=lambda x: (-x[0], x[1]["name"] or ""))
            n = 0
            for sc, o in scored[:per_industry]:
                # occupation belongs_to industry
                eid = make_edge_id(o["id"], "belongs_to", ind["id"])
                edges.append(
                    {
                        "id": eid,
                        "src_id": o["id"],
                        "dst_id": ind["id"],
                        "rel_type": "belongs_to",
                        "region": REGION,
                        "weight": min(1.0, 0.4 + 0.12 * sc),
                        "evidence": f"行业「{ind['name']}」关键词对齐岗位「{o['name']}」(score={sc})",
                        "attrs": {
                            "match_method": "industry_occupation_keyword",
                            "keywords": kws[:12],
                            "score": sc,
                            "review_status": "pending",
                        },
                        "source_system": SOURCE,
                        "source_id": f"{o['id']}->{ind['id']}",
                        "source_url": "https://www.zhipin.com/wapi/zpCommon/data/industry.json",
                        "license": "规则对齐；行业来自 BOSS 分类树",
                        "fetched_at": fetched_at,
                        "confidence": "ai_inferred",
                    }
                )
                n += 1
            if n and len(report["sample"]) < 20:
                report["sample"].append({"industry": ind["name"], "links": n, "top": scored[0][1]["name"] if scored else None})

        report["edges"] = len(edges)
        if dry_run:
            return report
        eby = {e["id"]: e for e in edges}
        edges = list(eby.values())
        up = upsert_edges(conn, edges)
        conn.commit()
        report["edges_upserted"] = up
        report["db_stats"] = stats(conn)
    finally:
        conn.close()
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "link_cn_industry_occupations.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--per-industry", type=int, default=12)
    args = p.parse_args()
    print(json.dumps(ingest(args.dry_run, args.per_industry), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
