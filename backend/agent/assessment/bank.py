"""出题：把岗位的技能构成变成考核题，**分批懒加载 + 按作答自适应**。

出题标准（设计说明）
====================

**考什么由知识图谱定，题目由模型写。**
岗位 `requires` 技能构成给出「考哪些技能」（skill_key）、「考到什么水平」
（required_level）、「各占多重」（weight）；模型据此 + 岗位职责 + 国标能力描述
生成对应难度的题目。库里没有题库，这一步必须由模型来做。

**题型**

| 题型 | 形式 | 职责 |
| --- | --- | --- |
| 选择题 | 情景判断题（SJT），4 个选项**各自对应一个能力档位** | 广度 + 客观定级：选中即定档，不需模型判分 |
| 问答题 | 开放追问，随题生成 rubric 评分要点 | 深度：验证「声称达标」是否有真实支撑 |

选项不是「我会/我不会」——自评量表只能测出学员如何看待自己。SJT 让不同水平的人
在同一情景下做出不同选择（只换常规件=L1，照手册逐项排查=L2，复现工况后用示波器
比对波形定位=L3，溯源批次缺陷并形成 SOP=L5），选项本身即水平证据。

**分批懒加载**
首批只出 3 题（约 8s，首屏可接受），学员边答边在后台出下一批。好处不只是快：
后续出题能看到前面的作答，可据此调难度、决定深挖还是换维度。

**自适应规则**
- 学员在某技能达到/超过岗位要求档 → 追加该技能的**问答题**，验证是否名副其实
- 明显低于要求档 → 不再深挖该技能（已能判定为短板），转向未测的高权重技能
- 下一批技能仍按权重优先、同大类不超过 2 项（保证雷达维度分散）

**收敛规则（防止无限出题，任一命中即停）**
1. 总题数达 MAX_QUESTIONS(10)
2. 批次数达 MAX_BATCHES(4)
3. 加权覆盖率 ≥ COVERAGE_TARGET(0.8)，且已答满首批
4. 没有可考的新技能了
5. 单技能出题数达 MAX_PER_SKILL(2)（1 选择 + 1 追问）

**题库缓存**：选择题按 (occupation_id, skill_key) 存 `biz_assessment_item`，
同岗位后续学员直接复用。问答题依赖当次作答上下文，不缓存。
"""
from __future__ import annotations

import json
import re
from typing import Any

from backend.agent.llm import invoke_fast, llm_ready
from backend.kg.pg_store.skill_level_meta import behavior_map, label_map

FIRST_BATCH = 3           # 首批题量（首屏速度）
BATCH_SIZE = 3            # 后续每批
MAX_QUESTIONS = 10        # 硬上限
MAX_BATCHES = 4           # 批次上限
COVERAGE_TARGET = 0.8     # 加权覆盖率达标即停
MAX_PER_SKILL = 2         # 同一技能最多出几题
MAX_PER_CATEGORY = 2      # 同一大类最多出几题

_PLACEHOLDER = re.compile(r"权重\s*\d+\s*%|·\s*L[1-5]\s*(·|$)")


def _is_real_requirement(text: str | None) -> bool:
    """是否为可读的国标能力描述（库内仅 28% 是真描述，其余为采集期占位串）。"""
    t = (text or "").strip()
    return len(t) >= 30 and not _PLACEHOLDER.search(t)


def _requirement_hint(item: dict[str, Any]) -> str | None:
    req = item.get("required_level")
    for lv in item.get("levels") or []:
        if lv.get("level") == req and _is_real_requirement(lv.get("requirement")):
            return str(lv["requirement"])[:300]
    return None


# ── 选技能 ───────────────────────────────────────────────────


