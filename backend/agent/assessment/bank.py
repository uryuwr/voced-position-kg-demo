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
# 档位/权重脏值解析的唯一实现（与报告侧、画像侧同一套规则）：`float(w or 0)`、
# `int(lv or 0)` 遇到 `"abc"` 会抛 ValueError —— 出题是长连接的第一步，
# 一条脏边就能让整场测评起不来。
from backend.kg.pg_store.config import as_level, as_weight
from backend.kg.pg_store.skill_level_meta import behavior_map, label_map

FIRST_BATCH = 3           # 首批题量（首屏速度）
BATCH_SIZE = 3            # 后续每批
MAX_QUESTIONS = 10        # 硬上限
MAX_BATCHES = 4           # 批次上限
COVERAGE_TARGET = 0.8     # 加权覆盖率达标即停
MIN_QUESTIONS = 4         # 题数下限：3 题就结束体感太草率
MIN_COVER_QUESTIONS = 4   # 覆盖题下限
VERIFY_LEVEL = 4          # 要求档 >= 此值的技能需要开放题验证
MAX_PER_SKILL = 2         # 同一技能最多出几题
MAX_PER_CATEGORY = 3      # 同一大类最多出几题（技能集中在少数大类的岗位不能因此少考）
RADAR_MIN_SKILLS = 3      # 报告雷达至少需要的已测技能数
OPEN_PER_RUN = 2          # 一轮测评里追加的问答题上限

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


def _fit_budget(
    cover: int, verify: int, *, budget: int, cover_floor: int
) -> tuple[int, int]:
    """把「想考的题数」压进 budget：**两类题各有下限，先保下限再按比例分剩余名额**。

    不能简单把清单截断到 budget：验证题一律排在覆盖题之后，一刀切等于把它们
    全砍掉，要求 L4/L5 的技能就再也拿不到证据——而区分两类题正是
    estimate_total 存在的理由。

    此前只有覆盖题有下限、验证题纯按比例缩，方向反了：技能越多 → demand 越大 →
    验证题占比越小。实测 4/8/12 项技能排 2 道验证题，20/28/40 项**只剩 1 道**。
    验证题是要模型判分的开放追问、是唯一的深度考核手段，**高标准岗位恰恰最需要它**。
    所以给它同样的下限 OPEN_PER_RUN（有需求时保满 2 道），名额从覆盖题里让。

    两个下限：
    - 覆盖题 `cover_floor`（雷达至少 RADAR_MIN_SKILLS 根轴、不少于 MIN_COVER_QUESTIONS 道）
    - 验证题 `min(verify, OPEN_PER_RUN)`

    预算装不下两个下限时（只会在 budget < cover_floor + OPEN_PER_RUN 时发生，
    真实路径上 budget 恒为 MAX_QUESTIONS=10 ≥ 4+2，够装）**优先保覆盖题**：
    雷达画不出来整份报告就没有形状，而验证题少一道只是深度弱一点。
    """
    demand = cover + verify
    if demand <= budget or budget <= 0:
        return cover, verify
    v_floor = min(verify, OPEN_PER_RUN)
    c_floor = min(cover_floor, cover, budget)
    # 按原比例分，但不低于各自下限、不高于各自需求量
    v = min(verify, max(v_floor, round(budget * verify / demand)))
    c = min(cover, budget - v)
    if c < c_floor:                      # 覆盖题被挤破下限 → 从验证题回收
        c = c_floor
        v = min(verify, max(0, budget - c))
    return c, v


