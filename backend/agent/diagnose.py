"""
岗位技能诊断 Agent。

- 有 AI 网关：LangGraph `create_react_agent` + KG 工具
- 无网关 / 调用失败：规则降级（关键词）

产物：parsed_skills[{skill_name, level, score, evidence}] + 可选 summary
"""
from __future__ import annotations

import json
import re
from typing import Any

from backend.agent.llm import gateway_info, get_chat_model, llm_ready
from backend.agent.skill_keywords import kg_recall, rule_parse_skills
from backend.agent.tools_kg import kg_tools


def _kg_recall(text: str, limit: int = 12) -> list[dict[str, Any]]:
    """见 `agent.skill_keywords.kg_recall`（词表与兜底规则已收敛到那里）。"""
    return kg_recall(text, limit)


def _rule_parse(text: str) -> list[dict[str, Any]]:
    """规则兜底：先查技能库召回，再退化为粗粒度关键词。

    实现在 `agent.skill_keywords`——同一份词表此前在 `biz_store` 里还有一份拷贝，
    两份已经漂移（少了 C# / 汽车维修 / 航标作业），造成同一份简历走两条诊断路径
    结果不同。别再在别处复制这段。
    """
    return rule_parse_skills(text)


def _extract_json_array(text: str) -> list[dict[str, Any]] | None:
    if not text:
        return None
    text = text.strip()
    # fenced
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1).strip()
    # array slice
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        except json.JSONDecodeError:
            pass
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict) and isinstance(data.get("skills"), list):
            return [x for x in data["skills"] if isinstance(x, dict)]
    except json.JSONDecodeError:
        return None
    return None


