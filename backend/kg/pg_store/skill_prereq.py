"""逻辑技能先修：kg_skill_prereq + 无环校验。"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.config import DEFAULT_REGION


def list_prereqs(skill_key: str, *, region: str | None = None) -> list[dict[str, Any]]:
    reg = region or DEFAULT_REGION
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT skill_key, prereq_skill_key, region, evidence, confidence, created_at
            FROM kg_skill_prereq
            WHERE region = %s AND skill_key = %s
            ORDER BY prereq_skill_key
            """,
            (reg, skill_key),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if hasattr(d.get("created_at"), "isoformat"):
            d["created_at"] = d["created_at"].isoformat()
        out.append(d)
    return out


def _would_cycle(
    skill_key: str, prereq_skill_key: str, *, region: str
) -> bool:
    """若 skill → prereq，再从 prereq 能否走到 skill。"""
    if skill_key == prereq_skill_key:
        return True
    with connect() as conn:
        rows = conn.execute(
            "SELECT skill_key, prereq_skill_key FROM kg_skill_prereq WHERE region=%s",
            (region,),
        ).fetchall()
    graph: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        graph[r["skill_key"]].append(r["prereq_skill_key"])
    graph[skill_key].append(prereq_skill_key)
    # BFS from prereq along edges skill→prereq meaning "depends on"
    # Cycle if we can reach skill_key starting from prereq following deps
    q = deque([prereq_skill_key])
    seen = {prereq_skill_key}
    while q:
        cur = q.popleft()
        for nxt in graph.get(cur, []):
            if nxt == skill_key:
                return True
            if nxt not in seen:
                seen.add(nxt)
                q.append(nxt)
    return False


def add_prereq(
    skill_key: str,
    prereq_skill_key: str,
    *,
    region: str | None = None,
    evidence: str | None = None,
    confidence: str = "manual_seed",
    created_by: str | None = None,
) -> dict[str, Any]:
    reg = region or DEFAULT_REGION
    sk = (skill_key or "").strip()
    pk = (prereq_skill_key or "").strip()
    if not sk or not pk:
        raise ValueError("skill_key 与 prereq_skill_key 必填")
    if sk == pk:
        raise ValueError("不能以自身为先修")
    if _would_cycle(sk, pk, region=reg):
        raise ValueError(f"添加先修会成环: {sk} → {pk}")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO kg_skill_prereq
              (skill_key, prereq_skill_key, region, evidence, confidence, created_by)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (region, skill_key, prereq_skill_key) DO UPDATE SET
              evidence = EXCLUDED.evidence,
              confidence = EXCLUDED.confidence
            """,
            (sk, pk, reg, evidence, confidence, created_by),
        )
        conn.commit()
    return {
        "skill_key": sk,
        "prereq_skill_key": pk,
        "region": reg,
        "evidence": evidence,
        "confidence": confidence,
    }


def remove_prereq(
    skill_key: str, prereq_skill_key: str, *, region: str | None = None
) -> bool:
    reg = region or DEFAULT_REGION
    with connect() as conn:
        cur = conn.execute(
            """
            DELETE FROM kg_skill_prereq
            WHERE region=%s AND skill_key=%s AND prereq_skill_key=%s
            """,
            (reg, skill_key, prereq_skill_key),
        )
        conn.commit()
        return cur.rowcount > 0


def set_prereqs(
    skill_key: str,
    prereq_keys: list[str],
    *,
    region: str | None = None,
    created_by: str | None = None,
) -> list[dict[str, Any]]:
    """整体替换某技能的先修列表（逐条无环校验）。"""
    reg = region or DEFAULT_REGION
    sk = skill_key.strip()
    with connect() as conn:
        conn.execute(
            "DELETE FROM kg_skill_prereq WHERE region=%s AND skill_key=%s",
            (reg, sk),
        )
        conn.commit()
    out = []
    for pk in prereq_keys:
        out.append(
            add_prereq(
                sk, pk, region=reg, created_by=created_by, confidence="manual_seed"
            )
        )
    return out
