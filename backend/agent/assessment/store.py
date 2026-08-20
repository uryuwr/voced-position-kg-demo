"""测评题目与作答的业务表读写。

会话状态从此落在 `biz_assessment_question` / `biz_assessment_answer` 两张表上，
不再依赖 LangGraph 的 checkpointer：

- 题目和答案是**要长期保存、可统计可复盘**的业务数据（哪道题区分度低、
  问答判分是不是普遍偏严、某技能全员薄弱），checkpointer 的序列化 blob 查不了
- 出题与答题因此可以完全解耦：题目先落库，学员什么时候答、答几道，
  都只是往 answer 表写行，不必唤醒任何图
- 刷新恢复、断点续答也变成普通查询
"""
from __future__ import annotations

import json
from typing import Any

from backend.kg.pg_store.client import connect

# 题目里属于「结构」的字段单独立列便于统计，其余（题干/选项/rubric）进 payload
_COLS = ("type", "variant", "skill_key", "category", "required_level", "weight")


def _to_row(q: dict[str, Any]) -> tuple:
    payload = {k: v for k, v in q.items() if k not in _COLS and k not in ("index", "session_id")}
    return (
        q.get("type") or "choice",
        q.get("variant"),
        q.get("skill_key"),
        q.get("category"),
        q.get("required_level"),
        q.get("weight"),
        json.dumps(payload, ensure_ascii=False),
    )


def _from_row(r: dict[str, Any]) -> dict[str, Any]:
    payload = r.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    return {
        **payload,
        "index": r["idx"],
        "type": r["type"],
        "variant": r["variant"],
        "skill_key": r["skill_key"],
        # skill_name 只在 payload 里（表没有这一列），所以**不能**在这里显式覆盖 ——
        # `**payload` 已经把它带进来了，这里再写一遍 None 就把它抹掉
        "category": r["category"],
        "required_level": r["required_level"],
        "weight": r["weight"],
    }


def _fill_names(qs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给读回来的题补展示名 —— **一次批量查，不做 N+1**。

    `skill_name` 只活在 `payload` 里（表没有这一列），而迁移前落库的题目 payload
    里根本没有这个字段，读回来就是 `skill_name=null`。落库快照改不动（那是当时冻结
    的事实），所以在读侧补：按 code 查一次名字表，查不到就退回 code —— 指向已删技能
    的历史题目仍要看得见，显示成一串 code 也比整行空白好排查。

    值本来就是中文名的老题（key 还没迁的那批）走 `display_name` 会原样返回，
    正好也是对的展示名。
    """
    miss = [str(q.get("skill_key") or "") for q in qs if not str(q.get("skill_name") or "").strip()]
    if not miss:
        return qs
    from backend.userprofile.skill_display import display_name, resolve_names

    m = resolve_names(miss)
    for q in qs:
        if not str(q.get("skill_name") or "").strip():
            q["skill_name"] = display_name(q.get("skill_key"), m) or q.get("skill_key")
    return qs


def save_questions(session_id: int, questions: list[dict[str, Any]], start_idx: int) -> int:
    """追加题目（幂等：同 (session, idx) 覆盖）。返回写入条数。"""
    if not questions:
        return 0
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO biz_assessment_question
              (session_id, idx, type, variant, skill_key, category, required_level, weight, payload)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT (session_id, idx) DO UPDATE SET
              type=EXCLUDED.type, variant=EXCLUDED.variant, skill_key=EXCLUDED.skill_key,
              category=EXCLUDED.category, required_level=EXCLUDED.required_level,
              weight=EXCLUDED.weight, payload=EXCLUDED.payload
            """,
            [(session_id, start_idx + i, *_to_row(q)) for i, q in enumerate(questions)],
        )
        conn.commit()
    return len(questions)


def list_questions(session_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM biz_assessment_question WHERE session_id=%s ORDER BY idx",
            (session_id,),
        ).fetchall()
    return _fill_names([_from_row(r) for r in rows])


