"""把岗位技能编排成 e-ai-spaces 的导入 payload。**纯函数，不碰 DB、不发请求。**

编排算法从 `biz_store.generate_path()` 移植而来——那套「大类分阶段 + 短板优先 +
课程挂载」的逻辑来自本项目的图谱数据，e-ai-spaces 不具备，是真实业务价值。
移植时只砍掉落库与进度管理：本地不再存路径副本，进度真源在对方。

纯函数是幂等的前提
------------------
契约的 409 是「同 external_path_id 但内容变化」。只要输入固定（一次诊断的结果
落库后就不再变），这里的输出就必须逐字节一致，否则超时重推会撞 409。
所以：不读时钟、不用集合迭代序、不掺随机数——排序键全部显式给定。

移植时修掉的一个既有 bug
------------------------
原 `_prio()` 用 `gaps[].skill_name` / `gaps[].skill_id` 判断某技能是不是短板，
但报告里的 gaps 项**根本没有这两个键**（实际是 `skill_key`）。于是 `is_gap` 恒为 1，
「短板优先」十几个月来一直没生效，排序退化成纯权重序。这里按 `skill_key` 对齐。
"""
from __future__ import annotations

from typing import Any

from backend.kg.pg_store.skill_level_meta import level_name
from backend.kg.pg_store.skill_taxonomy import category_rank
from backend.learningplan.schema import (
    MAX_MINUTES,
    MAX_PHASES,
    MAX_SKILLS_PER_TASK,
    ImportPayload,
    Phase,
    Resource,
    Skill,
    Task,
    sanitize_id,
)

# 取多少个技能进路径。原实现取 12 再截 8，这里保持同一结果。
SKILL_POOL = 12
MAX_SKILLS_IN_PATH = 8

# 建议耗时：按目标档位估算。**不是真实课时**——图谱里没有课时数据，
# 这只是给学员一个量级感，`total_duration` 的「约 N 周」也由它推出来。
_DURATION_BY_LEVEL = {1: 30, 2: 45, 3: 60, 4: 90, 5: 120}
_DEFAULT_DURATION = 45

# 首次导入时，把诊断已达标的技能标成 completed。
# 契约对 `completed` 的定义就是「诊断认定已达标的任务」，故取 True；
# 代价是学员在 e-ai-spaces 首屏即看到部分进度（方案文档 4.4 列为待产品确认）。
# 若产品要「首次全 false，仅换代结转」，把这里改 False 即可，其余逻辑不动。
CARRY_OVER_COMPLETED_ON_FIRST_IMPORT = True

# 分类 code 的兜底值。展示名从字典表取（category_name_of），别在这里写中文 ——
# 这里曾是 "未分类" 字面量，而 skill.category 存的是 code，
# 学习计划的阶段名就会显示成 "TECH"、"OPERATE" 给学员看。
from backend.kg.pg_store.skill_taxonomy import FALLBACK_CODE as UNCATEGORIZED
from backend.kg.pg_store.skill_taxonomy import name_of as category_name_of
from backend.kg.pg_store.skill_taxonomy import to_code as category_to_code


def external_path_id_for(region: str, session_id: int) -> str:
    """幂等键：一次诊断 = 一条路径。

    重新测评天然产生新 session_id → 新路径 id，正合契约「重新测评必须换新 ID」；
    同一 session 重复调用则幂等命中，对方回 `created:false`。

    **构造规则不对外暴露**（不进 Swagger、不进接口文档）：它只在本服务与
    e-ai-spaces 之间流转，前端既不构造也不解析。保持这个性质，将来改前缀或
    加租户维度才不会被既有调用方绑住。
    """
    return sanitize_id(f"vockg-{region}-s{session_id}", fallback=f"vockg-s{session_id}")


def _report_items(report: dict[str, Any]) -> list[dict[str, Any]]:
    """报告里的逐技能明细。

    `items` 是全量（含达标、未达标、未测），`gaps` / `strengths` 只是它的两个
    子集。判「达标」必须看 items —— 从 gaps 里找 ok=True 永远找不到。
    老报告可能没有 items，退回两个子集的并集。
    """
    items = report.get("items")
    if isinstance(items, list) and items:
        return [x for x in items if isinstance(x, dict)]
    merged = (report.get("gaps") or []) + (report.get("strengths") or [])
    return [x for x in merged if isinstance(x, dict)]


def _gap_keys(report: dict[str, Any]) -> set[str]:
    """判定为「未达标」的技能 key。见模块 docstring 里说的既有 bug。"""
    return {
        k for x in _report_items(report)
        if not x.get("ok") and (k := str(x.get("skill_key") or "").strip())
    }


