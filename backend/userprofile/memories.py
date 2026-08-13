"""用户画像服务客户端：五维记忆查询（走 BTS 服务间鉴权）。

    POST {OPENQ_AI_MANAGER}/api/v1/user-profile/memories/search

五维：identity / context / preference / experience / activity。
匹配度计算主要吃三维：

- **experience** —— situation/action/reasoning/keyLearning，是最直接的能力证据
- **context**    —— 当前状态与目标（在学什么、想提升什么）
- **preference** —— 学习偏好与结论性倾向

`Userid` 头是**目标 UC User ID**，即前端 MAC token 经 UC 校验后返回的 user_id
（见 backend/uc/client.py）；BTS 本身只带应用身份，不含用户。
"""
from __future__ import annotations

from typing import Any

from backend import settings

SEARCH_PATH = "/api/v1/user-profile/memories/search"

# 与能力评估相关的维度及每维取多少条（上限 20/维）。
# 技能画像只吃这三维：experience 是最直接的能力证据，context/preference 提供当前状态与倾向。
DEFAULT_FACETS: tuple[tuple[str, int], ...] = (
    ("experience", 10),
    ("context", 8),
    ("preference", 5),
)

# 展示用：五维全量（identity/activity 对匹配度没用，但学员画像页要完整呈现）
ALL_FACETS: tuple[tuple[str, int], ...] = (
    ("identity", 10),
    ("context", 10),
    ("preference", 10),
    ("experience", 10),
    ("activity", 10),
)

FACET_LABELS: dict[str, str] = {
    "identity": "身份",
    "context": "情境",
    "preference": "偏好",
    "experience": "经验",
    "activity": "活动",
}


def facet_view(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """五维记忆 → 展示用结构：每维一组，带中文名、条数与逐条摘要。"""
    groups = {g.get("facet"): g for g in (payload.get("groups") or [])}
    out: list[dict[str, Any]] = []
    for facet, label in FACET_LABELS.items():
        g = groups.get(facet) or {}
        items = g.get("items") or []
        out.append({
            "facet": facet,
            "label": label,
            "count": len(items),
            "next_cursor": g.get("nextCursor"),
            "items": [
                {
                    "memory_id": it.get("memoryId"),
                    "title": it.get("title"),
                    "summary": it.get("summary"),
                    "details": it.get("details"),
                    "subtype": (it.get("facetSubtype") or {}).get("label"),
                    "tags": [*(it.get("memoryTags") or []), *(it.get("facetLabels") or [])],
                    "captured_at": it.get("capturedAt"),
                    "facet_details": it.get("facetDetails") or {},
                }
                for it in items
            ],
            # 卡片一行摘要：优先 title，回落 summary
            "digest": " | ".join(
                str(it.get("title") or it.get("summary") or "").strip()
                for it in items if (it.get("title") or it.get("summary"))
            ) or "暂无数据",
        })
    return out


def available() -> bool:
    from backend.bts import bts_client

    return bool(settings.OPENQ_AI_MANAGER) and bts_client().available()


def search_memories(
    uc_user_id: str,
    *,
    facets: tuple[tuple[str, int], ...] = DEFAULT_FACETS,
    query: str | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """查询五维记忆。返回原始 groups 结构；未配置时抛 RuntimeError 由调用方降级。"""
    from backend.bts import BtsClient

    if not settings.OPENQ_AI_MANAGER:
        raise RuntimeError("未配置 OPENQ_AI_MANAGER（用户画像服务地址）")
    client = BtsClient(endpoint=settings.OPENQ_AI_MANAGER, timeout=timeout)
    body: dict[str, Any] = {
        "facets": [{"facet": f, "limit": n} for f, n in facets],
    }
    if query:
        body["query"] = query
    return client.post(SEARCH_PATH, json=body, uc_user_id=str(uc_user_id))


def _facet_detail_text(fd: Any) -> str:
    """把 facetDetails 里有信息量的字段摊平成短句。"""
    if not isinstance(fd, dict):
        return ""
    parts: list[str] = []
    # 各维的专属字段（文档「常见 facetDetails」）
    for k in (
        "description", "situation", "action", "reasoning", "possibleOutcome",
        "keyLearning", "currentStatus", "conclusionDirectives", "suggestions",
        "role", "relationship", "notes", "feedback",
    ):
        v = fd.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
        elif isinstance(v, list):
            parts.extend(str(x).strip() for x in v if str(x).strip())
    for k in ("associatedObjects", "associatedSubjects"):
        for o in fd.get(k) or []:
            name = (o or {}).get("name") if isinstance(o, dict) else None
            if name:
                parts.append(str(name))
    return "；".join(parts)


def memories_to_text(payload: dict[str, Any], *, max_chars: int = 6000) -> tuple[str, int]:
    """五维记忆 → 一段「自述」文本，供技能画像解析使用。

    保留 title/summary/details/facetDetails 与标签：技能线索往往藏在
    facetDetails.action / keyLearning 里，只取 summary 会丢掉大量证据。
    """
    lines: list[str] = []
    count = 0
    for g in payload.get("groups") or []:
        facet = g.get("facet") or ""
        items = g.get("items") or []
        if not items:
            continue
        lines.append(f"【{facet}】")
        for it in items:
            count += 1
            seg = [
                str(it.get("title") or "").strip(),
                str(it.get("summary") or "").strip(),
                str(it.get("details") or "").strip(),
                _facet_detail_text(it.get("facetDetails")),
            ]
            tags = [
                *(it.get("memoryTags") or []),
                *(it.get("facetLabels") or []),
            ]
            if tags:
                seg.append("标签：" + "、".join(str(t) for t in tags[:10]))
            text = "；".join(x for x in seg if x)
            if text:
                lines.append(f"- {text}")
    out = "\n".join(lines)
    return (out[:max_chars], count)
