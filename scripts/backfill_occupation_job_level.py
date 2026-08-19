"""从采集产物回填岗位的职级（`attrs.level`）与岗位族（`attrs.job_family`）。

问题
----
`link_boss_skill_chain --stage collect` 让 LLM 判定了每个岗位的 `job_level`
（1 入门 … 5 总监以上），写在 `data/staging/boss_skill_raw_<门类>.json` 里。
`stage_apply` 本该把它回写进节点 `attrs`，但库里有 **546 个 BOSS 岗位连
`level` 这个 key 都没有** —— 采集端判定了、节点上却查不到。

后果是连锁的，而且都不报错：

- `stage_advance` 的「from 职级必须严格低于 to」这道校验对它们形同虚设，
  于是出现「药物合成 → 分公司/代表处负责人」这种跨 3 级的晋升边
- 岗位详情页职级列空着，`progression.py` 多跳展开排不了序
- 学员端「下一级成长目标」按职级取最近一级，取不到

只补不覆盖
----------
只给**缺 `level`** 的节点补值。已经有值的一律不动 —— 那可能是人工在管理台调过的，
拿 LLM 的旧判定盖掉就是把人的决定抹了。同一岗位出现在多个门类产物里时，
取第一个非空值（各门类的 collect 是独立的，判定不一定一致，但都来自同一套提示词，
差异不大；真要精确得人工核）。

两边都写
--------
SQLite 是采集库、PG 是运行库。只改 PG 的话，下次 `migrate` 会用 SQLite 的
`attrs` 原样覆盖回去（`attrs = EXCLUDED.attrs`），回填白做 —— 技能档位回填
就是这么被抹掉过一次的。

用法::

    python -X utf8 scripts/backfill_occupation_job_level.py --dry-run
    python -X utf8 scripts/backfill_occupation_job_level.py
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import session

SQLITE = ROOT / "data" / "graph" / "kg.sqlite"
STAGING = ROOT / "data" / "staging"


def load_raw_levels() -> dict[str, dict[str, object]]:
    """从全部 collect 产物取 id → {level, job_family}，取第一个非空。"""
    out: dict[str, dict[str, object]] = {}
    for f in sorted(glob.glob(str(STAGING / "boss_skill_raw_*.json"))):
        try:
            data = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        for it in data.get("items") or []:
            oid = it.get("id")
            lv = it.get("job_level")
            if not oid or lv in (None, ""):
                continue
            try:
                lv = max(1, min(5, int(lv)))
            except (TypeError, ValueError):
                continue
            if oid not in out:
                out[oid] = {"level": lv, "job_family": it.get("job_family")}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    raw = load_raw_levels()
    print("采集产物里带职级的岗位：%d" % len(raw))

    # PG：找出缺 level 的
    with session() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, attrs FROM kg_node
            WHERE type = 'occupation' AND source_system = 'BOSS'
              -- 括号不能省：AND 优先级高于 OR，写成
              -- `type=.. AND src=.. AND 无key OR level为空` 会被解析成
              -- `(前三个) OR (level为空)`，把技能、课程等所有类型都捞进来（实测 20404 行）
              AND (
                    (attrs::jsonb ? 'level') IS NOT TRUE
                 OR (attrs::jsonb->>'level') IS NULL
              )
            """
        )
        missing = [dict(r) for r in cur.fetchall()]
    print("库内缺 attrs.level 的 BOSS 岗位：%d" % len(missing))

    fixable = [m for m in missing if m["id"] in raw]
    print("其中采集产物里有职级、可回填的：%d" % len(fixable))
    dist = collections.Counter(raw[m["id"]]["level"] for m in fixable)
    print("   回填后的职级分布：%s" % dict(sorted(dist.items())))
    still = len(missing) - len(fixable)
    if still:
        print("   仍无从得知职级的：%d（这些岗位没跑过 collect）" % still)

    if args.dry_run or not fixable:
        print("\n(dry-run，未写入)" if args.dry_run else "\n无可回填项")
        return

    # PG
    with session() as c, c.cursor() as cur:
        n = 0
        for m in fixable:
            a = m["attrs"]
            if isinstance(a, str):
                try:
                    a = json.loads(a or "{}")
                except Exception:
                    a = {}
            a = a or {}
            a["level"] = raw[m["id"]]["level"]
            if not a.get("job_family") and raw[m["id"]].get("job_family"):
                a["job_family"] = raw[m["id"]]["job_family"]
            cur.execute(
                "UPDATE kg_node SET attrs = %s, level = %s WHERE id = %s",
                (json.dumps(a, ensure_ascii=False), raw[m["id"]]["level"], m["id"]),
            )
            n += 1
        print("PG 已回填：%d" % n)

    # SQLite（采集库没有 level 列，只写 attrs）
    if SQLITE.exists():
        s = sqlite3.connect(SQLITE)
        s.row_factory = sqlite3.Row
        n = 0
        for m in fixable:
            row = s.execute("SELECT attrs FROM nodes WHERE id=?", (m["id"],)).fetchone()
            if not row:
                continue
            try:
                a = json.loads(row["attrs"] or "{}")
            except Exception:
                a = {}
            if a.get("level") not in (None, ""):
                continue
            a["level"] = raw[m["id"]]["level"]
            if not a.get("job_family") and raw[m["id"]].get("job_family"):
                a["job_family"] = raw[m["id"]]["job_family"]
            s.execute("UPDATE nodes SET attrs=? WHERE id=?",
                      (json.dumps(a, ensure_ascii=False), m["id"]))
            n += 1
        s.commit()
        s.close()
        print("SQLite 已回填：%d" % n)


if __name__ == "__main__":
    main()
