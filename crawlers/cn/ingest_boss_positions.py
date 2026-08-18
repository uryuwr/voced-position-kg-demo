"""BOSS 公开职位分类树 → occupation 节点（市场化岗位名）。

    GET https://www.zhipin.com/wapi/zpCommon/data/position.json

为什么需要它
------------
现有 occupation 全部来自《职业分类大典》（MOHRSS 1639 个），是**官方职业分类**；
而招聘市场用的是"Java""产品经理""算法工程师"这类**市场化岗位名**，库里一个都没有
（实测 BOSS 851 个岗位中仅 30 个与大典同名）。学员端按市场岗位检索时全部落空。

合规说明
--------
- `wapi/zpCommon/data/position.json` **不在 zhipin.com robots.txt 的 Disallow 列表内**
  （被禁的是 `*?position=*`、`*?city=*`、`/*?query=*` 等带查询串的搜索路径）。
- 只读一次分类树，不触碰职位搜索、不逆向 JS 挑战、不绕验证码。
- 岗位名本身是平台官方分类，故 confidence=official；但它**不带任何热度信息**
  （`rank` 字段实测恒为 0），热度另见 docs/热门岗位TOP100与方法论.md。

层级映射
--------
BOSS 三级树 = 一级门类（技术/产品/…21 个） > 二级方向（后端开发/…115 个） > 三级岗位（851 个）。
只有**三级**入库为 occupation；一级/二级作为 attrs.boss_l1 / boss_l2 保留，
供后续建 belongs_to（→industry）与 advances_to（同族晋升）时分组用。

⚠ 不建 belongs_to：BOSS 的 position（职位类别）与 industry（公司行业）是两个维度，
   岗位↔行业是 N:M，需单独的映射逻辑，见 link_boss_position_industry.py。

用法::

    python -m crawlers.cn.ingest_boss_positions --dry-run
    python -m crawlers.cn.ingest_boss_positions
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, stats, upsert_nodes
from backend.kg.paths import RAW, REPORTS, ensure_dirs
from backend.kg.provenance import make_node_id, utc_now_iso

API = "https://www.zhipin.com/wapi/zpCommon/data/position.json"
SOURCE_SYSTEM = "BOSS"
LICENSE = "公开数据接口（robots 未禁）"
REGION = "CN"
RAW_PATH = RAW / "CN" / "boss" / "boss_position_20260817.json"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 voced-kg-research/0.1 (educational research)"
)


def fetch_json() -> dict:
    import urllib.request

    req = urllib.request.Request(
        API, headers={"User-Agent": UA, "Referer": "https://www.zhipin.com/"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def flatten(tree: list[dict]) -> list[dict]:
    """展开三级树，只取三级岗位。"""
    out = []
    for l1 in tree:
        for l2 in l1.get("subLevelModelList") or []:
            for l3 in l2.get("subLevelModelList") or []:
                out.append(
                    {
                        "name": (l3.get("name") or "").strip(),
                        "code": l3.get("code"),
                        "l1": l1.get("name"),
                        "l1_code": l1.get("code"),
                        "l2": l2.get("name"),
                        "l2_code": l2.get("code"),
                    }
                )
    return [j for j in out if j["name"] and j["code"]]


def existing_names(conn) -> set[str]:
    cur = conn.execute("SELECT name FROM nodes WHERE type='occupation'")
    return {r[0] for r in cur.fetchall()}


def ingest(*, dry_run: bool = False, use_cache: bool = True) -> dict:
    ensure_dirs()
    RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
    fetched_at = utc_now_iso()

    if use_cache and RAW_PATH.exists():
        data = json.loads(RAW_PATH.read_text(encoding="utf-8"))
        cached = True
    else:
        data = fetch_json()
        RAW_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        cached = False

    report = {
        "source_system": SOURCE_SYSTEM,
        "api": API,
        "fetched_at": fetched_at,
        "from_cache": cached,
        "dry_run": dry_run,
        "api_code": data.get("code"),
    }
    if data.get("code") != 0:
        report["error"] = f"api code={data.get('code')} message={data.get('message')}"
        return report

    jobs = flatten(data.get("zpData") or [])
    report["boss_positions_total"] = len(jobs)

    conn = connect()
    try:
        have = existing_names(conn)
        nodes, dup = [], []
        for j in jobs:
            if j["name"] in have:
                dup.append(j["name"])
                continue
            nid = make_node_id(REGION, "occupation", SOURCE_SYSTEM, str(j["code"]))
            nodes.append(
                {
                    "id": nid,
                    "region": REGION,
                    "type": "occupation",
                    "name": j["name"],
                    "name_en": None,
                    "name_zh": j["name"],
                    "aliases": None,
                    "description": f"{j['l1']} > {j['l2']} > {j['name']}（BOSS 直聘职位分类）",
                    "attrs": json.dumps(
                        {
                            "boss_code": j["code"],
                            "boss_l1": j["l1"],
                            "boss_l1_code": j["l1_code"],
                            "boss_l2": j["l2"],
                            "boss_l2_code": j["l2_code"],
                            "name_kind": "market",  # 市场化岗位名，区别于大典官方名
                            "heat_status": "pending_calibration",
                        },
                        ensure_ascii=False,
                    ),
                    "source_system": SOURCE_SYSTEM,
                    "source_id": str(j["code"]),
                    "source_url": API,
                    "license": LICENSE,
                    "fetched_at": fetched_at,
                    "confidence": "official",
                }
            )

        nodes = list({n["id"]: n for n in nodes}.values())
        report["new_nodes"] = len(nodes)
        report["skipped_same_name"] = len(dup)
        report["skipped_sample"] = sorted(dup)[:20]
        by_l1: dict[str, int] = {}
        for j in jobs:
            if j["name"] not in have:
                by_l1[j["l1"]] = by_l1.get(j["l1"], 0) + 1
        report["new_by_l1"] = dict(sorted(by_l1.items(), key=lambda x: -x[1]))

        if not dry_run:
            report["upserted"] = upsert_nodes(conn, nodes)
            conn.commit()
            report["stats_after"] = stats(conn)
    finally:
        conn.close()

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"boss_positions_ingest_{'dryrun' if dry_run else 'applied'}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(out)
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refetch", action="store_true", help="忽略本地缓存，重新拉取")
    args = ap.parse_args()

    rep = ingest(dry_run=args.dry_run, use_cache=not args.refetch)
    print(json.dumps({k: v for k, v in rep.items() if k != "new_by_l1"}, ensure_ascii=False, indent=2)[:1200])
    if rep.get("new_by_l1"):
        print("\n新增岗位按一级门类：")
        for k, v in rep["new_by_l1"].items():
            print("  %-16s %d" % (k, v))


if __name__ == "__main__":
    main()
