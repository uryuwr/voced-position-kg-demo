"""判分：把学员作答折算成实测档位。

选择题
------
选项本身就是行为锚档位，选中即定档，无需模型。

问答题
------
职责是**校验选择题自评是否虚高**，而不是重新定级。所以它只会「维持或下调」
同一技能在选择题里的自评档，不会往上加——一段漂亮的自述不足以证明更高水平。

- AI 网关可用：让模型按证据充分度打 0–100 分，映射为可支撑的档位
- 网关未配置：规则降级，看回答里有没有「具体证据」（数字/结果/方法词）与足够篇幅

网关状态由 llm_ready() 决定；内测期 LLM_BASE_URL 为空时全部走规则路径。
"""
from __future__ import annotations

import json
import re
from typing import Any

from backend.agent.llm import gateway_info, invoke_fast, llm_ready

# 规则降级用的信号词：说明回答里有方法与结果，而不只是「我会」
_METHOD_WORDS = (
    "方法", "流程", "步骤", "工具", "方案", "排查", "分析", "优化", "调试",
    "标准", "规范", "复盘", "验证", "测试", "定位", "改进",
)
_RESULT_WORDS = ("结果", "提升", "下降", "完成", "解决", "通过", "达成", "交付", "上线")
_NUM = re.compile(r"\d+(\.\d+)?\s*(%|％|次|台|人|天|小时|分钟|元|万|个|条|件)?")


def grade_choice(question: dict[str, Any], answer: Any) -> dict[str, Any]:
    """选择题：学员选中的选项自带 level（该选项体现的能力档位），选中即定档。

    answer 是选项编号 value，不是档位本身——SJT 的选项顺序不按档位排列（否则
    学员一眼看出「越靠后越高级」就会直接选最后一个）。
    """
    by_value = {
        int(o["value"]): o
        for o in (question.get("options") or [])
        if o.get("value") is not None
    }
    try:
        picked = by_value.get(int(answer))
    except (TypeError, ValueError):
        picked = None
    if not picked:
        return {
            "skill_key": question.get("skill_key"),
        "skill_name": question.get("skill_name") or question.get("skill_key"),
            "skill_name": question.get("skill_name") or question.get("skill_key"),
            "type": "choice",
            "level": None,
            "invalid": True,
            "message": f"无效选项：{answer}（可选 {sorted(by_value)}）",
        }
    return {
        "skill_key": question.get("skill_key"),
        "skill_name": question.get("skill_name") or question.get("skill_key"),
        "category": question.get("category"),
        "type": "choice",
        "level": int(picked.get("level") or 0) or None,
        "picked_value": picked.get("value"),
        "picked_text": str(picked.get("text") or "")[:200],
        "required_level": question.get("required_level"),
        "weight": question.get("weight"),
        # SJT 是考核题，自评题只是降级方案，报告里要能区分证据强度
        "source": "sjt" if question.get("variant") == "sjt" else "self_report",
    }


def _rule_evidence_score(text: str) -> tuple[int, list[str]]:
    """规则评估证据充分度，返回 (0-100, 命中的证据类型)。"""
    t = (text or "").strip()
    hits: list[str] = []
    score = 0
    if len(t) >= 40:
        score += 20
        hits.append("篇幅")
    if len(t) >= 80:
        score += 15
    if any(w in t for w in _METHOD_WORDS):
        score += 25
        hits.append("方法")
    if any(w in t for w in _RESULT_WORDS):
        score += 20
        hits.append("结果")
    if _NUM.search(t):
        score += 20
        hits.append("量化")
    return min(100, score), hits


def _level_ceiling(score: int) -> int:
    """证据分 → 该回答最多能支撑到第几档。"""
    if score >= 85:
        return 5
    if score >= 70:
        return 4
    if score >= 50:
        return 3
    if score >= 30:
        return 2
    return 1


