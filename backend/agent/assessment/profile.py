"""简历/自述 → 初始技能画像（工作流第一步）。

为什么不用 create_react_agent
-----------------------------
ReAct 会让模型自行决定调几轮工具，实测一次解析要几十秒；而它作为工作流的**第一步**，
学员要干等这段时间才能看到第一题。这里的分工是确定的，不需要模型规划：

1. **召回**用知识图谱：文本里直接出现的 `skill_key` 即命中（一条 SQL，约 0.2s，且
   召回的技能名一定与库内口径一致，后续能直接和岗位要求对齐）
2. **定级**用一次 LLM 调用：给候选技能按行为锚判档，并补充文本里体现但未命中库表的技能

单次调用约 3–5s；网关不可用时退回「命中即 L2」的保守规则，流程照常继续。

注意：这一步的产出只是**推断值**，优先级低于后面测评的实测值（见 grading.merge_measured）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from backend.agent.llm import gateway_info, invoke_fast, llm_ready
from backend.kg.pg_store.skill_level_meta import behavior_map

RECALL_LIMIT = 20


def recall_skills(text: str, limit: int = RECALL_LIMIT) -> list[dict[str, Any]]:
    """库内技能名在文本中出现即命中；长名优先，避免「设备维护」重复计入其父串。"""
    t = (text or "").strip()
    if not t:
        return []
    from backend.kg.pg_store.client import connect
    from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL

    try:
        with connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT k, cat FROM (
                  SELECT ({SKILL_KEY_SQL}) AS k, n.category AS cat
                  FROM kg_node n
                  WHERE n.type='skill_level'
                    AND COALESCE(n.status,'published') = 'published'
                ) t
                WHERE length(k) >= 2 AND position(k in %s) > 0
                LIMIT %s
                """,
                (t, limit * 3),
            ).fetchall()
    except Exception:
        return []

    ranked = sorted(({"k": r["k"], "cat": r["cat"]} for r in rows), key=lambda x: -len(x["k"]))
    out: list[dict[str, Any]] = []
    taken: list[str] = []
    for h in ranked:
        if any(h["k"] in prev for prev in taken):
            continue
        taken.append(h["k"])
        out.append({"skill_name": h["k"], "category": h["cat"]})
        if len(out) >= limit:
            break
    return out


def _llm_rate(text: str, candidates: list[dict[str, Any]], occ_name: str | None) -> list[dict]:
    """一次调用：给候选技能定档，并补充未命中的技能。"""
    anchors = "\n".join(f"L{k}={v}" for k, v in behavior_map().items())
    system = (
        "你是职业技能画像分析员。依据简历原文，为每项技能判定档位 1–5。\n"
        f"档位行为锚：\n{anchors}\n"
        "规则：原文没有证据支撑的技能不要给高档；只写原文能支撑的技能。\n"
        '只输出 JSON 数组：[{"skill_name":"...","level":1-5,"score":0-100,"evidence":"原文依据"}]'
    )
    cand_names = {c["skill_name"] for c in candidates}
    cand = "、".join(c["skill_name"] for c in candidates) or "（无）"
    user = (
        f"目标岗位：{occ_name or '未指定'}\n"
        f"知识图谱已召回的候选技能（优先使用这些标准名称）：{cand}\n"
        f"简历原文：\n{text[:5000]}"
    )
    raw = invoke_fast([("system", system), ("user", user)], max_tokens=1500)
    m = re.search(r"\[[\s\S]*\]", raw)
    if not m:
        raise ValueError(f"模型未返回 JSON 数组：{str(raw)[:120]}")
    data = json.loads(m.group(0))
    out = []
    for d in data if isinstance(data, list) else []:
        name = str(d.get("skill_name") or "").strip()
        if not name:
            continue
        try:
            lv = max(1, min(5, int(d.get("level") or 1)))
        except (TypeError, ValueError):
            lv = 1
        # 过滤碎片名：模型有时会把「维修」「诊断」这种子串当技能，
        # 它们既不在图谱口径里，也会污染画像。不在候选表中的短名一律丢弃。
        if len(name) < 3 and name not in cand_names:
            continue
        out.append(
            {
                "skill_name": name,
                "level": lv,
                "score": int(d.get("score") or lv * 20),
                "evidence": str(d.get("evidence") or "")[:200],
            }
        )
    return out


def build_profile(
    text: str, *, occupation_name: str | None = None
) -> tuple[dict[str, int], dict[str, Any]]:
    """→ ({skill_name: level}, meta)。"""
    meta: dict[str, Any] = {"engine": "rule", "gateway": gateway_info()}
    t = (text or "").strip()
    if not t:
        return {}, {"engine": "skip"}

    cands = recall_skills(t)
    meta["recalled"] = len(cands)

    if llm_ready():
        try:
            rated = _llm_rate(t, cands, occupation_name)
            meta["engine"] = "llm_rate"
            meta["rated"] = len(rated)
            return {r["skill_name"]: r["level"] for r in rated}, meta
        except Exception as e:  # noqa: BLE001 — 解析失败不应中断测评
            meta["engine"] = "rule_fallback"
            meta["error"] = str(e)[:200]

    # 保守规则：命中即「掌握」，真实水平交给后面的测评实测
    return {c["skill_name"]: 2 for c in cands}, meta