def plan_batch_skills(
    items: list[dict[str, Any]],
    *,
    asked: dict[str, int] | None = None,
    graded: list[dict[str, Any]] | None = None,
    size: int = BATCH_SIZE,
) -> list[dict[str, Any]]:
    """选下一批要考的技能，并标注题型（choice / open）。

    asked: {skill_key: 已出题数}；graded: 已判分结果（用于自适应）。
    """
    asked = asked or {}
    graded = graded or []
    by_key = {str(i.get("skill_key")): i for i in items}

    # 已达标且权重高的技能 → 追加问答题验证（自适应深挖）
    verify: list[dict[str, Any]] = []
    for g in graded:
        key = str(g.get("skill_key") or "")
        it = by_key.get(key)
        if not it or g.get("type") != "choice":
            continue
        if asked.get(key, 0) >= MAX_PER_SKILL:
            continue
        lv, req = g.get("level") or 0, g.get("required_level") or 0
        if req and lv >= req:                       # 声称达标 → 需要证据
            verify.append({**it, "_item_type": "open", "_reason": "verify_claim"})
    verify.sort(key=lambda x: -(float(x.get("weight") or 0)))

    # 未考过的技能 → 出选择题，权重优先、同大类限额
    cat_used: dict[str, int] = {}
    for g in graded:
        it = by_key.get(str(g.get("skill_key") or ""))
        if it:
            c = str(it.get("category") or "未分类")
            cat_used[c] = cat_used.get(c, 0) + 1
    fresh: list[dict[str, Any]] = []
    for it in sorted(items, key=lambda x: -(float(x.get("weight") or 0))):
        key = str(it.get("skill_key"))
        if asked.get(key):
            continue
        cat = str(it.get("category") or "未分类")
        if cat_used.get(cat, 0) + sum(
            1 for f in fresh if str(f.get("category") or "未分类") == cat
        ) >= MAX_PER_CATEGORY:
            continue
        fresh.append({**it, "_item_type": "choice", "_reason": "coverage"})

    # 验证题不超过本批一半：主线仍是扩大覆盖
    out = verify[: max(1, size // 2)] + fresh
    return out[:size]


def should_stop(
    items: list[dict[str, Any]],
    *,
    asked_total: int,
    batches: int,
    graded: list[dict[str, Any]],
    next_batch: list[dict[str, Any]],
) -> tuple[bool, str]:
    """收敛判定。返回 (是否停止, 原因)。"""
    if asked_total >= MAX_QUESTIONS:
        return True, f"达到题量上限 {MAX_QUESTIONS}"
    if batches >= MAX_BATCHES:
        return True, f"达到批次上限 {MAX_BATCHES}"
    if not next_batch:
        return True, "没有可考的新技能"
    total_w = sum(float(i.get("weight") or 0) for i in items)
    if total_w > 0 and asked_total >= FIRST_BATCH:
        tested = {str(g.get("skill_key")) for g in graded if g.get("level")}
        cov = sum(
            float(i.get("weight") or 0) for i in items if str(i.get("skill_key")) in tested
        ) / total_w
        if cov >= COVERAGE_TARGET:
            return True, f"加权覆盖率已达 {round(cov*100)}%"
    return False, ""


# ── 降级题（网关不可用） ─────────────────────────────────────


def fallback_choice(item: dict[str, Any]) -> dict[str, Any]:
    """自评题。考核力弱于 SJT，仅保证流程可跑通，报告里以 variant 区分。"""
    labels, behaviors = label_map(), behavior_map()
    req = int(item.get("required_level") or 0)
    levels = sorted({int(x) for x in (item.get("available_levels") or []) if x})
    if req and req not in levels:
        levels = sorted(set(levels) | {req})
    return {
        "type": "choice",
        "variant": "self_report",
        "skill_key": item.get("skill_key"),
        "category": item.get("category"),
        "required_level": req or None,
        "weight": item.get("weight"),
        "prompt": f"在「{item.get('skill_key')}」上，哪一条最符合你目前的实际水平？",
        "options": [
            {
                "value": i + 1,
                "level": lv,
                "text": f"L{lv} {labels.get(lv,'')}：{behaviors.get(lv,'')}",
            }
            for i, lv in enumerate(levels or [1, 2, 3, 4, 5])
        ],
    }


def fallback_open(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "open",
        "variant": "generic",
        "skill_key": item.get("skill_key"),
        "category": item.get("category"),
        "required_level": int(item.get("required_level") or 0) or None,
        "weight": item.get("weight"),
        "prompt": (
            f"请具体描述一次你在「{item.get('skill_key')}」上的实际经历："
            "任务是什么、你用了什么方法、结果如何（有数据请一并给出）。"
        ),
        "rubric": ["有具体任务背景", "方法/步骤可复述", "结果可验证（最好有量化）"],
        "min_chars": 80,
    }


# ── 模型出题 ─────────────────────────────────────────────────

_SYS_CHOICE = """你是职业技能考核命题专家。为给定的每个技能各出一道情景判断题。
题干≤70字，须是该岗位真实工作场景中的具体问题，不要问「你会不会」「你的水平如何」。
每题恰好4个选项、每项≤35字，标注 level（该选项体现的能力档位，同题内互不相同），
都要像合理答案（不要有荒谬项）；level 越高越体现独立性、系统性与前瞻性。
档位标尺：{anchors}
只输出JSON：{{"items":[{{"skill_key":"...","prompt":"...","options":[{{"level":1,"text":"..."}},{{"level":2,"text":"..."}},{{"level":3,"text":"..."}},{{"level":5,"text":"..."}}]}}]}}"""

_SYS_OPEN = """你是职业技能考核命题专家。为给定的每个技能各出一道开放追问题。
题干≤70字，针对该技能的关键难点追问具体经历或方案，要能区分真做过与只听说过。
每题给3条 rubric（评分要点），每条是一个可判断的观察点、≤20字。
只输出JSON：{{"items":[{{"skill_key":"...","prompt":"...","rubric":["...","...","..."]}}]}}"""


def _extract(raw: str) -> list[dict[str, Any]]:
    """解析模型返回的 items 数组，容忍尾部被 max_tokens 截断的情况。

    截断时整包 JSON 不可解析，但前面已完整的题目仍然可用——逐个对象扫出来，
    比整批降级成自评题划算得多。
    """
    text = raw or ""
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            data = json.loads(m.group(0))
            items = list(data.get("items") or [])
            if items:
                return items
        except json.JSONDecodeError:
            pass
    # 逐个提取 items 数组里已闭合的对象
    out: list[dict[str, Any]] = []
    start = text.find('"items"')
    scan = text[start:] if start >= 0 else text
    depth, buf = 0, ""
    for ch in scan:
        if ch == "{":
            depth += 1
        if depth:
            buf += ch
        if ch == "}":
            depth -= 1
            if depth == 0 and buf:
                try:
                    obj = json.loads(buf)
                    if isinstance(obj, dict) and obj.get("skill_key"):
                        out.append(obj)
                except json.JSONDecodeError:
                    pass
                buf = ""
    if out:
        return out
    raise ValueError(f"模型未返回可解析的 JSON：{text[:150]}")


def _skill_lines(items: list[dict[str, Any]], *, with_context: str = "") -> str:
    lines = []
    for it in items:
        line = (
            f"- {it.get('skill_key')}｜大类：{it.get('category') or '未分类'}"
            f"｜岗位要求档：L{it.get('required_level') or '?'}"
        )
        hint = _requirement_hint(it)
        if hint:
            line += f"\n  国标该档描述：{hint}"
        lines.append(line)
    return "\n".join(lines) + (f"\n\n{with_context}" if with_context else "")


def _norm_choice(gen: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
    prompt = str(gen.get("prompt") or "").strip()
    if len(prompt) < 8:
        return None
    seen: set[int] = set()
    options: list[dict[str, Any]] = []
    for o in gen.get("options") or []:
        try:
            lv = max(1, min(5, int(o.get("level"))))
        except (TypeError, ValueError):
            continue
        txt = str(o.get("text") or "").strip()
        if not txt or lv in seen:
            continue
        seen.add(lv)
        options.append({"value": len(options) + 1, "level": lv, "text": txt[:300]})
    if len(options) < 3:
        return None
    return {
        "type": "choice",
        "variant": "sjt",
        "skill_key": item.get("skill_key"),
        "category": item.get("category"),
        "required_level": int(item.get("required_level") or 0) or None,
        "weight": item.get("weight"),
        "prompt": prompt[:400],
        "options": options,
    }


def _norm_open(gen: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
    prompt = str(gen.get("prompt") or "").strip()
    if len(prompt) < 8:
        return None
    rubric = [str(x).strip()[:80] for x in (gen.get("rubric") or []) if str(x).strip()]
    return {
        "type": "open",
        "variant": "sjt",
        "skill_key": item.get("skill_key"),
        "category": item.get("category"),
        "required_level": int(item.get("required_level") or 0) or None,
        "weight": item.get("weight"),
        "prompt": prompt[:400],
        "rubric": rubric[:3] or ["有具体任务背景", "方法可复述", "结果可验证"],
        "min_chars": 80,
    }


def generate_batch(
    batch_skills: list[dict[str, Any]],
    *,
    occupation: dict[str, Any] | None = None,
    graded: list[dict[str, Any]] | None = None,
    use_cache: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """出一批题 → (questions, meta)。meta.engine ∈ cache/llm/llm_partial/fallback。"""
    if not batch_skills:
        return [], {"engine": "empty"}
    occ = occupation or {}
    occ_id = occ.get("id")
    want_choice = [i for i in batch_skills if i.get("_item_type") != "open"]
    want_open = [i for i in batch_skills if i.get("_item_type") == "open"]
    meta: dict[str, Any] = {"engine": "fallback", "choice": len(want_choice), "open": len(want_open)}

    out: list[dict[str, Any]] = []
    pending_choice = want_choice

    # 选择题可复用题库；问答题依赖当次作答上下文，不缓存
    if use_cache and occ_id and pending_choice:
        from backend.agent.assessment.item_store import load_choice_items

        hit = load_choice_items(occ_id, [str(i.get("skill_key")) for i in pending_choice])
        if hit:
            out += [dict(hit[str(i.get("skill_key"))]) for i in pending_choice if str(i.get("skill_key")) in hit]
            pending_choice = [i for i in pending_choice if str(i.get("skill_key")) not in hit]
            meta["cached"] = len(out)

    if llm_ready() and (pending_choice or want_open):
        anchors = "；".join(f"L{k}={v}" for k, v in behavior_map().items())
        miss = 0
        try:
            if pending_choice:
                user = (
                    f"岗位：{occ.get('name') or '未指定'}\n"
                    f"岗位职责：{(occ.get('description') or '（无）')[:400]}\n"
                    f"技能：\n{_skill_lines(pending_choice)}"
                )
                raw = invoke_fast(
                    [("system", _SYS_CHOICE.format(anchors=anchors)), ("user", user)],
                    max_tokens=1200 * max(1, len(pending_choice)),
                )
                gen = {str(g.get("skill_key") or ""): g for g in _extract(raw)}
                for it in pending_choice:
                    q = _norm_choice(gen.get(str(it.get("skill_key")), {}), it)
                    if q is None:
                        q, miss = fallback_choice(it), miss + 1
                    out.append(q)
                if occ_id:
                    from backend.agent.assessment.item_store import save_choice_items

                    save_choice_items(occ_id, out)

            if want_open:
                ctx = ""
                for g in graded or []:
                    if g.get("type") == "choice" and any(
                        str(g.get("skill_key")) == str(i.get("skill_key")) for i in want_open
                    ):
                        ctx += (
                            f"学员在「{g.get('skill_key')}」的选择显示其做法为："
                            f"{(g.get('picked_text') or '')[:80]}（判为L{g.get('level')}）。"
                            "请针对这个层级追问，验证其真实性。\n"
                        )
                user = (
                    f"岗位：{occ.get('name') or '未指定'}\n"
                    f"技能：\n{_skill_lines(want_open, with_context=ctx)}"
                )
                raw = invoke_fast(
                    [("system", _SYS_OPEN), ("user", user)],
                    max_tokens=800 * max(1, len(want_open)),
                )
                gen = {str(g.get("skill_key") or ""): g for g in _extract(raw)}
                for it in want_open:
                    q = _norm_open(gen.get(str(it.get("skill_key")), {}), it)
                    if q is None:
                        q, miss = fallback_open(it), miss + 1
                    out.append(q)

            meta["engine"] = "cache" if not pending_choice and not want_open else (
                "llm" if miss == 0 else "llm_partial"
            )
            meta["fallback_count"] = miss
        except Exception as e:  # noqa: BLE001 — 出题失败不能挡住测评
            meta["error"] = str(e)[:300]
            for it in pending_choice:
                out.append(fallback_choice(it))
            for it in want_open:
                out.append(fallback_open(it))
    elif pending_choice or want_open:
        out += [fallback_choice(i) for i in pending_choice] + [fallback_open(i) for i in want_open]
    elif out:
        meta["engine"] = "cache"

    meta["variants"] = sorted({q.get("variant") or "?" for q in out})
    return out, meta
