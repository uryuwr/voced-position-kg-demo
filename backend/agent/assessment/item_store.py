"""题库缓存。

出题一次要调模型十几秒；同一岗位的题对所有学员是通用的，没必要每次重出。
按 (occupation_id, skill_key, item_type, required_level) 存一份，命中即秒出卷。

命中条件要求「本次挑中的技能全部有缓存」——缺一项就整卷重出，避免同一份卷子里
一半 SJT 一半自评题、难度不齐。

失效：换模型或改了出题 prompt 时提升 SCHEMA_VERSION 即可自然绕过旧题。
"""
from __future__ import annotations

import json
from typing import Any

from backend.kg.pg_store.client import connect

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS biz_assessment_item (
  id             bigserial PRIMARY KEY,
  occupation_id  text NOT NULL,
  skill_key      text NOT NULL,
  item_type      text NOT NULL,           -- choice | open
  required_level int,
  schema_version int NOT NULL DEFAULT 1,
  payload        jsonb NOT NULL,
  model          text,
  created_at     timestamptz DEFAULT now(),
  UNIQUE (occupation_id, skill_key, item_type, schema_version)
);
CREATE INDEX IF NOT EXISTS idx_biz_assess_item_occ
  ON biz_assessment_item(occupation_id, schema_version);
"""


def ensure_item_schema() -> None:
    with connect() as conn:
        conn.execute(_DDL)
        conn.commit()


def load_choice_items(
    occupation_id: str, skill_keys: list[str]
) -> dict[str, dict[str, Any]]:
    """按技能取缓存的选择题（逐项命中即可，不要求整批齐全）。

    只缓存选择题：问答题要结合学员本次的选择来追问，换个人就该换问法。
    """
    if not skill_keys:
        return {}
    ensure_item_schema()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT skill_key, payload FROM biz_assessment_item
            WHERE occupation_id=%s AND schema_version=%s
              AND item_type='choice' AND skill_key = ANY(%s)
            """,
            (occupation_id, SCHEMA_VERSION, list(skill_keys)),
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        p = r["payload"]
        if isinstance(p, str):
            p = json.loads(p)
        out[r["skill_key"]] = p
    return out


def save_choice_items(occupation_id: str, questions: list[dict[str, Any]]) -> int:
    """只存模型产出的选择题（variant=sjt）；降级题与问答题不缓存。"""
    ensure_item_schema()
    rows = [
        q
        for q in questions
        if q.get("variant") == "sjt" and q.get("type") == "choice" and q.get("skill_key")
    ]
    if not rows:
        return 0
    from backend import settings

    with connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO biz_assessment_item
                  (occupation_id, skill_key, item_type, required_level,
                   schema_version, payload, model)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT (occupation_id, skill_key, item_type, schema_version)
                DO UPDATE SET payload = EXCLUDED.payload,
                              model = EXCLUDED.model,
                              created_at = NOW()
                """,
                [
                    (
                        occupation_id,
                        q["skill_key"],
                        q["type"],
                        q.get("required_level"),
                        SCHEMA_VERSION,
                        json.dumps(
                            {k: v for k, v in q.items() if k not in ("index", "total")},
                            ensure_ascii=False,
                        ),
                        settings.LLM_MODEL,
                    )
                    for q in rows
                ],
            )
        conn.commit()
    return len(rows)


def clear_items(occupation_id: str | None = None) -> int:
    """清题库（改了 prompt 想重出时用）。"""
    ensure_item_schema()
    with connect() as conn:
        if occupation_id:
            cur = conn.execute(
                "DELETE FROM biz_assessment_item WHERE occupation_id=%s", (occupation_id,)
            )
        else:
            cur = conn.execute("DELETE FROM biz_assessment_item")
        n = cur.rowcount
        conn.commit()
    return n
