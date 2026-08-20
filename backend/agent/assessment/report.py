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
单项达标率 min(实测/要求, 1) 按权重加权，分母是岗位**全部可评分**技能的权重——
没测到的按 0 分计入。它回答的是「这个岗位你整体准备好了多少」，所以未覆盖的技能
必须拖低分数：那部分确实还没有证据。置信度由 coverage（本次覆盖了多少权重）说明。

此前这里的分母只有**实测**技能权重，而岗位探索列表（`biz_store.match_with_profile`）
用的是全权重：同一个学员、两个契合度完全一样的岗位，只因一个诊断过一个没诊断，
列表上就并排显示 100% 和 30%。级联优先级本身没错，错在两个数不在同一刻度上却
并排展示，因此统一到全权重分母。

「你考过的部分掌握得怎样」这个口径仍有价值，另出 `tested_match_score`（分母只算
实测权重）供报告详情页用；列表排序、目标卡一律用 match_score。

没有基准就不编分数（2026-08）
----------------------------
要求档缺失或越界的技能**分子分母都不计**（`item.scorable=false`）：既不虚高也不压低，
因为没有基准根本无法评分。此前这类项经 `_ratio(lv, 0)` 拿到 1.0、按满分计入分子，
一个只测出 L1 的学员在「镗工」（3 项技能要求档全缺）上能拿到 60%——彻底的错答案。

库里 608 个有技能构成的岗位中 490 个（80%）要求档全缺（老国标节点只有
`attrs.level_code`、没有产品档 `attrs.level`），所以这不是边缘情况：这些岗位
`match_score` 为 **None**（不是 0.0——0% 会被学员读成「完全不匹配」，真相是
「这个岗位没配能力要求」），并由 `score_status` 说明是哪一种算不出来。

