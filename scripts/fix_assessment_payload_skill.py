"""补 `biz_assessment_item` / `biz_assessment_question` 的 **payload 内层** 技能标识。

## 为什么还需要这一刀

`scripts/migrate_skill_key_to_code.py` 把这两张表的 `skill_key` **列**刷成了 code，
但两张表还各有一个 `payload jsonb` —— 出题那一刻整个题目 dict 原样冻结进去，
里面**也带着一份 `skill_key`**。列刷了、JSON 没刷，于是：

- `biz_assessment_item`（题库缓存）：命中缓存时 `bank.generate_batch` 直接把 payload
  当题目吐给学员端，`skill_key` 是中文名、`skill_name` 压根没有。
  症状是学员端 `skill_name=null`，且前端按 `skill_key` 与技能构成对比时**静默失配**。
- `biz_assessment_question`（会话已出的题）：读路径 `store._from_row` 的 `skill_key`
  取的是列（对的），但 `skill_name` 只能从 payload 取，老题没有这个字段。

读侧已各自加了兜底（`bank._from_cache` 身份归请求、`store._fill_names` 批量补名），
这个脚本把**数据本身**也修干净：直连库或将来新写的消费者不该再被脏 payload 骗一次。

## 怎么定名字

按 `payload.skill_key` 的形态分两路：

- 已经是 code：按 code 查库拿 `skill_name`
- 还是中文名：那串中文本身就是展示名，按名字查库拿当前 code（**不用
  `derive_key` 反推** —— 技能若改过名，反推出的 code 与库里存的不是一个，
  见 `backend/kg/skill_key.py`）；查不到就只补名字、不动 key，宁可留着也不猜。

幂等，默认 dry-run：

    PYTHONPATH=. python -X utf8 scripts/fix_assessment_payload_skill.py            # 只看
    PYTHONPATH=. python -X utf8 scripts/fix_assessment_payload_skill.py --apply
"""

from __future__ import annotations

import json
import sys

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL as _KEY
from backend.kg.pg_store.skill_aggregate import SKILL_NAME_SQL as _NAME
from backend.kg.skill_key import is_valid_key

APPLY = "--apply" in sys.argv
TABLES = ("biz_assessment_item", "biz_assessment_question")


def _lookup(conn, names: set[str], codes: set[str]) -> tuple[dict[str, str], dict[str, str]]:
    """(名字→code, code→名字)，一次查库。"""
    name2key: dict[str, str] = {}
    key2name: dict[str, str] = {}
    if not names and not codes:
        return name2key, key2name
    for r in conn.execute(
        f"""
        SELECT DISTINCT ({_KEY}) AS k, ({_NAME}) AS nm
        FROM kg_node n
        WHERE n.type = 'skill_level' AND NOT n.is_draft
          AND (({_NAME}) = ANY(%s) OR ({_KEY}) = ANY(%s))
        """,
        (list(names), list(codes)),
    ).fetchall():
        if r["k"] and r["nm"]:
            name2key.setdefault(r["nm"], r["k"])
            key2name.setdefault(r["k"], r["nm"])
    return name2key, key2name


def run() -> int:
    total_bad = 0
    with connect() as conn:
        for tbl in TABLES:
            rows = conn.execute(
                f"SELECT id, skill_key AS col_key, payload FROM {tbl} ORDER BY id"
            ).fetchall()
            parsed = []
            for r in rows:
                p = r["payload"]
                if isinstance(p, str):
                    p = json.loads(p)
                if isinstance(p, dict):
                    parsed.append((r["id"], r["col_key"], p))

            names = {
                str(p.get("skill_key") or "").strip()
                for _, _, p in parsed
                if str(p.get("skill_key") or "").strip()
                and not is_valid_key(str(p.get("skill_key") or "").strip())
            }
            codes = {
                str(p.get("skill_key") or "").strip()
                for _, _, p in parsed
                if is_valid_key(str(p.get("skill_key") or "").strip())
            } | {str(c or "").strip() for _, c, _ in parsed if is_valid_key(str(c or "").strip())}
            name2key, key2name = _lookup(conn, names, codes)

            fixed, unresolved = [], []
            for rid, col_key, p in parsed:
                raw = str(p.get("skill_key") or "").strip()
                nm = str(p.get("skill_name") or "").strip()
                new_key, new_name = raw, nm
                if raw and not is_valid_key(raw):
                    # 老形态：key 位置上装的就是展示名
                    new_name = nm or raw
                    # 列已经迁过了，优先信列；列也脏才按名字查
                    new_key = (
                        col_key
                        if is_valid_key(str(col_key or "").strip())
                        else (name2key.get(raw) or raw)
                    )
                elif raw and (not nm or is_valid_key(nm)):
                    new_name = key2name.get(raw) or nm or raw
                if (new_key, new_name) != (raw, nm):
                    fixed.append((rid, raw, new_key, new_name))
                    if not is_valid_key(new_key):
                        unresolved.append(raw)

            print(f"\n== {tbl} ==  共 {len(parsed)} 行，需要修 {len(fixed)} 行")
            for rid, raw, k, n in fixed[:5]:
                print(f"   id={rid}  payload.skill_key {raw!r} → {k!r}   skill_name → {n!r}")
            if len(fixed) > 5:
                print(f"   …… 其余 {len(fixed) - 5} 行同形")
            if unresolved:
                print(f"   ! {len(unresolved)} 行按名字查不到技能，只补名字未换 key：{sorted(set(unresolved))[:5]}")
            total_bad += len(fixed)

            if APPLY and fixed:
                with conn.cursor() as cur:
                    cur.executemany(
                        f"UPDATE {tbl} SET payload = payload "
                        "|| jsonb_build_object('skill_key', %s::text, 'skill_name', %s::text) "
                        "WHERE id = %s",
                        [(k, n, rid) for rid, _, k, n in fixed],
                    )
                conn.commit()
                print(f"   已写入 {len(fixed)} 行")

    if not APPLY and total_bad:
        print(f"\n（dry-run）共 {total_bad} 行待修，加 --apply 落库")
    elif not total_bad:
        print("\nPASS 两张表的 payload 里技能标识都是当前形态")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