def _met_keys(report: dict[str, Any]) -> set[str]:
    """已达标且真的测过的技能 key —— `completed` 的来源。

    三个条件缺一不可：

    - `ok` 为真；
    - `measured_level` 非空 —— 未测过的技能 `ok` 也可能为真（要求档为 0 时），
      把没测过的标成"已完成"，学员在对方首屏看到的进度就是假的；
    - `scorable` **不为 False**。注意判据是 `is not False` 而不是取真值：
      这个字段是无基准改造时才加的，**历史报告里是 `None`**，
      按真值判会把所有老报告的达标项全判成未达标。
    """
    out: set[str] = set()
    for x in _report_items(report):
        k = str(x.get("skill_key") or "").strip()
        if k and x.get("ok") and x.get("measured_level") is not None \
                and x.get("scorable") is not False:
            out.add(k)
    return out


def _skill_key_of(s: dict[str, Any]) -> str:
    for f in ("skill_key", "skill_name", "name"):
        v = str(s.get(f) or "").strip()
        if v:
            return v
    return "技能"


def _skill_name_of(s: dict[str, Any]) -> str:
    """展示名 —— 和 `_skill_key_of` 分工：**它管身份，这个管上屏**。

    `skill_key` 自 2026-08-19 起是 ASCII code（`SK` + md5 前 10 位），拿它当任务名
    推给学习计划服务，学员在对方页面上看到的就是「补齐技能：SK208ab276b3（目标 专家）」。
    这个错在本服务侧完全看不见 —— 我们只发不收，落库的 `path_snapshot` 也是照发的原样。

    退回 key 而不是留空：指向已删技能的历史计划仍要能生成，显示成一串 code
    也比任务没有名字好。
    """
    for f in ("skill_name", "name"):
        v = str(s.get(f) or "").strip()
        if v:
            return v
    return _skill_key_of(s)


