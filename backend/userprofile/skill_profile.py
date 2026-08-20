"""用户技能画像：测评实测 + 五维记忆推断，合并成一份 {skill_key: level}。

优先级
------
**测评实测 > 记忆推断**。实测是学员在情景判断题里做出的选择、经问答题验证过的档位；
记忆是从自然语言里推断的，证据强度不同。同一技能两者都有时取实测，记忆只补空缺——
这样既不让「只测了 4 项」把其余全算 0，也不让文本推断盖过真实作答。

缓存
----
记忆→画像要调一次模型（几秒），而画像与岗位无关：一整页岗位共用一份，翻页也不必重算。
故按 user_id 进程内缓存 + TTL。记忆不会分钟级变化，TTL 取 10 分钟足够。

降级
----
记忆服务不可用 / 未配 BTS / 未配模型时逐级回落，最差就是只用测评画像（即改造前的行为）。
"""
from __future__ import annotations

import threading
import time
from typing import Any

_TTL_SECONDS = 600
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LOCK = threading.Lock()


def _cached(user_id: str) -> dict[str, Any] | None:
    with _LOCK:
        hit = _CACHE.get(user_id)
    if not hit:
        return None
    ts, data = hit
    if time.time() - ts > _TTL_SECONDS:
        with _LOCK:
            _CACHE.pop(user_id, None)
        return None
    return data


def _store(user_id: str, data: dict[str, Any]) -> None:
    with _LOCK:
        _CACHE[user_id] = (time.time(), data)


def invalidate(user_id: str | None = None) -> None:
    """画像变更后（如刚做完测评）清缓存。"""
    with _LOCK:
        if user_id:
            _CACHE.pop(user_id, None)
        else:
            _CACHE.clear()


def assessment_levels(user_id: str) -> dict[str, int]:
    """测评/诊断沉淀的技能画像（biz_user_skill）。"""
    from backend.kg.pg_store.client import connect

    with connect() as conn:
        rows = conn.execute(
            "SELECT skill_name, level FROM biz_user_skill WHERE user_id=%s", (user_id,)
        ).fetchall()
        from backend.userprofile.skill_display import profile_levels

        # 同 biz_store 那处：code 与名字都建键，两条匹配路径都要喂到
        return profile_levels([dict(r) for r in rows], conn=conn)


def memory_levels(user_id: str) -> tuple[dict[str, int], dict[str, Any]]:
    """五维记忆 → 技能画像（图谱召回对齐口径 + 一次模型定级）。"""
    from backend.agent.assessment.profile import build_profile
    from backend.userprofile import memories as mem

    meta: dict[str, Any] = {"engine": "none"}
    if not mem.available():
        meta["error"] = "用户画像服务未配置（OPENQ_AI_MANAGER / BTS）"
        return {}, meta
    try:
        payload = mem.search_memories(user_id)
    except Exception as e:  # noqa: BLE001 — 外部服务不可用不该影响列表展示
        meta["error"] = f"记忆查询失败：{str(e)[:160]}"
        return {}, meta

    text, n = mem.memories_to_text(payload)
    meta["memory_count"] = n
    if not text.strip():
        meta["engine"] = "empty"
        return {}, meta
    try:
        levels, pmeta = build_profile(text)
        meta.update(pmeta)
        meta["engine"] = pmeta.get("engine") or "rule"
        return levels, meta
    except Exception as e:  # noqa: BLE001
        meta["error"] = f"画像解析失败：{str(e)[:160]}"
        return {}, meta


def diagnosed_match(user_id: str, occupation_id: str) -> dict[str, Any] | None:
    """该用户针对**这个岗位**最近一次诊断报告里的匹配度。

    做过诊断就直接用报告里的数——它是学员实际作答算出来的，比任何实时推断都准，
    也省掉一次模型调用。级联的第一优先级。
    """
    from backend.kg.pg_store.client import connect

    with connect() as conn:
        r = conn.execute(
            """
            SELECT r.match_score, r.created_at, s.id AS session_id, s.channel
            FROM biz_diagnosis_result r
            JOIN biz_diagnosis_session s ON s.id = r.session_id
            WHERE s.user_id = %s AND s.target_occupation_id = %s
              AND r.match_score IS NOT NULL
            ORDER BY r.created_at DESC LIMIT 1
            """,
            (user_id, occupation_id),
        ).fetchone()
    if not r:
        return None
    return {
        "match_score": float(r["match_score"]),
        "session_id": r["session_id"],
        "channel": r["channel"],
        "diagnosed_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


def get_profile(user_id: str, *, use_cache: bool = True) -> dict[str, Any]:
    """合并画像。

    返回 {levels, source, assessment_count, memory_count, meta}
    source ∈ assessment（仅实测）/ memory（仅记忆）/ mixed（两者）/ none。
    """
    if use_cache:
        hit = _cached(user_id)
        if hit:
            return hit

    a = assessment_levels(user_id)
    m, meta = memory_levels(user_id)

    # 实测优先，记忆只补实测没覆盖到的技能
    levels = dict(m)
    levels.update(a)

    if a and m:
        source = "mixed"
    elif a:
        source = "assessment"
    elif m:
        source = "memory"
    else:
        source = "none"

    data = {
        "levels": levels,
        "source": source,
        "assessment_count": len(a),
        "memory_count": len(m),
        "meta": meta,
    }
    _store(user_id, data)
    return data
