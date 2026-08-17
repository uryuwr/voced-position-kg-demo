"""综合能力报告：实测档位 × 岗位标准要求 → 匹配度 / 双系列雷达 / 优势 / 短板。

对齐原型「AI 智能能力诊断报告」
------------------------------
- 右上角 **综合能力匹配度**            → match_score
- **能力维度与目标极域图**（双层雷达）  → radar.series 两条：学员实测能力 / 岗位标准要求
- **优势能力领域**（已达到或超越基准）  → strengths[]，展示「技能名 L4」
- **关键能力短板**（需优先攻关提升）    → gaps[]，未测到的显示「待补强」，测到但不足的显示实际档
- **基于短板一键生成学习计划**          → next_action 里带上短板技能，供学习计划接口直接消费

匹配度口径（**与 position_match 同源**）
--------------------------------------
单项达标率 min(实测/要求, 1) 按权重加权，分母是岗位**全部**技能权重——没测到的
按 0 分计入。它回答的是「这个岗位你整体准备好了多少」，所以未覆盖的技能必须拖低
分数：那部分确实还没有证据。置信度由 coverage（本次覆盖了多少权重）说明。

此前这里的分母只有**实测**技能权重，而岗位探索列表（`biz_store.match_with_profile`）
用的是全权重：同一个学员、两个契合度完全一样的岗位，只因一个诊断过一个没诊断，
列表上就并排显示 100% 和 30%。级联优先级本身没错，错在两个数不在同一刻度上却
并排展示，因此统一到全权重分母。

「你考过的部分掌握得怎样」这个口径仍有价值，另出 `tested_match_score`（分母只算
实测权重）供报告详情页用；列表排序、目标卡一律用 match_score。
"""
from __future__ import annotations

from typing import Any

from backend.kg.pg_store.skill_level_meta import base_score, label_map


def _ratio(measured: int, required: int) -> float:
    if not required:
        return 1.0 if measured else 0.0
    return min(measured / required, 1.0) if measured else 0.0


RADAR_MIN_AXES = 3          # 少于 3 个轴画不成多边形
RADAR_MAX_AXES = 6          # 轴太多标签会糊成一团
LEVEL_MIN, LEVEL_MAX = 1, 5  # 产品档 L1–L5（真源 kg/pg_store/skill_level_meta.py）


def _level(v: Any) -> int | None:
    """读侧档位守卫：1–5 之外或非整数 → None（视作**缺失**）。

    `skill_level_meta.base_score()` 对未知档位 raise KeyError，而档位两条来路都
    不干净：required_level 来自 kg_edge 指向的 skill_level 节点 `attrs.level`
    （无约束 TEXT；读侧 `config.attrs_level_int` 的正则只挡非数字，`9` 会原样穿过来），
    measured_level 来自选项 level / 简历画像。写侧 `write._assert_attrs_sane` 校验过，
    但采集脚本与直连改库绕得过应用层，读侧必须自己站得住——
    一条脏档位不能打死一整份报告。

    取 None 而不是夹到 5：把 9 当成 L5 会凭空给出「已达标」的结论。口径与
    `config.attrs_level_int`（脏值取 NULL）一致。
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        lv = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return lv if LEVEL_MIN <= lv <= LEVEL_MAX else None


def _score(level: Any) -> int:
    """档位 → 基准分；缺失或越界取 0 分，不抛异常。"""
    lv = _level(level)
    return base_score(lv) if lv else 0


def _build_radar(tested_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """双系列雷达（学员实测 / 岗位标准要求）。

    **轴是技能，不是技能大类** —— 原型上的「流量承接与转化 / 广告精准投放 /
    数据分析与复盘 / 直播控场与话术」都是具体技能。早先按大类聚合，遇到技能集中在
    一两个大类的岗位（如计算机程序设计员的 4 项技能几乎同类）就只剩 1–2 根轴，
    直接退化成「维度不足，无法绘制」。

    技能不足 3 项时退回大类聚合再试一次，仍不足才判定画不出来。

    档位一律过 `_score()`：build_report 传进来的行已经夹过，但本函数是模块级的，
    脏档位不该在这里变成 KeyError。
    """
    def _axes_from(rows: list[dict[str, Any]]) -> dict[str, Any]:
        top = sorted(rows, key=lambda x: -(x.get("weight") or 0))[:RADAR_MAX_AXES]
        return {
            "axis_type": "skill",
            "categories": [r["skill_key"] for r in top],
            "series": [
                {
                    "key": "user",
                    "name": "学员实测能力",
                    "scores": [_score(r["measured_level"]) for r in top],
                },
                {
                    "key": "required",
                    "name": "岗位标准要求",
                    "scores": [_score(r["required_level"]) for r in top],
                },
            ],
            "scores": [_score(r["measured_level"]) for r in top],
        }

    if len(tested_rows) >= RADAR_MIN_AXES:
        return _axes_from(tested_rows)

    # 技能数不够，退回按大类聚合（多个技能合成一根轴，至少可能凑出 3 根）
    acc: dict[str, dict[str, list[float]]] = {}
    order: list[str] = []
    for r in tested_rows:
        cat = r["category"] or "未分类"
        if cat not in acc:
            acc[cat] = {"user": [], "req": []}
            order.append(cat)
        acc[cat]["user"].append(_score(r["measured_level"]))
        acc[cat]["req"].append(_score(r["required_level"]))

    def _avg(v: list[float]) -> int:
        return round(sum(v) / len(v)) if v else 0

    return {
        "axis_type": "category",
        "categories": order,
        "series": [
            {"key": "user", "name": "学员实测能力", "scores": [_avg(acc[c]["user"]) for c in order]},
            {"key": "required", "name": "岗位标准要求", "scores": [_avg(acc[c]["req"]) for c in order]},
        ],
        "scores": [_avg(acc[c]["user"]) for c in order],
    }


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
        # 档位在这里就夹干净：越界的 required_level 不只画歪雷达，
        # 还会经 _ratio 污染 ratio / ok / urgency，所以守卫放在最上游而不是 _build_radar
        req = _level(it.get("required_level")) or 0
        w = float(it.get("weight") or 0)
        m = measured.get(key) or {}
        lv = _level(m.get("level")) or 0
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

    # match_score 分母是岗位**全部**技能权重（与 match_with_profile 同源，见模块 docstring）：
    # 它答的是「这个岗位你整体准备好了多少」，没考到的那部分本就还没有证据，该拖低分数。
    # 分子分母都在上面的循环里按行累加，未测项 ratio=0。
    tested_rows = [r for r in rows if r["tested"]]
    t_total = sum(r["weight"] for r in tested_rows) or 0.0
    t_got = sum(r["ratio"] * r["weight"] for r in tested_rows)
    match_score = round(100 * got_w / total_w, 1) if total_w else 0.0
    # 只算实测权重的那个口径：答「你考过的部分掌握得怎样」，不参与列表排序与横向比较
    tested_match_score = round(100 * t_got / t_total, 1) if t_total else 0.0
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

    radar = _build_radar(tested_rows)

    return {
        "channel": channel,
        "target_occupation_id": (occupation or {}).get("id"),
        "target_occupation_name": (occupation or {}).get("name"),
        "match_score": match_score,
        # 只按实测技能算的匹配度：与 match_score 分母不同，不可混用（详情页展示用）
        "tested_match_score": tested_match_score,
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