配置不全时服务端主动降级（2026-08）
----------------------------------
「全缺」与「全配齐」之间还有一大片中间态：某岗位 82% 的权重没配要求档、只有 18%
配了，学员那 18% 全达标 ⇒ 分数 50%、状态 `ok`，学员读成「我匹配一半」，
真相是「这岗位大部分要求没人填」。所以缺基准权重占比**超过**
`config.PARTIAL_BASELINE_PCT`（30%）时，分数照给（那 18% 是真实依据，丢掉更亏），
但 `score_status` 降级成 `partial_baseline`，`summary` 里也写明仅供参考。
阈值放服务端而不是前端：前端每个页面各定一次迟早不一致。
"""
from __future__ import annotations

from typing import Any

from backend.kg.pg_store.config import (
    as_level,
    as_weight,
    degrade_for_baseline_gap,
    weighted_score,
)
from backend.kg.pg_store.skill_level_meta import base_score, label_map


def _ratio(measured: int, required: int) -> float:
    """单项达标率。

    `required=0`（岗位没给要求档）返回 1.0 是**既有语义**，保持不动；但这类项已经
    不进 match_score 的分子分母了（见 build_report 的 scorable 分支），
    所以「有实测即满分」不再能把总分抬高——它只留在 `item.ratio` 上供展示。
    """
    if not required:
        return 1.0 if measured else 0.0
    return min(measured / required, 1.0) if measured else 0.0


RADAR_MIN_AXES = 3          # 少于 3 个轴画不成多边形
RADAR_MAX_AXES = 6          # 轴太多标签会糊成一团

# 档位守卫（1–5 之外或非整数 → None，视作缺失）与权重守卫的唯一实现在
# kg/pg_store/config.py：报告侧与画像侧（biz_store.match_with_profile）必须同一套
# 规则，否则一条脏数据进来两个页面又给出两个数字。


def _score(level: Any) -> int:
    """档位 → 基准分；缺失或越界取 0 分，不抛异常。

    `skill_level_meta.base_score()` 对未知档位 raise KeyError，而档位来路不干净
    （详见 `config.as_level`），一条脏档位不能打死一整份报告。
    """
    lv = as_level(level)
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
            # 轴标签是**给人看的**，用展示名。用 skill_key 的话雷达图上是一圈哈希
            "categories": [r.get("skill_name") or r["skill_key"] for r in top],
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


def _summary(
    *,
    match_score: float | None,
    score_status: str,
    coverage: float,
    strengths: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    untested: list[dict[str, Any]],
    no_baseline: list[dict[str, Any]],
    no_baseline_weight: float = 0.0,
) -> str:
    """一句话结论。算不出分时说清是**哪一种**算不出来，不要写「匹配度 None%」。"""
    if score_status == "no_baseline":
        return (
            f"该岗位 {len(no_baseline)} 项技能均未配置能力要求档，无法计算匹配度"
            f"（本次实测 {len(no_baseline) - len(untested)} 项，结果见明细）"
        )
    if score_status == "no_skills":
        return "该岗位尚未配置技能构成，无法计算匹配度"
    tail = f"；另有 {len(no_baseline)} 项因未配要求档无法评分" if no_baseline else ""
    # 缺口过阈值时把「这个数只能参考」写进给学员看的那句话里：前端可能只展示 summary，
    # 光把 partial_baseline 放在字段里，读到的人还是会把它当结论。
    if score_status == "partial_baseline":
        tail += f"（占岗位权重 {no_baseline_weight}%，分数仅供参考）"
    return (
        f"综合能力匹配度 {match_score}%（覆盖岗位技能权重 {coverage}%）；"
        f"已达标 {len(strengths)} 项，待补强 {len(gaps)} 项"
        f"，另有 {len(untested)} 项本次未覆盖" + tail
    )


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
    total_w = 0.0        # 分母：**有可信要求档**的技能权重（缺基准的不计）
    got_w = 0.0          # 分子：达标率 × 权重，同样只算有基准的
    t_total = 0.0        # 其中本次实测到的权重（tested_match_score 的分母）
    t_got = 0.0
    all_w = 0.0          # 岗位全部技能权重，只用来算「多少权重因缺基准无法评分」
    nb_w = 0.0           # 缺基准的权重

    for it in required_items:
        key = it.get("skill_key")
        # 档位与权重在这里就解析干净（规则见 config.as_level / as_weight）：
        # 越界的 required_level 不只画歪雷达，还会经 _ratio 污染 ratio / ok / urgency；
        # 非数值 weight 直接 float() 会抛 ValueError 打死整份报告。守卫必须在最上游。
        req = as_level(it.get("required_level"))
        w = as_weight(it.get("weight"))
        m = measured.get(key) or {}
        lv = as_level(m.get("level")) or 0
        r = _ratio(lv, req or 0)
        all_w += w
        if req:
            total_w += w
            got_w += r * w
            if m:
                t_total += w
                t_got += r * w
        else:
            # 没有基准就无法评分：分子分母都不计，由 score_status / no_baseline 显式表达
            nb_w += w
        rows.append(
            {
                "skill_key": key,
                # 展示名：key 是 SKxxxxxxxxxx，前端与雷达轴都要用这个
                "skill_name": it.get("skill_name") or key,
                "category": it.get("category") or "未分类",
                "required_level": req,
                "required_label": labels.get(req) if req else None,
                "measured_level": lv or None,
                "measured_label": labels.get(lv),
                "weight": w,
                "weight_pct": round(w * 100) if w else None,
                "ratio": round(r, 3),
                "ok": bool(lv and req and lv >= req),
                "scorable": bool(req),
                "tested": bool(m),
                "source": m.get("source"),
                "evidence_score": m.get("evidence_score"),
                "capped": bool(m.get("capped")),
                # 短板紧迫度：权重越高、差距越大越该先补
                "urgency": round(w * max(0, (req - lv)), 4) if req else 0.0,
            }
        )

    # match_score 分母是岗位**可评分**技能的全部权重（与 match_with_profile 同源，
    # 见模块 docstring）：它答的是「这个岗位你整体准备好了多少」，没考到的那部分本就
    # 还没有证据、该拖低分数；而缺要求档的那部分没有基准，谈不上达标率，只能排除。
    tested_rows = [r for r in rows if r["tested"]]
    scorable_rows = [r for r in rows if r["scorable"]]
    no_baseline = sorted(
        (r for r in rows if not r["scorable"]), key=lambda x: -(x["weight"] or 0)
    )
    match_score, score_status = weighted_score(
        skill_total=len(rows),
        scorable_total=len(scorable_rows),
        total_w=total_w,
        got_w=got_w,
        has_evidence=any(r["tested"] for r in scorable_rows),
    )
    # 只算实测权重的那个口径：答「你考过的部分掌握得怎样」，不参与列表排序与横向比较。
    # 一项可评分技能都没有时同样给 None，理由与 match_score 相同。
    tested_match_score = (
        None if match_score is None else (round(100 * t_got / t_total, 1) if t_total else 0.0)
    )
    # 证据覆盖率：分母是**可评分**权重，答「该评的部分考了多少」
    coverage = round(100 * t_total / total_w, 1) if total_w else 0.0
    # 基准缺口：分母是岗位**全部**权重，答「有多少权重因为没配要求档而无法评分」。
    # 权重全为 0（脏数据）时退回按项数算，否则这个字段在最需要它的岗位上恒为 0
    no_baseline_weight = (
        round(100 * nb_w / all_w, 1) if all_w
        else (round(100 * len(no_baseline) / len(rows), 1) if rows else 0.0)
    )
    # 配置不全的岗位：分数照给（已配置的那部分是真实依据，不该丢），但状态降级成
    # partial_baseline —— 否则「82% 的权重没人填要求档、剩下 18% 全达标」会以
    # `ok` + 50% 的面目出现，学员读成「我匹配一半」。阈值见 config.PARTIAL_BASELINE_PCT。
    score_status = degrade_for_baseline_gap(score_status, no_baseline_weight)

    # 优势/短板都只在**测过且有基准**的技能里选：没考过就没有证据，没基准就没有标尺
    strengths = [r for r in tested_rows if r["scorable"] and r["ok"]]
    # 超越幅度大的优先，其次看权重：L1 要求达 L1 也算达标，但不该排在
    # 「要求 L3 实测 L5」前面
    strengths.sort(
        key=lambda x: (
            -((x["measured_level"] or 0) - (x["required_level"] or 0)),
            -(x["weight"] or 0),
        )
    )
    gaps = [r for r in tested_rows if r["scorable"] and not r["ok"]]
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
        "score_status": score_status,
        # 只按实测技能算的匹配度：与 match_score 分母不同，不可混用（详情页展示用）
        "tested_match_score": tested_match_score,
        # 本次测评覆盖了**可评分**技能权重的百分之多少 —— 匹配度的置信度靠它说明
        "coverage": coverage,
        # 缺要求档、无法评分的权重占岗位全部技能权重的百分之多少
        "no_baseline_weight": no_baseline_weight,
        "radar": radar,
        "strengths": strengths,
        "gaps": gaps,
        "untested": untested,
        "no_baseline": no_baseline,
        "items": rows,
        "counts": {
            "skill_total": len(rows),
            "tested": len(tested_rows),
            "untested": len(untested),
            "strength": len(strengths),
            "gap": len(gaps),
        },
        "summary": _summary(
            match_score=match_score,
            score_status=score_status,
            coverage=coverage,
            strengths=strengths,
            gaps=gaps,
            untested=untested,
            no_baseline=no_baseline,
            no_baseline_weight=no_baseline_weight,
        ),
        # 原型「基于短板一键生成个人自适应学习计划」
        "next_action": {
            "type": "learning_path",
            "label": "基于短板一键生成个人自适应学习计划",
            "gap_skills": [g["skill_key"] for g in gaps[:8]],
        },
    }
