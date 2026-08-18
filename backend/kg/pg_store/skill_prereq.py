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


def prereq_map(
    conn, skill_keys: list[str], *, region: str | None = None
) -> dict[str, list[str]]:
    """批量取 `{技能 → 先修技能 key 列表}`，供「技能列表」类接口一次查完。

    **只按左端 `skill_key` 筛，不要求先修技能也落在传入集合内**——原型要展示的是
    「这个技能真正的前置」，哪怕那个前置不在本岗位的技能集里（学员照样得先会它）。
    这与技能图谱（`industry_graph`）的语义**不同**：那里画的是集合内的依赖连线，
    两端都必须在集内才有边可画。两处都对，别互相照搬。

    收 `conn` 而不自己 `connect()`：调用方普遍已持有连接，先修查询总是与技能列表
    查询同处一个请求，多开一条连接纯属浪费（本项目已因此栽过性能问题）。
    """
    keys = sorted({k for k in (skill_keys or []) if k})
    if not keys:
        return {}
    out: dict[str, list[str]] = {}
    for r in conn.execute(
        """
        SELECT skill_key, prereq_skill_key FROM kg_skill_prereq
        WHERE region = %s AND skill_key = ANY(%s)
        ORDER BY skill_key, prereq_skill_key
        """,
        (region or DEFAULT_REGION, keys),
    ).fetchall():
        out.setdefault(r["skill_key"], []).append(r["prereq_skill_key"])
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
