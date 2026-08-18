"""学堂在线课程采集 —— 补中国大学MOOC 没覆盖到的技能。

定位
----
`harvest_mooc_courses.py` 跑完后，552 个技术技能里仍有约 4 成没有真实课程
（多是 `Jetpack组件使用`、`EMC/EMI设计` 这类工程细分）。本脚本用学堂在线补这批。

合规
----
- `xuetangx.com` **无 robots.txt**（HTTP 404，实测 2026-08-17）→ 未设抓取限制
- 接口 `POST /api/v1/lms/get_product_list/` 是搜索页自身调用的公开接口，
  契约用 Playwright 观察页面自己发出的请求得到（**不逆向签名、无需登录**）；
  必需自定义头 `xtbz: xt` + `django-language: zh`，缺了会 4xx
- 单线程 + 请求间隔限速；UA 标明研究用途

复用 `harvest_mooc_courses` 的两道质量门槛
----------------------------------------
1. `--min-learners` 学习人数下限（默认 3000）
2. `is_relevant()` 名称相关性 —— **这道不能省**：搜索是模糊匹配，搜不到时会返回
   热门课，MOOC 那轮就出现过「3D图形渲染」匹配到《猴博士高数不挂科》

用法::

    python -m crawlers.cn.harvest_xuetangx_courses --stage fetch --l1 技术 --only-missing
    python -m crawlers.cn.harvest_xuetangx_courses --stage apply --l1 技术 --dry-run
    python -m crawlers.cn.harvest_xuetangx_courses --stage apply --l1 技术
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, upsert_edges, upsert_nodes
from backend.kg.paths import REPORTS, STAGING
from backend.kg.provenance import make_edge_id, make_node_id, utc_now_iso
from crawlers.cn.harvest_mooc_courses import is_relevant, query_variants, skill_keys, slug

REGION = "CN"
SRC_COURSE = "XUETANGX"
SRC_LINK = "LINK_CN_RULE"
API = "https://www.xuetangx.com/api/v1/lms/get_product_list/?page=1"
COURSE_URL = "https://www.xuetangx.com/course/{cid}"
LICENSE = "学堂在线公开课程信息（站点无 robots 限制）"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 voced-kg-research/0.1 (educational research)"
)
OUT = STAGING / "xuetangx_courses.json"
MOOC_OUT = STAGING / "mooc_courses.json"


def search(keyword: str, *, timeout: int = 25) -> list[dict]:
    body = json.dumps({
        "query": keyword, "chief_org": [], "classify": [],
        "selling_type": [], "status": [], "appid": 10000,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        API, data=body,
        headers={
            "User-Agent": UA,
            "Content-Type": "application/json",
            "django-language": "zh",
            "xtbz": "xt",  # 站点自定义头，缺了直接 4xx
            "Referer": f"https://www.xuetangx.com/search?query={urllib.parse.quote(keyword)}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    if not d.get("success"):
        raise RuntimeError(f"success=False msg={d.get('message')}")
    out = []
    for c in (d.get("data") or {}).get("product_list") or []:
        name = re.sub(r"</?em>", "", str(c.get("name") or "")).strip()
        if not name:
            continue
        out.append({
            "course_id": c.get("id") or c.get("course_id"),
            "sign": c.get("course_sign") or c.get("sign"),
            "name": name,
            "school": (c.get("org") or {}).get("name") if isinstance(c.get("org"), dict) else c.get("org_name"),
            "learners": c.get("count") or c.get("learner_count") or 0,
            "img": c.get("cover") or c.get("img"),
        })
    return out


def missing_skills(l1: str) -> list[str]:
    """MOOC 那轮没拿到真实课程的技能。"""
    keys = skill_keys(l1)
    if not MOOC_OUT.exists():
        return keys
    got = json.loads(MOOC_OUT.read_text(encoding="utf-8")).get("items", {})
    return [k for k in keys if not got.get(k)]


def stage_fetch(*, l1: str, only_missing: bool, limit: int | None,
                sleep: float, min_learners: int) -> dict:
    keys = missing_skills(l1) if only_missing else skill_keys(l1)
    if limit:
        keys = keys[:limit]
    STAGING.mkdir(parents=True, exist_ok=True)
    done: dict[str, list[dict]] = {}
    if OUT.exists():
        done = json.loads(OUT.read_text(encoding="utf-8")).get("items", {})

    todo = [k for k in keys if k not in done]
    failed, empty = [], []
    for i, key in enumerate(todo, 1):
        hits: list[dict] = []
        for q in query_variants(key):
            try:
                got = search(q)
            except Exception as e:
                failed.append({"skill": key, "query": q, "error": str(e)[:100]})
                time.sleep(max(sleep, 1.2))
                continue
            hits = [c for c in got
                    if (c["learners"] or 0) >= min_learners and is_relevant(key, q, c["name"])]
            if hits:
                for c in hits:
                    c["matched_query"] = q
                break
            time.sleep(sleep)
        done[key] = sorted(hits, key=lambda x: -(x["learners"] or 0))[:3] if hits else []
        if not hits:
            empty.append(key)
        if i % 20 == 0:
            OUT.write_text(json.dumps({"generated_at": utc_now_iso(), "min_learners": min_learners,
                                       "items": done}, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(sleep)

    OUT.write_text(json.dumps({"generated_at": utc_now_iso(), "min_learners": min_learners,
                               "items": done}, ensure_ascii=False, indent=2), encoding="utf-8")
    hit_n = sum(1 for v in done.values() if v)
    return {"stage": "fetch", "platform": "xuetangx", "l1": l1,
            "only_missing": only_missing, "skills_queried": len(keys),
            "with_course": hit_n, "no_course": len(keys) - hit_n,
            "min_learners": min_learners, "failed": failed[:8], "empty_sample": empty[:12],
            "sample": {k: [(c["name"], c["learners"]) for c in v]
                       for k, v in list(done.items())[:6] if v}}


def stage_apply(*, l1: str, dry_run: bool) -> dict:
    if not OUT.exists():
        return {"error": f"缺 {OUT}，先跑 --stage fetch"}
    data = json.loads(OUT.read_text(encoding="utf-8"))
    items = data["items"]
    fetched_at = utc_now_iso()

    conn = connect()
    try:
        nodes_by_key: dict[str, dict[int, str]] = {}
        for nid, name, attrs in conn.execute(
            "SELECT id, name, attrs FROM nodes WHERE type='skill_level'"
        ):
            try:
                a = json.loads(attrs or "{}")
            except Exception:
                a = {}
            k = a.get("skill_key") or str(name or "").split(" · ")[0]
            try:
                lv = int(a.get("level"))
            except (TypeError, ValueError):
                continue
            if k:
                nodes_by_key.setdefault(k, {})[lv] = nid

        new_nodes, edges, removed = {}, [], 0
        for key, courses in items.items():
            if not courses:
                continue
            levels = nodes_by_key.get(key) or {}
            anchor = levels.get(3) or (levels.get(max(levels)) if levels else None)
            if not anchor:
                continue
            for c in courses:
                ident = c.get("sign") or c.get("course_id")
                if not ident:
                    continue
                cid = make_node_id(REGION, "course", SRC_COURSE, str(ident))
                url = COURSE_URL.format(cid=ident)
                new_nodes.setdefault(cid, {
                    "id": cid, "region": REGION, "type": "course",
                    "name": c["name"], "name_en": None, "name_zh": c["name"],
                    "aliases": None,
                    "description": (f"{c['name']}"
                                    + (f"（{c['school']}）" if c.get("school") else "")
                                    + f" · 学堂在线 学习 {c['learners']} 人"),
                    "attrs": json.dumps({"role": "learnable_resource", "playable": True,
                                         "match_method": "xuetangx_search",
                                         "learner_count": c["learners"],
                                         "school": c.get("school"), "img_url": c.get("img"),
                                         "skill_key": key,
                                         "matched_query": c.get("matched_query")},
                                        ensure_ascii=False),
                    "source_system": SRC_COURSE, "source_id": str(ident),
                    "source_url": url, "license": LICENSE,
                    "fetched_at": fetched_at, "confidence": "official",
                })
                edges.append({
                    "id": make_edge_id(anchor, "taught_by", cid),
                    "src_id": anchor, "dst_id": cid, "rel_type": "taught_by",
                    "region": REGION, "weight": 0.9,
                    "evidence": (f"技能「{key}」→ 学堂在线《{c['name']}》"
                                 + (f"（{c['school']}）" if c.get("school") else "")
                                 + f"，学习 {c['learners']} 人（≥{data['min_learners']} 门槛）"),
                    "attrs": json.dumps({"match_method": "xuetangx_search", "skill_key": key,
                                         "learner_count": c["learners"]}, ensure_ascii=False),
                    "source_system": SRC_LINK, "source_id": f"{anchor}->{cid}",
                    "source_url": url, "license": LICENSE,
                    "fetched_at": fetched_at, "confidence": "official",
                })
            # 有真课就撤掉该技能的检索落地页边
            for lv, nid in levels.items():
                for (eid,) in conn.execute(
                    """SELECT e.id FROM edges e JOIN nodes c ON c.id=e.dst_id
                       WHERE e.src_id=? AND e.rel_type='taught_by'
                         AND c.source_system='SEARCH_LANDING_CN'""", (nid,)).fetchall():
                    if not dry_run:
                        conn.execute("DELETE FROM edges WHERE id=?", (eid,))
                    removed += 1

        rep = {"stage": "apply", "platform": "xuetangx", "l1": l1, "dry_run": dry_run,
               "skills_with_course": sum(1 for v in items.values() if v),
               "new_course_nodes": len(new_nodes), "taught_by_edges": len(edges),
               "search_landing_edges_removed": removed}
        if not dry_run:
            if new_nodes:
                rep["nodes_upserted"] = upsert_nodes(conn, list(new_nodes.values()))
            rep["edges_upserted"] = upsert_edges(conn, edges)
            conn.commit()
    finally:
        conn.close()
    return rep


def main() -> None:
    import urllib.parse  # noqa: F401  search() 里用到

    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=("fetch", "apply"))
    ap.add_argument("--l1", default="技术")
    ap.add_argument("--only-missing", action="store_true", help="只查 MOOC 没命中的技能")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--sleep", type=float, default=0.8)
    ap.add_argument("--min-learners", type=int, default=3000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rep = (stage_fetch(l1=args.l1, only_missing=args.only_missing, limit=args.limit,
                       sleep=args.sleep, min_learners=args.min_learners)
           if args.stage == "fetch" else stage_apply(l1=args.l1, dry_run=args.dry_run))
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"xuetangx_{args.stage}_{slug(args.l1)}.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=2)[:2200])


if __name__ == "__main__":
    main()