def get_question(session_id: int, idx: int) -> dict[str, Any] | None:
    with connect() as conn:
        r = conn.execute(
            "SELECT * FROM biz_assessment_question WHERE session_id=%s AND idx=%s",
            (session_id, idx),
        ).fetchone()
    return _fill_names([_from_row(r)])[0] if r else None


def question_count(session_id: int) -> int:
    with connect() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) n FROM biz_assessment_question WHERE session_id=%s",
                (session_id,),
            ).fetchone()["n"]
        )


def save_answer(
    session_id: int,
    idx: int,
    raw_answer: Any,
    *,
    grade: dict[str, Any] | None = None,
    status: str = "pending",
) -> None:
    """记录作答；选择题当场判分（grade 直接给），问答题先 pending 后台补。"""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO biz_assessment_answer
              (session_id, idx, raw_answer, level, score, grade_status, grade_json, graded_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb, CASE WHEN %s='graded' THEN NOW() END)
            ON CONFLICT (session_id, idx) DO UPDATE SET
              raw_answer=EXCLUDED.raw_answer, level=EXCLUDED.level, score=EXCLUDED.score,
              grade_status=EXCLUDED.grade_status, grade_json=EXCLUDED.grade_json,
              answered_at=NOW(), graded_at=EXCLUDED.graded_at
            """,
            (
                session_id,
                idx,
                None if raw_answer is None else str(raw_answer),
                (grade or {}).get("level"),
                (grade or {}).get("evidence_score"),
                status,
                json.dumps(grade or {}, ensure_ascii=False),
                status,
            ),
        )
        conn.commit()


def update_grade(session_id: int, idx: int, grade: dict[str, Any], status: str = "graded") -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE biz_assessment_answer
               SET level=%s, score=%s, grade_status=%s, grade_json=%s::jsonb, graded_at=NOW()
             WHERE session_id=%s AND idx=%s
            """,
            (
                grade.get("level"),
                grade.get("evidence_score"),
                status,
                json.dumps(grade, ensure_ascii=False),
                session_id,
                idx,
            ),
        )
        conn.commit()


def list_answers(session_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM biz_assessment_answer WHERE session_id=%s ORDER BY idx",
            (session_id,),
        ).fetchall()
    out = []
    for r in rows:
        g = r.get("grade_json") or {}
        if isinstance(g, str):
            g = json.loads(g)
        out.append(
            {
                "index": r["idx"],
                "raw_answer": r["raw_answer"],
                "level": r["level"],
                "score": r["score"],
                "grade_status": r["grade_status"],
                **(g or {}),
            }
        )
    return out


def progress(session_id: int) -> dict[str, Any]:
    """答题进度：出了几题、答了几题、还有几道在判分。"""
    with connect() as conn:
        r = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM biz_assessment_question WHERE session_id=%s) AS asked,
              (SELECT COUNT(*) FROM biz_assessment_answer   WHERE session_id=%s) AS answered,
              (SELECT COUNT(*) FROM biz_assessment_answer
                WHERE session_id=%s AND grade_status='pending') AS grading
            """,
            (session_id, session_id, session_id),
        ).fetchone()
    return {
        "asked": int(r["asked"] or 0),
        "answered": int(r["answered"] or 0),
        "grading": int(r["grading"] or 0),
    }


def next_unanswered(session_id: int) -> dict[str, Any] | None:
    """下一道未作答的题（刷新恢复用）。"""
    with connect() as conn:
        r = conn.execute(
            """
            SELECT q.* FROM biz_assessment_question q
            LEFT JOIN biz_assessment_answer a
                   ON a.session_id = q.session_id AND a.idx = q.idx
            WHERE q.session_id=%s AND a.id IS NULL
            ORDER BY q.idx LIMIT 1
            """,
            (session_id,),
        ).fetchone()
    return _from_row(r) if r else None