def grade_open(
    question: dict[str, Any], answer: str, *, self_level: int | None
) -> dict[str, Any]:
    """问答题：按证据充分度给出档位上限，并对自评做「不上调」的收敛。"""
    text = (answer or "").strip()
    meta: dict[str, Any] = {"engine": "rule", "gateway": gateway_info()}
    met: list[str] = []

    if llm_ready():
        try:
            score, comment, met = _llm_score(question, text)
            meta["engine"] = "llm"
        except Exception as e:  # noqa: BLE001 — 判分失败不能中断测评
            score, hits = _rule_evidence_score(text)
            comment = f"AI 判分失败，规则降级（证据：{'、'.join(hits) or '不足'}）"
            meta["engine"] = "rule_fallback"
            meta["error"] = str(e)[:200]
    else:
        score, hits = _rule_evidence_score(text)
        comment = f"证据要素：{'、'.join(hits) or '不足'}"

    ceiling = _level_ceiling(score)
    # 只收敛不抬升：问答题用于验证，不用于加分
    final = min(self_level, ceiling) if self_level else ceiling
    return {
        "skill_key": question.get("skill_key"),
        "category": question.get("category"),
        "type": "open",
        "level": final,
        "self_level": self_level,
        "evidence_score": score,
        "capped": bool(self_level and ceiling < self_level),
        "comment": comment,
        "rubric_met": met,
        "rubric": question.get("rubric") or [],
        "required_level": question.get("required_level"),
        "weight": question.get("weight"),
        "source": "verified",
        "meta": meta,
    }


def _llm_score(question: dict[str, Any], text: str) -> tuple[int, str, list[str]]:
    """按出题时生成的 rubric 逐条判，比笼统的「证据充分度」更贴岗位要求。"""
    rubric = question.get("rubric") or ["有具体任务背景", "方法可复述", "结果可验证"]
    rubric_lines = "\n".join(f"{i+1}. {r}" for i, r in enumerate(rubric))
    system = (
        "你是职业技能测评评分员。逐条对照评分要点判断学员回答是否满足，"
        "再给出 0–100 总分。不要评估文采；回答里自称水平高但没有事实支撑的，不得给高分。\n"
        f"评分要点：\n{rubric_lines}\n"
        '只输出 JSON：{"score":0-100,"met":[要点序号,...],"comment":"一句中文评语"}'
    )
    user = (
        f"被测技能：{question.get('skill_key')}（岗位要求档 L{question.get('required_level') or '?'}）\n"
        f"题目：{question.get('prompt')}\n"
        f"学员回答：\n{text[:3000]}"
    )
    raw = invoke_fast([("system", system), ("user", user)], max_tokens=600)
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        raise ValueError(f"模型未返回 JSON：{str(raw)[:120]}")
    data = json.loads(m.group(0))
    score = max(0, min(100, int(data.get("score") or 0)))
    met_idx = [int(i) for i in (data.get("met") or []) if str(i).isdigit()]
    met = [rubric[i - 1] for i in met_idx if 1 <= i <= len(rubric)]
    return score, str(data.get("comment") or "")[:200], met


def merge_measured(
    graded: list[dict[str, Any]], profile_levels: dict[str, int] | None = None
) -> dict[str, dict[str, Any]]:
    """合并同一技能的多次作答与简历画像 → 每技能一个实测档。

    优先级：问答题校验结果 > 选择题自评 > 简历推断。
    问答题已经做过「不上调」收敛，所以它在时直接采信。
    """
    out: dict[str, dict[str, Any]] = {}
    for g in graded:
        key = g.get("skill_key")
        if not key or g.get("invalid") or not g.get("level"):
            continue
        prev = out.get(key)
        if prev is None or (g.get("type") == "open" and prev.get("type") != "open"):
            out[key] = dict(g)
    for key, lv in (profile_levels or {}).items():
        if key not in out and lv:
            out[key] = {
                "skill_key": key,
                "level": int(lv),
                "type": "profile",
                "source": "resume",
            }
    return out