def _weight_of(s: dict[str, Any]) -> float:
    try:
        w = float(s.get("weight") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return w if w > 0 else 0.0


def _order_skills(skills: list[dict[str, Any]], gap_keys: set[str]) -> list[dict[str, Any]]:
    """短板优先，其次权重降序。同分时按 skill_key 兜底，保证顺序确定。"""
    def key(s: dict[str, Any]) -> tuple:
        k = _skill_key_of(s)
        return (0 if k in gap_keys else 1, -_weight_of(s), k)

    return sorted(skills, key=key)[:MAX_SKILLS_IN_PATH]


MIN_PHASE_WEIGHT = 0.01   # 契约要 phase_weight > 0，0 会被 422


def _phase_weights(sums: list[float]) -> list[float]:
    """阶段权重归一到 Σ=100，且每个都 > 0。

    前 n-1 个 round(.,2)，**最后一个取 100-已分配**——各自 round 会让总和落在
    99.98 这种位置，契约的 Σ∈[99,101] 虽容得下，但没必要留这个抖动。

    两个退化情况都必须兜住，它们都来自脏数据而不是代码错误，
    不该让学员因此拿不到计划：

    - **整列权重为 0** → 阶段间均分；
    - **某个阶段的技能权重全是 0，别的阶段不是** → 归一后它是 0.0，
      被对方 422 拒。给它 `MIN_PHASE_WEIGHT`，差额从最大的那个阶段扣。
    """
    n = len(sums)
    if n == 1:
        return [100.0]

    total = sum(sums)
    if total <= 0:
        base = round(100.0 / n, 2)
        out = [base] * (n - 1) + [round(100.0 - base * (n - 1), 2)]
    else:
        out = [round(w / total * 100.0, 2) for w in sums[:-1]]
        out.append(round(100.0 - sum(out), 2))

    for i, w in enumerate(out):
        if w >= MIN_PHASE_WEIGHT:
            continue
        need = MIN_PHASE_WEIGHT - w
        donor = max(range(n), key=lambda j: out[j])
        if out[donor] - need < MIN_PHASE_WEIGHT:
            break                      # 匀不动了（阶段过多），交给模型校验去报
        out[donor] = round(out[donor] - need, 2)
        out[i] = MIN_PHASE_WEIGHT
    return out


def build_payload(
    *,
    session_id: int,
    region: str,
    occupation_id: str,
    occupation_name: str,
    skills: list[dict[str, Any]],
    report: dict[str, Any],
    courses_by_key: dict[str, list[dict[str, Any]]] | None = None,
    revision_of: str | None = None,
) -> ImportPayload:
    """构造导入 payload。校验不过直接抛 `pydantic.ValidationError`（=本地 bug）。

    `courses_by_key` 由调用方先查好喂进来（`courses.courses_for_skill_keys`），
    这里不碰 DB —— 保证可单测、可离线重放。
    """
    courses_by_key = courses_by_key or {}
    gap_keys = _gap_keys(report)
    met_keys = _met_keys(report) if CARRY_OVER_COMPLETED_ON_FIRST_IMPORT else set()
    ordered = _order_skills(skills, gap_keys)
    if not ordered:
        raise ValueError("该岗位没有可用的技能构成，无法生成学习计划")

    # 阶段：按技能大类分组，顺序沿用国标「职业功能」推进顺序（安全环保 → 作业准备
    # → 操作加工 → 检修质检 → 技术管理 → 培训指导），与技能图谱的分区顺序同源。
    # 分组键先过 to_code：库里 category 还留着中文写法（采集脚本/直连改库/历史数据），
    # 不归一就会出现「作业准备」与「操作与加工」分成两个阶段、而展示名都叫「操作与生产」。
    # to_code 的 docstring 写的就是这条：读写两侧都要走它，只在一处映射另一处就会分家。
    def _cat_of(sk: dict[str, Any]) -> str:
        return category_to_code(sk.get("category") or UNCATEGORIZED)

    all_cats = sorted({_cat_of(s) for s in ordered},
                      key=lambda c: (category_rank(c), c))
    # 超上限就把尾部并进最后一个保留阶段，而不是丢技能。
    # （当前 MAX_SKILLS_IN_PATH=8 < MAX_PHASES=12，走不到这里；留作防御。）
    kept = all_cats[:MAX_PHASES]
    remap = {c: (c if c in kept else kept[-1]) for c in all_cats}

    grouped: dict[str, list[dict[str, Any]]] = {c: [] for c in kept}
    for s in ordered:
        grouped[remap[_cat_of(s)]].append(s)
    # 空阶段必须剔除：契约要求每阶段至少 1 个任务，空的会被 422
    cats = [c for c in kept if grouped[c]]

    seq = 0
    built: list[tuple[str, list[Task]]] = []
    weight_sums: list[float] = []
    total_minutes = 0

    for cat in cats:
        tasks: list[Task] = []
        wsum = 0.0
        for s in grouped[cat]:
            seq += 1
            key = _skill_key_of(s)
            req = s.get("required_level")
            try:
                req_int = int(req) if req is not None else 0
            except (TypeError, ValueError):
                req_int = 0
            minutes = min(_DURATION_BY_LEVEL.get(req_int, _DEFAULT_DURATION), MAX_MINUTES)
            total_minutes += minutes
            wsum += _weight_of(s)

            disp = _skill_name_of(s)
            name = f"补齐技能：{disp}"
            if req_int:
                # 档位名只能从 skill_level_meta 读，禁止硬编码（CLAUDE.md）
                name += f"（目标 {level_name(req_int)}）"

            res: list[Resource] = []
            for c in courses_by_key.get(key, []):
                url = str(c.get("source_url") or "").strip()
                title = str(c.get("name") or "").strip()
                # 契约要绝对 http(s)；空值与相对路径直接丢弃而不是硬塞
                if title and url.lower().startswith(("http://", "https://")):
                    res.append(Resource(title=title[:256], url=url[:1024]))

            sid = str(s.get("id") or "").strip() or key
            desc = str(s.get("desc") or "").strip()
            tasks.append(
                Task(
                    external_task_id=sanitize_id(
                        f"s{session_id}-t{seq}", fallback=f"t{seq}"
                    ),
                    name=name[:256],
                    description=desc[:1024] or None,
                    estimated_minutes=minutes,
                    completed=key in met_keys,
                    # skill_id 是身份（bundle id），skill_name 是展示名 —— 这里曾经
                    # 两个都塞 key，对方拿 skill_name 上屏就是一串 code
                    skills=[Skill(skill_id=sid[:128], skill_name=disp[:256])][
                        :MAX_SKILLS_PER_TASK
                    ],
                    resources=res,
                )
            )

        weight_sums.append(wsum)
        built.append((cat, tasks))

    # 权重先算完再建 Phase：Pydantic 默认不校验赋值，先建后改会让
    # phase_weight=0 这种非法值绕过 gt=0，一路带到对方那里才 422。
    phase_models = [
        Phase(
            external_phase_id=f"stage-{i}",
            # 阶段名给学员看，用中文展示名而不是分类 code
            phase_name=category_name_of(cat)[:256],
            phase_weight=w,
            tasks=tasks,
        )
        for i, ((cat, tasks), w) in enumerate(
            zip(built, _phase_weights(weight_sums)), start=1
        )
    ]

    weeks = max(1, round(total_minutes / 300))   # 按每周 5 小时估
    return ImportPayload(
        external_path_id=external_path_id_for(region, session_id),
        external_job_id=occupation_id[:128],
        job_name=(occupation_name or occupation_id)[:256],
        title=f"{occupation_name or occupation_id} 岗位自适应攻关路径"[:256],
        goal=(occupation_name or occupation_id)[:1024],
        total_duration=f"约 {weeks} 周",
        revision_of_external_path_id=revision_of,
        phases=phase_models,
    )
