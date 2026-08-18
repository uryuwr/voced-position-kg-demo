"""把「课标目录条目」升级为**可学的真实课程**。

问题
----
课程库里 15960 门来自教育部专业课标（`source_system=MOE_CN`，`role=curriculum_catalog`），
它们是培养方案里的**课目名称**，`source_url` 全指向课标目录页 —— 点开是专业培养方案，
不是课程内容。本体早有约定「课标中的课目名称不能单独冒充可学资源」，
但学员在岗位详情里看到它们，会以为点进去能学。

思路
----
课标课程名（`PythonWeb开发`、`NoSQL数据库技术与应用`）**比技能名更接近课程命名习惯**
—— 技能名是「能力」（SpringBoot开发），课程名是「课程」（Java程序设计）。所以用
课标课程名去慕课平台反查，命中率高于用技能名搜。

命中后**不删课标条目**（它是专业培养方案的一部分，有独立价值），而是：
1. 建真实课程节点
2. 给同一技能补一条 `taught_by` → 真实课程
3. 课标条目上标 `attrs.has_real_alternative=true`，前端可折叠

用法::

    python -m crawlers.cn.upgrade_catalog_to_real_course --l1 技术 --dry-run
    python -m crawlers.cn.upgrade_catalog_to_real_course --l1 技术
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, upsert_edges, upsert_nodes
from backend.kg.paths import REPORTS, STAGING
from backend.kg.provenance import make_edge_id, make_node_id, utc_now_iso
from crawlers.cn.harvest_mooc_courses import (
    COURSE_URL,
    LICENSE,
    MoocClient,
    SRC_COURSE,
    is_relevant,
    slug,
)

REGION = "CN"
SRC_LINK = "LINK_CN_RULE"
OUT = STAGING / "catalog_upgrade.json"


def catalog_targets(conn, l1: str) -> list[dict]:
    """技术类岗位技能上挂着的课标条目：(技能节点, 课标课程名)。"""
    rows = conn.execute(
        """
        SELECT DISTINCT n.id AS skill_id, n.attrs AS skill_attrs, n.name AS skill_name,
               c.id AS cat_id, c.name AS cat_name
        FROM nodes o
        JOIN edges re ON re.src_id = o.id AND re.rel_type = 'requires'
        JOIN nodes n  ON n.id = re.dst_id AND n.type = 'skill_level'
        JOIN edges te ON te.src_id = n.id AND te.rel_type = 'taught_by'
        JOIN nodes c  ON c.id = te.dst_id AND c.source_system = 'MOE_CN'
        WHERE o.type = 'occupation' AND o.source_system = 'BOSS'
        """
    ).fetchall()
    out = []
    for r in rows:
        try:
            a = json.loads(r[1] or "{}")
        except Exception:
            a = {}
        if l1 not in ("all", "*") and a.get("boss_l1") not in (None, l1):
            pass  # 技能节点没有 boss_l1，靠上面 JOIN 的岗位已过滤
        out.append({
            "skill_id": r[0],
            "skill_key": a.get("skill_key") or str(r[2] or "").split(" · ")[0],
            "level": a.get("level"),
            "catalog_id": r[3],
            "catalog_name": r[4],
        })
    return out


def run(*, l1: str, dry_run: bool, sleep: float, min_learners: int) -> dict:
    fetched_at = utc_now_iso()
    conn = connect()
    try:
        targets = catalog_targets(conn, l1)
        # 同一课标课程名可能挂在多个技能上，按名字去重后只搜一次
        by_name: dict[str, list[dict]] = {}
        for t in targets:
            by_name.setdefault(t["catalog_name"], []).append(t)

        cli = MoocClient()
        cli.open_session()
        found: dict[str, list[dict]] = {}
        failed = []
        for name in sorted(by_name):
            try:
                got = cli.search(name)
            except Exception as e:
                failed.append({"catalog": name, "error": str(e)[:100]})
                time.sleep(max(sleep, 1.2))
                continue
            hits = [c for c in got
                    if (c["learners"] or 0) >= min_learners
                    and is_relevant(name, name, c["name"])
                    and c.get("school_short") and c.get("course_id")]
            if hits:
                found[name] = sorted(hits, key=lambda x: -(x["learners"] or 0))[:2]
            time.sleep(sleep)

        STAGING.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"generated_at": fetched_at, "items": found},
                                  ensure_ascii=False, indent=2), encoding="utf-8")

        new_nodes, edges, mark_catalog = {}, [], set()
        for name, courses in found.items():
            for t in by_name[name]:
                for c in courses:
                    ident = f"{c['school_short']}-{c['course_id']}"
                    cid = make_node_id(REGION, "course", SRC_COURSE, ident)
                    url = COURSE_URL.format(school_short=c["school_short"], course_id=c["course_id"])
                    new_nodes.setdefault(cid, {
                        "id": cid, "region": REGION, "type": "course",
                        "name": c["name"], "name_en": None, "name_zh": c["name"],
                        "aliases": None,
                        "description": (f"{c['name']}"
                                        + (f"（{c['school']}）" if c.get("school") else "")
                                        + f" · 中国大学MOOC 选课 {c['learners']} 人"),
                        "attrs": json.dumps({"role": "learnable_resource", "playable": True,
                                             "match_method": "catalog_name_to_mooc",
                                             "learner_count": c["learners"],
                                             "school": c.get("school"), "img_url": c.get("img"),
                                             "from_catalog": name}, ensure_ascii=False),
                        "source_system": SRC_COURSE, "source_id": ident,
                        "source_url": url, "license": LICENSE,
                        "fetched_at": fetched_at, "confidence": "official",
                    })
                    edges.append({
                        "id": make_edge_id(t["skill_id"], "taught_by", cid),
                        "src_id": t["skill_id"], "dst_id": cid, "rel_type": "taught_by",
                        "region": REGION, "weight": 0.9,
                        "evidence": (f"课标课目「{name}」→ 中国大学MOOC《{c['name']}》"
                                     + (f"（{c['school']}）" if c.get("school") else "")
                                     + f"，选课 {c['learners']} 人；替代课标目录页成为可学资源"),
                        "attrs": json.dumps({"match_method": "catalog_name_to_mooc",
                                             "skill_key": t["skill_key"],
                                             "from_catalog": name,
                                             "learner_count": c["learners"]}, ensure_ascii=False),
                        "source_system": SRC_LINK, "source_id": f"{t['skill_id']}->{cid}",
                        "source_url": url, "license": LICENSE,
                        "fetched_at": fetched_at, "confidence": "official",
                    })
                mark_catalog.add(t["catalog_id"])

        rep = {"l1": l1, "dry_run": dry_run,
               "catalog_entries_on_skills": len(targets),
               "distinct_catalog_names": len(by_name),
               "matched_names": len(found),
               "new_course_nodes": len(new_nodes),
               "taught_by_edges": len(edges),
               "catalog_marked": len(mark_catalog),
               "failed": failed[:6],
               "sample": {k: [(c["name"], c["learners"]) for c in v]
                          for k, v in list(found.items())[:8]}}

        if not dry_run:
            if new_nodes:
                rep["nodes_upserted"] = upsert_nodes(conn, list(new_nodes.values()))
            rep["edges_upserted"] = upsert_edges(conn, edges)
            # 课标条目不删：它是培养方案的一部分。只打标记，供前端折叠展示
            for cat_id in mark_catalog:
                row = conn.execute("SELECT attrs FROM nodes WHERE id=?", (cat_id,)).fetchone()
                try:
                    a = json.loads((row[0] if row else "") or "{}")
                except Exception:
                    a = {}
                a["has_real_alternative"] = True
                conn.execute("UPDATE nodes SET attrs=? WHERE id=?",
                             (json.dumps(a, ensure_ascii=False), cat_id))
            conn.commit()
    finally:
        conn.close()

    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"catalog_upgrade_{slug(l1)}_{'dryrun' if dry_run else 'applied'}.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    return rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--l1", default="技术")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.8)
    ap.add_argument("--min-learners", type=int, default=3000)
    args = ap.parse_args()
    print(json.dumps(run(l1=args.l1, dry_run=args.dry_run, sleep=args.sleep,
                         min_learners=args.min_learners), ensure_ascii=False, indent=2)[:2200])


if __name__ == "__main__":
    main()
