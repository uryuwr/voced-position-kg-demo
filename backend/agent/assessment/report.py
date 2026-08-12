"""综合能力报告：实测档位 × 岗位标准要求 → 匹配度 / 双系列雷达 / 优势 / 短板。

对齐原型「AI 智能能力诊断报告」
------------------------------
- 右上角 **综合能力匹配度**            → match_score
- **能力维度与目标极域图**（双层雷达）  → radar.series 两条：学员实测能力 / 岗位标准要求
- **优势能力领域**（已达到或超越基准）  → strengths[]，展示「技能名 L4」
- **关键能力短板**（需优先攻关提升）    → gaps[]，未测到的显示「待补强」，测到但不足的显示实际档
- **基于短板一键生成学习计划**          → next_action 里带上短板技能，供学习计划接口直接消费

匹配度算法沿用 position_match 的加权达标率（单项 min(实测/要求,1)，按国标权重加权），
保证「岗位探索列表的匹配度」与「诊断报告的匹配度」同源，不会出现两个数字。
"""
from __future__ import annotations

from typing import Any

from backend.kg.pg_store.skill_level_meta import base_score, label_map


def _ratio(measured: int, required: int) -> float:
    if not required:
        return 1.0 if measured else 0.0
    return min(measured / required, 1.0) if measured else 0.0


def build_report(
    *,
    occupation: dict[str, Any] | None,
    required_items: list[dict[str, Any]],
    measured: dict[str, dict[str, Any]],
    channel: str = "assessment",
) -> dict[str, Any]:
    """required_items 来自岗位技能构成；measured 来自 grading.merge_measured。"""
    labels = label_map()
    rows: list[dict[str, Any]] = []
    total_w = 0.0
    got_w = 0.0

    for it in required_items:
        key = it.get("skill_key")
        req = int(it.get("required_level") or 0)
        w = float(it.get("weight") or 0)
        m = measured.get(key) or {}
        lv = int(m.get("level") or 0)
        r = _ratio(lv, req)
        total_w += w
        got_w += r * w
        rows.append(
            {
                "skill_key": key,
                "category": it.get("category") or "未分类",
                "required_level": req or None,
                "required_label": labels.get(req),
                "measured_level": lv or None,
                "measured_label": labels.get(lv),
                "weight": w,
                "weight_pct": round(w * 100) if w else None,
                "ratio": round(r, 3),
                "ok": bool(lv and req and lv >= req),
                "tested": bool(m),
                "source": m.get("source"),
                "evidence_score": m.get("evidence_score"),
                "capped": bool(m.get("capped")),
                # 短板紧迫度：权重越高、差距越大越该先补
                "urgency": round(w * max(0, (req - lv)), 4) if req else 0.0,
            }
        )

    # 匹配度只算**测过**的技能：一次测评覆盖 6–10 项核心技能，若把没考到的
    # 二十多项也按 0 分计入，分数会被稀释成一个既不反映能力、也无法改善的数字
    # （学员补再多短板，只要没被抽中出题就仍是 0）。未覆盖部分单独用 coverage 表达。
    tested_rows = [r for r in rows if r["tested"]]
    t_total = sum(r["weight"] for r in tested_rows) or 0.0
    t_got = sum(r["ratio"] * r["weight"] for r in tested_rows)
    match_score = round(100 * t_got / t_total, 1) if t_total else 0.0
    coverage = round(100 * t_total / total_w, 1) if total_w else 0.0

    # 优势/短板都只在测过的技能里选——没考过就没有证据，不该下结论
    strengths = [r for r in tested_rows if r["ok"]]
    # 超越幅度大的优先，其次看权重：L1 要求达 L1 也算达标，但不该排在
    # 「要求 L3 实测 L5」前面
    strengths.sort(
        key=lambda x: (
            -((x["measured_level"] or 0) - (x["required_level"] or 0)),
            -(x["weight"] or 0),
        )
    )
    gaps = [r for r in tested_rows if not r["ok"]]
    gaps.sort(key=lambda x: -x["urgency"])
    untested = sorted(
        (r for r in rows if not r["tested"]), key=lambda x: -(x["weight"] or 0)
    )

    # 雷达：按技能大类聚合成轴，两条系列同轴对比（原型的绿/紫双层）。
    # 同样只用测过的维度，否则会出现一条恒为 0 的轴。
    axes: list[str] = []
    acc: dict[str, dict[str, list[float]]] = {}
    for r in tested_rows:
        cat = r["category"]
        if cat not in acc:
            acc[cat] = {"user": [], "req": []}
            axes.append(cat)
        acc[cat]["user"].append(base_score(r["measured_level"]) if r["measured_level"] else 0)
        acc[cat]["req"].append(base_score(r["required_level"]) if r["required_level"] else 0)

    def _avg(v: list[float]) -> int:
        return round(sum(v) / len(v)) if v else 0

    radar = {
        "categories": axes,
        "series": [
            {"key": "user", "name": "学员实测能力", "scores": [_avg(acc[c]["user"]) for c in axes]},
            {"key": "required", "name": "岗位标准要求", "scores": [_avg(acc[c]["req"]) for c in axes]},
        ],
        # 兼容旧前端：scores 单系列仍指学员实测
        "scores": [_avg(acc[c]["user"]) for c in axes],
    }

    return {
        "channel": channel,
        "target_occupation_id": (occupation or {}).get("id"),
        "target_occupation_name": (occupation or {}).get("name"),
        "match_score": match_score,
        # 本次测评覆盖了岗位技能权重的百分之多少 —— 匹配度的置信度靠它说明
        "coverage": coverage,
        "radar": radar,
        "strengths": strengths,
        "gaps": gaps,
        "untested": untested,
        "items": rows,
        "counts": {
            "skill_total": len(rows),
            "tested": len(tested_rows),
            "untested": len(untested),
            "strength": len(strengths),
            "gap": len(gaps),
        },
        "summary": (
            f"综合能力匹配度 {match_score}%（覆盖岗位技能权重 {coverage}%）；"
            f"已达标 {len(strengths)} 项，待补强 {len(gaps)} 项"
            f"，另有 {len(untested)} 项本次未覆盖"
        ),
        # 原型「基于短板一键生成个人自适应学习计划」
        "next_action": {
            "type": "learning_path",
            "label": "基于短板一键生成个人自适应学习计划",
            "gap_skills": [g["skill_key"] for g in gaps[:8]],
        },
    }