def plan_all_skills(
    items: list[dict[str, Any]], plan: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """**一次性**规划整场要考的技能与题型，不看学员作答。

    出题因此可以在会话一开始就全部排定、并在后台并行生成：学员答题时下一批已经
    躺在缓存里，不必等模型。代价是失去「按回答调难度」的自适应——权衡后选前者：
    等待十几秒换一点难度微调不划算，而验证题本来就可以按**岗位要求档**预先决定
    （要求 L4/L5 的技能一律追问，不必等学员先自评达标）。

    题量以 `est['total']`（已受 MAX_QUESTIONS 约束）为预算，而不是照 `est['cover']`
    照排——后者是**没夹过上限的需求量**，直接用它，20 项技能的岗位会排出 18 题。
    """
    est = plan or estimate_total(items)
    ranked = sorted(items, key=lambda x: -(as_weight(x.get("weight"))))
    budget = min(int(est.get("total") or 0) or MAX_QUESTIONS, MAX_QUESTIONS)
    cover_n = min(int(est.get("cover") or 0) or MIN_COVER_QUESTIONS, len(ranked))
    verify_n = max(0, int(est.get("verify") or 0))
    cover_n, verify_n = _fit_budget(
        cover_n,
        verify_n,
        budget=budget,
        cover_floor=min(len(ranked), max(RADAR_MIN_SKILLS, MIN_COVER_QUESTIONS)),
    )

    # 覆盖题：按权重降序，同大类限额以保证雷达维度分散；不足时放开限额补足
    picked: list[dict[str, Any]] = []
    per_cat: dict[str, int] = {}
    for it in ranked:
        cat = str(it.get("category") or "未分类")
        if per_cat.get(cat, 0) >= MAX_PER_CATEGORY:
            continue
        picked.append(it)
        per_cat[cat] = per_cat.get(cat, 0) + 1
        if len(picked) >= cover_n:
            break
    if len(picked) < cover_n:
        keys = {str(x.get("skill_key")) for x in picked}
        for it in ranked:
            if str(it.get("skill_key")) not in keys:
                picked.append(it)
                if len(picked) >= cover_n:
                    break

    out = [{**it, "_item_type": "choice", "_reason": "coverage"} for it in picked]
    # 验证题：要求档高的技能追加开放题，按权重优先
    hard = [it for it in picked if (as_level(it.get("required_level")) or 0) >= VERIFY_LEVEL]
    for it in hard[:verify_n]:
        out.append({**it, "_item_type": "open", "_reason": "verify_high_bar"})
    return out


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
    verify.sort(key=lambda x: -(as_weight(x.get("weight"))))

    # 未考过的技能 → 出选择题，权重优先、同大类限额
    cat_used: dict[str, int] = {}
    for g in graded:
        it = by_key.get(str(g.get("skill_key") or ""))
        if it:
            c = str(it.get("category") or "未分类")
            cat_used[c] = cat_used.get(c, 0) + 1
    fresh: list[dict[str, Any]] = []
    for it in sorted(items, key=lambda x: -(as_weight(x.get("weight")))):
        key = str(it.get("skill_key"))
        if asked.get(key):
            continue
        cat = str(it.get("category") or "未分类")
        if cat_used.get(cat, 0) + sum(
            1 for f in fresh if str(f.get("category") or "未分类") == cat
        ) >= MAX_PER_CATEGORY:
            continue
        fresh.append({**it, "_item_type": "choice", "_reason": "coverage"})

    # 大类限额是为了让雷达维度分散，但当它把候选卡到不够出题时必须放开——
    # 「计算机程序设计员」这类岗位 4 项技能几乎同属一类，卡住就只能出 3 题
    if len(verify) + len(fresh) < size:
        picked_keys = {str(x.get("skill_key")) for x in fresh}
        for it in sorted(items, key=lambda x: -(as_weight(x.get("weight")))):
            key = str(it.get("skill_key"))
            if asked.get(key) or key in picked_keys:
                continue
            fresh.append({**it, "_item_type": "choice", "_reason": "coverage_relaxed"})
            picked_keys.add(key)
            if len(verify) + len(fresh) >= size:
                break

    # 验证题不超过本批一半：主线仍是扩大覆盖
    out = verify[: max(1, size // 2)] + fresh
    return out[:size]


def estimate_total(items: list[dict[str, Any]]) -> dict[str, Any]:
    """**出题前**按岗位技能构成算出这次要考多少题，之后一直考到这个数为止。

    此前用的是「多重收敛条件先到先停」，权重集中的岗位 3 题就能凑够 80% 覆盖率，
    学员体感是「怎么才问三道就结束了」。改成先定题数、出满为止，进度也才有意义。

    题数 = 覆盖题 + 验证题：

    - **覆盖题**：按权重降序累计到 COVERAGE_TARGET 所需的技能数；再往上取
      RADAR_MIN_SKILLS（报告雷达至少要 3 根轴）与 MIN_COVER_QUESTIONS 的较大值，
      并以技能总数封顶（只有 4 项技能的岗位出不了 8 道覆盖题）
    - **验证题**：要求档 ≥ VERIFY_LEVEL 的技能属于高标准项，自评达标必须拿证据，
      每项一道开放追问，上限 OPEN_PER_RUN

    返回 {total, cover, verify, reason}，reason 供排查「为什么是这个题数」。

    **口径**：`cover`/`verify` 是按岗位技能构成算出的**需求量**，没有夹 MAX_QUESTIONS
    （留着它们才能看出「本想考 16 项，被上限压到 9 项」）；`total` 才是这场的题量，
    受 MAX_QUESTIONS 封顶。排题只能以 `total` 为预算，两类题的配额由
    `plan_all_skills` 调 `_fit_budget` 按 cover:verify 的比例分。
    """
    n = len(items)
    if not n:
        return {"total": 0, "cover": 0, "verify": 0, "reason": "该岗位没有技能构成"}

    ranked = sorted(items, key=lambda x: -(as_weight(x.get("weight"))))
    total_w = sum(as_weight(i.get("weight")) for i in ranked)
    cover = 0
    if total_w > 0:
        acc = 0.0
        for it in ranked:
            cover += 1
            acc += as_weight(it.get("weight"))
            if acc / total_w >= COVERAGE_TARGET:
                break
    else:
        cover = min(n, MIN_COVER_QUESTIONS)
    cover = min(n, max(cover, RADAR_MIN_SKILLS, MIN_COVER_QUESTIONS))

    # 高要求档的技能要验证：要求 L4/L5 却只凭选择题定档，虚高无从发现
    hard = [
        it for it in ranked[:cover]
        if (as_level(it.get("required_level")) or 0) >= VERIFY_LEVEL
    ]
    verify = min(len(hard), OPEN_PER_RUN)

    total = max(MIN_QUESTIONS, min(MAX_QUESTIONS, cover + verify))
    return {
        "total": total,
        "cover": cover,
        "verify": verify,
        "reason": (
            f"{n} 项技能中，按权重覆盖 {COVERAGE_TARGET:.0%} 需 {cover} 项"
            f"（不少于 {max(RADAR_MIN_SKILLS, MIN_COVER_QUESTIONS)} 项）；"
            f"其中要求档≥L{VERIFY_LEVEL} 的 {len(hard)} 项需追问验证，取 {verify} 道"
        ),
    }


def should_stop(
    items: list[dict[str, Any]],
    *,
    asked_total: int,
    batches: int,
    graded: list[dict[str, Any]],
    next_batch: list[dict[str, Any]],
    target_total: int = 0,
) -> tuple[bool, str]:
    """收敛判定：**出满预估题数才停**，另有两道兜底防止出不满时卡死。"""
    if target_total and asked_total >= target_total:
        return True, f"已出满预估题数 {target_total}"
    if asked_total >= MAX_QUESTIONS:
        return True, f"达到题量上限 {MAX_QUESTIONS}"
    if batches >= MAX_BATCHES:
        return True, f"达到批次上限 {MAX_BATCHES}"
    if not next_batch:
        return True, "没有可考的新技能"
    return False, ""


# ── 降级题（网关不可用） ─────────────────────────────────────


def fallback_choice(item: dict[str, Any]) -> dict[str, Any]:
    """自评题。考核力弱于 SJT，仅保证流程可跑通，报告里以 variant 区分。"""
    labels, behaviors = label_map(), behavior_map()
    req = (as_level(item.get("required_level")) or 0)
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
        "required_level": (as_level(item.get("required_level")) or 0) or None,
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
        "required_level": (as_level(item.get("required_level")) or 0) or None,
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
        "required_level": (as_level(item.get("required_level")) or 0) or None,
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
