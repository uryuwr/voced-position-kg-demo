"""导出图谱基准数据快照，供同步到预生产 / 生产。

产出（`data/baseline/`）::

    kg_baseline_<ver>.jsonl.gz        每行一条记录，含 kind 字段区分表
    kg_baseline_<ver>.manifest.json   行数 + sha256 + **覆盖率快照**

为什么是 jsonl 而不是 pg_dump
-----------------------------
pg_dump 带 schema、owner、序列，跨环境不通用（预生产库还与 bcs-ai-agent 共用）。
jsonl 可读、可 diff、进 git 后能回溯「这一版到底改了什么」，gz 后几 MB。

为什么 manifest 里要存覆盖率，而不只是行数
----------------------------------------
2026-08-18 那次 `attrs = EXCLUDED.attrs` 把归一化结果盖回旧形态，**行数一模一样**、
技能档位数字逐位退回、全程零报错。只校验行数抓不到这类错，必须比对语义指标
（可评分岗位数、各段覆盖率、分类分布）。导入端拿它做导入后自检。

导什么、不导什么
----------------
进：`kg_node` / `kg_edge` 中 **`is_draft=false` 且 `status='published'`** 的行
    + `kg_skill_category` + `kg_skill_prereq`
不进：
  - `is_draft = true` —— 别人编辑到一半的中间态，不是内容
  - `status = 'archived'` —— 逻辑删除，导入端只 upsert 不删，目标环境已有的不受影响
  - `status = 'draft' / 'disabled'` —— 基准只要发布态。**注意**：本地这批 1174 个
    draft 是真实采集数据（认证 470、专业 346、岗位 309），不是垃圾 ——
    它们不进快照意味着预生产也不会有，要么先在本地走发布流程，要么明确放弃
  - 全部 `biz_*` —— 用户运行时数据，各环境自己长
  - `kg_proposal` —— 审核队列，环境相关
  - 任何 fixture —— 由 `audit_baseline_quality.py --gate` 在导出前挡住

用法::

    python -X utf8 scripts/export_baseline.py                    # 版本号取当天日期
    python -X utf8 scripts/export_baseline.py --ver 20260820b
    python -X utf8 scripts/export_baseline.py --skip-gate        # 跳过质量闸门（不建议）
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import session

OUT_DIR = ROOT / "data" / "baseline"

# 基准 = **只有 published 的线上行**。两个谓词都不能省：
#   is_draft            主键是 (id, is_draft)，同一 id 有两行，不钉就把草稿也导出去
#   status='published'  不是「非 archived」而是「就是 published」——
#                       draft/disabled 也不进基准。写成 `<> 'archived'` 时导出
#                       33328 行（多出 draft 1174 + disabled 6），比 published 多一截
_LIVE_N = "COALESCE(is_draft, false) = false AND COALESCE(status,'published') = 'published'"
_LIVE_E = _LIVE_N

TABLES: list[tuple[str, str]] = [
    ("node", f"SELECT * FROM kg_node WHERE {_LIVE_N}"),
    ("edge", f"SELECT * FROM kg_edge WHERE {_LIVE_E}"),
    ("skill_category", "SELECT * FROM kg_skill_category WHERE COALESCE(status,'published') = 'published'"),
    ("skill_prereq", "SELECT * FROM kg_skill_prereq"),
]


def _json_default(o: object) -> object:
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return str(o)


def coverage_snapshot(cur) -> dict[str, object]:
    """语义指标。导入端比对这些数字，不只比行数。"""
    snap: dict[str, object] = {}
    cur.execute(
        f"""
        WITH occ AS (SELECT id FROM kg_node WHERE type='occupation' AND {_LIVE_N})
        SELECT count(*)::int total,
          count(*) FILTER (WHERE EXISTS(SELECT 1 FROM kg_edge e WHERE e.src_id=o.id
            AND e.rel_type='belongs_to' AND COALESCE(e.status,'published')='published'))::int ind,
          count(*) FILTER (WHERE EXISTS(SELECT 1 FROM kg_edge e WHERE e.dst_id=o.id
            AND e.rel_type='prepares_for' AND COALESCE(e.status,'published')='published'))::int maj,
          count(*) FILTER (WHERE EXISTS(SELECT 1 FROM kg_edge e WHERE e.src_id=o.id
            AND e.rel_type='requires' AND COALESCE(e.status,'published')='published'))::int sk,
          count(*) FILTER (WHERE EXISTS(SELECT 1 FROM kg_edge e WHERE e.src_id=o.id
            AND e.rel_type='advances_to' AND COALESCE(e.status,'published')='published'))::int adv
        FROM occ o
        """
    )
    snap["occupation_coverage"] = dict(cur.fetchone())
    # 技能档位分布：08-18 被盖回旧形态时，就是这组数字逐位退回的
    cur.execute(
        f"""SELECT (attrs::jsonb->>'level') lv, count(*)::int n FROM kg_node
            WHERE type='skill_level' AND {_LIVE_N} GROUP BY 1 ORDER BY 1"""
    )
    snap["skill_level_dist"] = {str(r["lv"]): r["n"] for r in cur.fetchall()}
    cur.execute(
        f"""SELECT COALESCE(NULLIF(category,''),'(空)') c, count(DISTINCT (attrs::jsonb->>'skill_key'))::int n
            FROM kg_node WHERE type='skill_level' AND {_LIVE_N} GROUP BY 1 ORDER BY 2 DESC"""
    )
    snap["skill_category_dist"] = {r["c"]: r["n"] for r in cur.fetchall()}
    cur.execute(
        f"SELECT type, count(*)::int n FROM kg_node WHERE {_LIVE_N} GROUP BY 1 ORDER BY 1"
    )
    snap["node_by_type"] = {r["type"]: r["n"] for r in cur.fetchall()}
    cur.execute(
        f"SELECT rel_type, count(*)::int n FROM kg_edge WHERE {_LIVE_E} GROUP BY 1 ORDER BY 1"
    )
    snap["edge_by_rel"] = {r["rel_type"]: r["n"] for r in cur.fetchall()}
    return snap


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ver", help="版本号，缺省用当天日期")
    ap.add_argument("--skip-gate", action="store_true",
                    help="跳过质量闸门。**不建议** —— 闸门就是防止 fixture 被发到生产")
    args = ap.parse_args()

    if not args.skip_gate:
        print("跑质量闸门 …")
        r = subprocess.run(
            [sys.executable, "-X", "utf8", "scripts/audit_baseline_quality.py",
             "--gate", "--sample", "0"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            print(r.stdout[-1200:])
            print("质量闸门未通过，拒绝导出。先跑 scripts/clean_baseline.py --apply")
            sys.exit(1)
        print("  闸门通过")

    # 版本号不用 datetime.now()：脚本要能在固定输入下产出可复现的文件名
    ver = args.ver or subprocess.run(
        ["git", "log", "-1", "--format=%cd", "--date=format:%Y%m%d"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.strip() or "unknown"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data_path = OUT_DIR / f"kg_baseline_{ver}.jsonl.gz"
    man_path = OUT_DIR / f"kg_baseline_{ver}.manifest.json"

    counts: dict[str, int] = {}
    h = hashlib.sha256()
    with session() as c, c.cursor() as cur, gzip.open(data_path, "wt", encoding="utf-8") as f:
        for kind, sql in TABLES:
            cur.execute(sql)
            n = 0
            for row in cur:
                d = dict(row)
                d.pop("is_draft", None)          # 导入端一律写 false，不搬这个字段
                line = json.dumps({"kind": kind, **d}, ensure_ascii=False,
                                  sort_keys=True, default=_json_default)
                f.write(line + "\n")
                h.update(line.encode("utf-8"))
                n += 1
            counts[kind] = n
            print("  %-16s %6d 行" % (kind, n))
        cov = coverage_snapshot(cur)

    man = {
        "version": ver,
        "counts": counts,
        "sha256_of_lines": h.hexdigest(),
        "coverage": cov,
        "note": (
            "导入后用 coverage 自检：行数一致但语义退回的情况真发生过"
            "（attrs = EXCLUDED.attrs 把归一化结果盖回旧形态，行数一模一样）"
        ),
    }
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")

    size_mb = data_path.stat().st_size / 1024 / 1024
    print()
    print("数据：%s  (%.1f MB)" % (data_path.name, size_mb))
    print("清单：%s" % man_path.name)
    print("覆盖率：%s" % json.dumps(cov["occupation_coverage"], ensure_ascii=False))


if __name__ == "__main__":
    main()