def _normalize_skills(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for s in raw:
        name = (
            s.get("skill_name")
            or s.get("skill_key")
            or s.get("name")
            or ""
        ).strip()
        if not name:
            continue
        try:
            level = int(s.get("level") or s.get("required_level") or 2)
        except (TypeError, ValueError):
            level = 2
        level = max(1, min(5, level))
        try:
            score = int(s.get("score") or level * 20)
        except (TypeError, ValueError):
            score = level * 20
        out.append(
            {
                "skill_name": name,
                "level": level,
                "score": max(0, min(100, score)),
                "evidence": str(s.get("evidence") or s.get("reason") or "agent")[:500],
            }
        )
    return out or _rule_parse("")


def _run_react(user_prompt: str, system: str) -> tuple[list[dict[str, Any]], str]:
    """create_react_agent：模型可多轮调 KG 工具后输出技能 JSON。"""
    from langgraph.prebuilt import create_react_agent

    model = get_chat_model()
    tools = kg_tools()
    agent = create_react_agent(model, tools, prompt=system)
    result = agent.invoke(
        {"messages": [("user", user_prompt)]},
        config={"recursion_limit": 12},
    )
    messages = result.get("messages") or []
    # 取最后一条 AI 文本
    final_text = ""
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if content and isinstance(content, str) and content.strip():
            # 跳过纯 tool 痕迹
            if getattr(msg, "type", None) == "ai" or msg.__class__.__name__ in (
                "AIMessage",
                "AIMessageChunk",
            ):
                final_text = content
                break
        if isinstance(msg, dict) and msg.get("content"):
            final_text = str(msg["content"])
            break
    skills = _extract_json_array(final_text)
    if skills is None:
        # 再扫一遍全部消息
        for msg in reversed(messages):
            c = getattr(msg, "content", None) or (
                msg.get("content") if isinstance(msg, dict) else None
            )
            skills = _extract_json_array(str(c or ""))
            if skills:
                break
    if not skills:
        return _rule_parse(user_prompt), final_text or "agent 未产出 JSON，已规则降级"
    return _normalize_skills(skills), final_text


def run_resume_diagnose(
    content_text: str,
    *,
    target_occupation_id: str | None = None,
    target_occupation_name: str | None = None,
) -> dict[str, Any]:
    """简历诊断：有 AI 网关则 ReAct，否则规则。"""
    meta = {"engine": "rule", "gateway": gateway_info()}
    text = (content_text or "").strip()
    if not text:
        return {"skills": _rule_parse(""), "summary": "空简历", "meta": meta}

    if not llm_ready():
        return {
            "skills": _rule_parse(text),
            "summary": "AI 网关未配置，规则解析",
            "meta": meta,
        }

    occ_hint = target_occupation_name or target_occupation_id or "未指定"
    system = (
        "你是职业教育岗位技能诊断助手。可使用工具查询知识图谱中的岗位技能要求。"
        "根据简历文本与目标岗位，输出用户技能画像。"
        "最终回复必须是 JSON 数组，每项："
        '{"skill_name":"...", "level":1-5, "score":0-100, "evidence":"..."}。'
        "level 语义：1了解 2掌握 3熟练 4精通 5专家。不要输出其它说明文字。"
    )
    user = (
        f"目标岗位：{occ_hint}\n"
        f"occupation_id：{target_occupation_id or '无'}\n"
        f"简历原文：\n{text[:6000]}\n"
        "若有 occupation_id，请先 get_occupation_skills 对照岗位要求再评估。"
    )
    try:
        skills, raw = _run_react(user, system)
        meta["engine"] = "create_react_agent"
        meta["raw_preview"] = (raw or "")[:300]
        return {
            "skills": skills,
            "summary": f"AI 诊断完成，识别 {len(skills)} 项技能",
            "meta": meta,
        }
    except Exception as e:
        meta["engine"] = "rule_fallback"
        meta["error"] = str(e)[:300]
        return {
            "skills": _rule_parse(text),
            "summary": f"AI 调用失败，规则降级：{e}",
            "meta": meta,
        }


def run_chat_diagnose(
    user_message: str,
    *,
    target_occupation_id: str | None = None,
    target_occupation_name: str | None = None,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """对话诊断一轮：有网关则 ReAct 评估本轮回答。"""
    meta = {"engine": "rule", "gateway": gateway_info()}
    text = (user_message or "").strip()
    if not llm_ready():
        skills = _rule_parse(text)
        score = min(100, 40 + 10 * len(skills))
        return {
            "skills": skills,
            "score": score,
            "reply": f"（规则）记录 {len(skills)} 项技能线索，综合约 {score // 20} 分。",
            "meta": meta,
        }

    occ_hint = target_occupation_name or target_occupation_id or "目标岗位"
    system = (
        "你是职业技能测评对话助手。可调用知识图谱工具查看岗位技能要求。"
        "根据学员本轮回答评估技能，并给出简短中文反馈。"
        "最终一条消息必须包含两段：1) 一句中文反馈 2) 单独一行 JSON 数组 "
        '[{"skill_name","level","score","evidence"}]。'
    )
    hist = ""
    for h in (history or [])[-6:]:
        hist += f"{h.get('role','user')}: {h.get('content','')}\n"
    user = (
        f"岗位：{occ_hint} id={target_occupation_id or '无'}\n"
        f"历史：\n{hist}\n学员本轮：{text}\n"
        "可先查岗位技能再评估。"
    )
    try:
        skills, raw = _run_react(user, system)
        meta["engine"] = "create_react_agent"
        # 反馈：去掉 JSON 的前文
        reply = re.sub(r"\[[\s\S]*\]\s*$", "", raw or "").strip()
        if not reply:
            reply = f"已根据回答评估 {len(skills)} 项技能。"
        score = min(100, 40 + 12 * len(skills))
        return {
            "skills": skills,
            "score": score,
            "reply": reply[:800],
            "meta": meta,
        }
    except Exception as e:
        skills = _rule_parse(text)
        meta["engine"] = "rule_fallback"
        meta["error"] = str(e)[:300]
        return {
            "skills": skills,
            "score": min(100, 40 + 10 * len(skills)),
            "reply": f"AI 暂不可用（{e}），已用规则记录线索。",
            "meta": meta,
        }
