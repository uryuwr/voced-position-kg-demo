"""
无官方对照时：用规则+语义启发式为 CN 专业批量生成 prepares_for 候选并入库。

策略：
1. 课标已写入的 official 边保留（不覆盖 confidence=official）
2. 对尚无 prepares_for 的职教/应用向专业，按专业名关键词 → 大典岗位名匹配
3. confidence=ai_inferred，evidence 标明 LLM/规则推断，review_status=pending

Usage:
  python -m crawlers.cn.link_prepares_for_llm --dry-run
  python -m crawlers.cn.link_prepares_for_llm --limit 200
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
SOURCE_SYSTEM = "LINK_CN_AI"
LICENSE = "AI/规则推断专→岗；需抽检；非教育部正式对照"

# 专业名关键词 → 岗位名关键词（可多对多）
KEYWORD_MAP = [
    (["软件", "程序设计", "应用开发", "移动应用", "Web", "前端", "后端"], ["程序设计", "软件测试", "软件工程技术"]),
    (["网络", "信息安全", "网络安全", "信息通信"], ["网络工程技术", "网络与信息安全", "信息通信网络"]),
    (["人工智能", "智能计算", "机器学习"], ["人工智能工程技术", "人工智能训练"]),
    (["大数据", "数据科学", "数据挖掘", "数据技术"], ["大数据工程技术", "数据分析"]),
    (["物联网", "传感"], ["物联网工程技术", "物联网安装"]),
    (["云计算", "云服务"], ["云计算工程技术"]),
    (["区块链"], ["区块链工程技术", "区块链应用"]),
    (["虚拟现实", "VR", "数字媒体", "动漫", "游戏"], ["虚拟现实工程技术", "虚拟现实产品", "动画制作", "数字媒体技术"]),
    (["工业互联网", "智能制造", "工业机器人"], ["工业互联网工程技术", "智能制造工程技术", "工业机器人系统"]),
    (["机电", "数控", "机械制造", "模具", "焊接"], ["机床", "数控", "机械制造", "钳工", "模具", "焊接"]),
    (["汽车", "新能源车", "新能源汽车"], ["汽车", "新能源汽车"]),
    (["护理", "康养", "养老", "助产", "康复"], ["护理", "健康", "养老", "康复"]),
    (["电子商务", "跨境电商", "网店", "直播电商"], ["电子商务", "网店", "互联网营销"]),
    (["会计", "财务管理", "审计", "税务"], ["会计", "财务"]),
    (["建筑", "工程造价", "BIM", "工程管理", "市政"], ["建筑", "工程造价", "建筑信息模型", "工程管理"]),
    (["物流", "供应链", "冷链"], ["物流", "仓储", "供应链"]),
    (["电气", "自动化", "机电一体化"], ["电气", "自动化", "电工"]),
    (["计算机应用", "计算机科学", "信息技术"], ["程序设计", "软件工程技术", "信息系统", "计算机维修"]),
    (["电子信息", "应用电子", "微电子"], ["电子", "电子设备", "半导体"]),
    (["通信", "5G", "移动通信"], ["通信", "信息通信", "无线电"]),
    (["旅游", "酒店", "会展"], ["旅游", "酒店", "会展"]),
    (["烹饪", "餐饮", "西餐", "中餐"], ["中式烹调", "西式烹调", "餐饮"]),
    (["学前教育", "早期教育", "小学教育"], ["保育", "幼教", "教育"]),
    (["市场营销", "工商管理", "连锁经营"], ["营销", "商务", "连锁"]),
    (["环境", "环保", "水务", "生态"], ["环境保护", "环境监测", "污水处理"]),
    (["药学", "中药", "制药"], ["药学", "中药", "药物"]),
    (["农林", "园艺", "畜牧", "兽医"], ["农业", "园艺", "畜牧", "兽医"]),
    (["无人机", "低空经济", "飞行器"], ["无人机", "航空"]),
    (["集成电路", "芯片", "半导体"], ["集成电路", "半导体", "电子"]),
    (["轨道交通", "铁道", "城市轨道"], ["铁路", "城市轨道交通", "机车"]),
    (["民航", "空中乘务", "飞行"], ["民航", "航空"]),
    (["法律", "司法", "社会工作"], ["法律", "司法", "社会工作"]),
    (["设计", "视觉传达", "产品设计", "室内设计"], ["设计", "广告", "室内装饰"]),
]


def match_keywords(major_name: str) -> list[str]:
    hits = []
    for mkeys, okeys in KEYWORD_MAP:
        if any(k in major_name for k in mkeys):
            hits.extend(okeys)
    # 去重保序
    seen = set()
    out = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def ingest(dry_run: bool = False, limit: int = 0, per_major: int = 4) -> dict:
    ensure_dirs()
    fetched_at = utc_now_iso()
    conn = connect()
    edges = []
    report = {"majors_considered": 0, "edges": 0, "pairs_sample": [], "skipped_has_edge": 0}
    try:
        # 已有 prepares_for 的专业
        has_edge = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT src_id FROM edges WHERE rel_type='prepares_for' AND region=?",
                (REGION,),
            )
        }
        majors = conn.execute(
            """
            SELECT id, name, source_id, attrs FROM nodes
            WHERE region=? AND type='major'
            ORDER BY name
            """,
            (REGION,),
        ).fetchall()
        occs = conn.execute(
            """
            SELECT id, name, source_id, attrs FROM nodes
            WHERE region=? AND type='occupation' AND source_system='MOHRSS_CN'
            """,
            (REGION,),
        ).fetchall()
        # 过滤 low tier
        occ_list = []
        for o in occs:
            attrs = {}
            try:
                attrs = json.loads(o["attrs"] or "{}")
            except Exception:
                pass
            if attrs.get("recommend_tier") == "low":
                continue
            occ_list.append(o)

        for m in majors:
            if limit and report["majors_considered"] >= limit:
                break
            if m["id"] in has_edge:
                report["skipped_has_edge"] += 1
                continue
            mname = m["name"] or ""
            kws = match_keywords(mname)
            # 无词典命中时：用专业名核心词（去「技术/工程」等后缀）与岗位名重叠
            soft = False
            if not kws:
                core = re.sub(
                    r"(技术|工程|应用|服务|管理|教育|专业|与|及|的)$",
                    "",
                    mname,
                )
                core = re.sub(r"(技术|工程|应用)$", "", core)
                if len(core) >= 2:
                    kws = [core[:6] if len(core) > 6 else core]
                    soft = True
                else:
                    continue
            report["majors_considered"] += 1
            scored = []
            for o in occ_list:
                on = o["name"] or ""
                score = sum(1 for k in kws if k in on)
                # soft：专业名与岗位名互相包含
                if soft and score == 0:
                    if (len(mname) >= 2 and mname[:4] in on) or (
                        len(on) >= 2 and on[:4] in mname
                    ):
                        score = 1
                if score:
                    scored.append((score, o))
            scored.sort(key=lambda x: (-x[0], x[1]["name"] or ""))
            for score, o in scored[:per_major]:
                eid = make_edge_id(m["id"], "prepares_for", o["id"])
                edges.append(
                    {
                        "id": eid,
                        "src_id": m["id"],
                        "dst_id": o["id"],
                        "rel_type": "prepares_for",
                        "region": REGION,
                        "weight": float(score),
                        "evidence": f"规则/LLM关键词对齐：专业「{m['name']}」↔岗位「{o['name']}」(keywords={kws})",
                        "attrs": {
                            "match_method": "keyword_llm_heuristic",
                            "keywords": kws,
                            "score": score,
                            "review_status": "pending",
                        },
                        "source_system": SOURCE_SYSTEM,
                        "source_id": f"{m['source_id']}->{o['source_id']}",
                        "source_url": "https://www.moe.gov.cn/",
                        "license": LICENSE,
                        "fetched_at": fetched_at,
                        "confidence": "ai_inferred",
                    }
                )
                if len(report["pairs_sample"]) < 40:
                    report["pairs_sample"].append(
                        {"major": m["name"], "occupation": o["name"], "score": score}
                    )

        report["edges"] = len(edges)
        if dry_run:
            return report
        # 不覆盖已有 official 边
        to_write = []
        for e in edges:
            row = conn.execute("SELECT confidence FROM edges WHERE id=?", (e["id"],)).fetchone()
            if row and (row["confidence"] if hasattr(row, "keys") else row[0]) == "official":
                continue
            to_write.append(e)
        n = upsert_edges(conn, to_write)
        conn.commit()
        report["edges_upserted"] = n
        report["db_stats"] = stats(conn)
    finally:
        conn.close()
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "link_cn_prepares_for_llm.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--per-major", type=int, default=4)
    args = p.parse_args()
    print(json.dumps(ingest(args.dry_run, args.limit, args.per_major), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
