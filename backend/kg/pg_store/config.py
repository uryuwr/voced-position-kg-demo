"""PostgreSQL connection config from env + 读侧脏值口径（状态可见性、档位/权重强转）。"""
from __future__ import annotations

import math
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from backend.kg.paths import ROOT

# 配置真源是 backend/.env（见 backend/settings.py）。
# 这里不能自己 load_dotenv(ROOT/".env")——那读的是**仓库根** .env，独立部署时根本不存在，
# 单独 import 本模块的脚本（如 python -m backend.kg.pg_store.migrate）会拿不到 DATABASE_URL。
import backend.settings  # noqa: F401  仅为触发 .env 加载

# Default local docker: docker run ... -e POSTGRES_USER=voced ...
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://voced:<your-password>@localhost:5432/voced_kg",
)
SQLITE_PATH = Path(os.getenv("SQLITE_PATH", str(ROOT / "data" / "graph" / "kg.sqlite")))
if not SQLITE_PATH.is_absolute():
    SQLITE_PATH = ROOT / SQLITE_PATH

# API / migrate default region scope
DEFAULT_REGION = os.getenv("KG_REGION", "CN")


# ── 状态可见性三级规则 ────────────────────────────────────────
# archived  = 逻辑删除：**任何接口都不返回**，只留在库里，恢复需直接改库
# draft      = 草稿：仅管理台可见
# disabled   = 停用：仅管理台可见（停用后可再发布）
# published  = 发布态：前台与管理台都可见
PUBLIC_STATUSES = ("published",)
ADMIN_STATUSES = ("published", "draft", "disabled")
ARCHIVED_STATUS = "archived"


def edge_published(alias: str = "e") -> str:
    """【前台】边可见性：仅 published。

    `status` 为 NULL 的历史数据视为 published。
    读路径的每个 kg_edge 查询都要带上，否则归档形同虚设——只过滤节点挡不住边，
    比如 industry→industry 的 parent_of 两端都是正常节点。
    """
    return f"COALESCE({alias}.status, 'published') = 'published'"


def edge_not_archived(alias: str = "e") -> str:
    """【管理台】边可见性：除 archived 外都可见（含 draft / disabled）。"""
    return f"COALESCE({alias}.status, 'published') <> '{ARCHIVED_STATUS}'"


def node_published(alias: str = "") -> str:
    """【前台】节点可见性：仅 published。alias 为空时用于无别名的 kg_node 查询。"""
    col = f"{alias}.status" if alias else "status"
    return f"COALESCE({col}, 'published') = 'published'"


def node_not_archived(alias: str = "") -> str:
    """【管理台】节点可见性：除 archived 外都可见。"""
    col = f"{alias}.status" if alias else "status"
    return f"COALESCE({col}, 'published') <> '{ARCHIVED_STATUS}'"


# ── 草稿态：同表两行（方案 docs/方案-管理台草稿态与发布.md）────────────
# 一条记录最多两行：线上行 is_draft=false，草稿行 is_draft=true 且 status 恒为 'draft'。
# 前台按 status='published' 读，草稿自动被挡；管理台口径是 `<> 'archived'`，
# **两行都满足**，所以管理台的每个查询都要再拼一个 prefer_draft。
DRAFT_STATUS = "draft"


def prefer_draft(alias: str = "") -> str:
    """【管理台】同一 id 只留一行，有草稿就取草稿行。

    实现是反连接而不是方案里写的 `DISTINCT ON (id) … ORDER BY id, is_draft DESC`：
    `DISTINCT ON` 要求 ORDER BY 以去重列开头，而这 20 多处读路径全都有自己的排序
    （created_at DESC / sort_order / name / 权重…）和 GROUP BY，套 DISTINCT ON 就得
    每处包一层子查询、把原来的 ORDER BY 挪进去。反连接是纯 WHERE 片段，语义等价
    （草稿存在取草稿，否则取线上行），能原地 AND 进任何一处查询，也走
    `idx_kg_node_draft` 部分索引。

    **alias 缺省时用表名限定**，不能写成裸 `id`：子查询里 `kg_node __pd` 也有 id 列，
    内层作用域优先，`__pd.id = id` 会被解析成恒真，去重静默失效。
    """
    a = alias or "kg_node"
    return (
        f"({a}.is_draft OR NOT EXISTS ("
        f"SELECT 1 FROM kg_node __pd WHERE __pd.id = {a}.id AND __pd.is_draft))"
    )


def prefer_draft_edge(alias: str = "e") -> str:
    """【管理台】边的 prefer_draft，见 `prefer_draft`。"""
    a = alias or "kg_edge"
    return (
        f"({a}.is_draft OR NOT EXISTS ("
        f"SELECT 1 FROM kg_edge __pde WHERE __pde.id = {a}.id AND __pde.is_draft))"
    )


def has_draft_edges(alias: str = "") -> str:
    """这个节点的**发布单元**里有没有草稿边（`unit_id` 指向它）。

    「这条记录有未发布的改动」不等于「有草稿节点行」：改技能构成只产生草稿**边**。
    显示口径（`unit_draft_kinds`）与筛选口径（`list_nodes` 的 `status=draft`）
    必须是同一份判断 —— 上一版显示对了、筛选漏了，运营点「只看草稿」全空。
    所以这条 EXISTS 是唯一定义，两处都引它。
    """
    a = alias or "kg_node"
    return (
        f"EXISTS (SELECT 1 FROM kg_edge __de "
        f"WHERE __de.is_draft AND __de.unit_id = {a}.id)"
    )


def online_only(alias: str = "") -> str:
    """【前台】只读线上行。

    给「关联查名字」这类**没有状态过滤**的 JOIN 用（`LEFT JOIN kg_node sn ON sn.id=e.src_id`）：
    这种 JOIN 靠不上 status 不变量，一旦端点节点有草稿行，一条边会 JOIN 出两行 ——
    列表里同一条边出现两次、total 虚高，而且显示的是草稿里的新名字。
    这类查询前台与管理台都会走，前台必须钉在线上行上，否则「编辑期间前台逐字节不变」不成立。
    """
    a = alias or "kg_node"
    return f"NOT {a}.is_draft"


def online_only_edge(alias: str = "e") -> str:
    """【前台】边只读线上行，见 `online_only`。"""
    a = alias or "kg_edge"
    return f"NOT {a}.is_draft"

# 要登录报名 / 按学期开课的平台 —— 见 learnable_course 说明。
# 展示层（skill_aggregate 判 kind=enroll）直接 import 这个常量，不要另抄一份：
# 过滤层与展示层各写一份的话，只同步了一处就会出现「列表里没有、详情里标真课」。
ENROLL_SOURCES = ("ICOURSE163", "XUETANGX")


def learnable_course(alias: str = "n") -> str:
    """【前台】课程节点是否**真的能学** —— 学员端所有资源查询都必须带上这个条件。

    库里 17482 个 course 节点中 **15960 个（91%）是教育部课标的「课目名称」**
    （`role=curriculum_catalog`、`playable=false`），它们的 `source_url` 全指向
    「职业教育专业教学标准」的大类列表页 —— 点开是「农林牧渔大类 / 资源环境与安全
    大类 …」，与具体技能毫无关系，学员根本没法用。

    这批数据本身没错：它是**专业培养方案的课程体系**，在「专业开设哪些课」
    （`major -offers_course-> course`）语境下是对的，不该删。错的是把它当成
    「可学资源」推给学员。

    「能学」的完整口径是**点开当场就能开始学**：免登录、免报名、无开课周期。
    2026-08-18 按这条尺子把中国大学MOOC / 学堂在线也划了出去 —— 它们是真课程，
    但要登录报名，且按学期开课，往期课程结束后非选课用户点进去只剩介绍页。
    那批节点已 `archived`（`scripts/archive_courses.py`），`node_published()`
    就挡住了；这里再挡一道，是为了 `--restore` 后语义仍然成立。

    判定优先认 attrs —— 将来任何来源只要标了 `playable=false` 或
    `role=curriculum_catalog`，一律不进学员资源列表；`enroll_required=true`
    同理。`_ENROLL_SOURCES` 是给历史数据兜底的，那批节点入库时还没有这个字段。

    ⚠ 这是**唯一判定源**。别在各接口里再抄一份 `source_system <> 'MOE_CN'`：
    本轮已经因为「同一判定多处各写一份」出过四次问题（清单脚本只认 ICOURSE163、
    新增 OFFICIAL_DOCS 没同步常量……），症状都是「数据在库里但页面不对」。
    """
    a = f"{alias}." if alias else ""
    attrs_json = (
        f"(CASE WHEN {a}attrs IS NULL OR btrim({a}attrs) = '' "
        f"THEN NULL ELSE {a}attrs::json END)"
    )
    enroll_srcs = ", ".join(f"'{s}'" for s in ENROLL_SOURCES)
    return (
        f"COALESCE({attrs_json}->>'role', '') <> 'curriculum_catalog' "
        f"AND COALESCE({attrs_json}->>'playable', 'true') <> 'false' "
        f"AND COALESCE({attrs_json}->>'enroll_required', 'false') <> 'true' "
        f"AND COALESCE({a}source_system, '') NOT IN ({enroll_srcs})"
    )


def attrs_level_int(alias: str = "n") -> str:
    """技能产品档 `attrs.level` → int，**脏值取 NULL 而不是让整条查询炸**。

    attrs 是 TEXT 列，`attrs.level` 没有任何数据库约束。裸写 `(attrs::json->>'level')::int`
    时，只要库里有**一行** level 是 `"L3"`、`"三级"`、`"3.5"` 这类值，
    PostgreSQL 就抛 `invalid input syntax for type integer`，
    整个列表接口 500 —— 一行脏数据打死整页，和当初 weight_sum 那个 500 同形。

    写入侧已在 write.py 校验（1–5 的整数），这里是读侧兜底：
    采集脚本、历史数据、直连改库都绕得过应用层校验，读路径必须自己站得住。

    **整数值的浮点（`3.0`）要收成 3**，与 Python 侧 `as_level` 同一套规则。
    原来的 `^[0-9]+$` 把它判成脏值取 NULL，于是那个岗位的所有技能都成了「无基准」，
    `match_score` 变 null、页面说「尚未配置能力要求」而库里明明配了（BUG-8）。
    两边规则不一致更糟：同一行数据 SQL 说没有、Python 说有，两个页面两个结论。
    真小数（2.5）仍取 NULL —— 档位是枚举，没有 2.5 档。
    """
    expr = f"({alias}.attrs::json->>'level')"
    return (
        f"CASE WHEN {expr} ~ '^[0-9]+(\\.0+)?$' "
        f"THEN trunc({expr}::numeric)::int END"
    )


# ── Python 侧脏值口径：与上面 attrs_level_int 的 SQL 侧同一套规则 ──────────────
#
# 为什么放在这里：`attrs.level` / `kg_edge.weight` 一路从 SQL 流到报告、匹配度、出题
# 三条读路径，此前**每条路径各自解析**——报告侧 `float(w or 0)` 遇 `"abc"` 抛
# ValueError 打死整份报告，画像侧 `isinstance(w,(int,float))` 把 `"0.5"` 读成 0，
# 同一份脏数据在两个页面上给出两个数字（BUG-5）。解析规则必须只有一处。

LEVEL_MIN, LEVEL_MAX = 1, 5   # 产品档 L1–L5（档位真源见 skill_level_meta.py）


def as_level(v: Any) -> int | None:
    """产品档 → 1–5 的 int；**越界或不可解析取 None（视作缺失），不夹取、不抛错**。

    三条来路都不干净：`required_level` 来自 `kg_edge` 指向的 `skill_level` 节点
    `attrs.level`（无约束 TEXT，`attrs_level_int` 的正则只挡非数字，`9` 会原样穿过来）；
    `measured_level` 来自选项 level 与简历画像（模型给几就是几）；写侧
    `write._assert_attrs_sane` 校验过，但采集脚本与直连改库绕得过应用层。

    取 None 而不是夹到 5：把 9 当成 L5 会凭空给出「已达标」的结论。
    调用方**必须**把 None 当「没有基准 / 没有证据」处理，而不是当 0 分参与加权——
    见 `report.build_report` 与 `biz_store.match_with_profile` 的 scorable 分支。
    """
    if v is None or isinstance(v, bool):
        return None
    # **整数值的浮点要收成整数**（BUG-8）：`int(str(3.0))` = `int("3.0")` → ValueError
    # → None，于是 `3.0` 被判成「没有要求档」，那个岗位的所有技能都成了「无基准」，
    # `match_score` 变 null、页面显示「该岗位尚未配置能力要求」，而库里明明配了 ——
    # 静默给出错结论，比抛异常难查得多。
    # psycopg 把 numeric 读成 `Decimal` 是常态，JSON 里 `"level": 3.0` 也完全合法。
    # 真小数（2.5）仍取 None：档位是枚举，没有 2.5 档。
    try:
        d = Decimal(str(v).strip())
    except (TypeError, ValueError, ArithmeticError):
        return None
    # `inf` / `nan` 能被 Decimal 收下，但 to_integral_value / int() 会抛
    # OverflowError、InvalidOperation —— 而这个函数的契约是**任何输入都不抛错**
    if not d.is_finite():
        return None
    if d != d.to_integral_value():
        return None
    lv = int(d)
    return lv if LEVEL_MIN <= lv <= LEVEL_MAX else None


def as_weight(v: Any) -> float:
    """`kg_edge.weight` → 非负有限 float；脏值取 0.0，**任何输入都不抛错**。

    口径（两条读路径必须一致，否则同一份数据算出两个匹配度）：

    - `0.5` / `"0.5"` / `Decimal("0.5")` → 0.5 —— JSON/TEXT 里数字写成字符串很常见，
      该解析就解析；此前报告侧认 `"0.5"`、画像侧认 0.0，脏数据上口径就分家了
    - `"abc"` / `""` / `[]` / `{}` / `None` / `True` → 0.0（`float("abc")` 曾让整份报告 500）
    - `nan` / `inf` → 0.0：参与加权会把匹配度污染成 nan，前端显示成空白
    - 负数 → 0.0：权重是占比，负权重能把加权分算成负数（「匹配度 -33%」）
    """
    if v is None or isinstance(v, bool):
        return 0.0
    try:
        w = float(v)
    except (TypeError, ValueError):
        return 0.0
    return w if math.isfinite(w) and w > 0 else 0.0


# 算分结论的词表：一种「能用」、一种「能用但只能当参考」、四种「算不出来」。
# 前端据此决定显示数字、显示数字+提示、还是只显示说明文案，
# 口径与 `PositionMatchOut.source` 的 no_overlap / none 一致：**不要用 0% 冒充结论**。
#
# **顺序即契约**：`AssessmentReportOut.score_status` 的 Literal 必须与这里逐项同序，
# 由 tests/unit/test_pg_guards.py::test_契约里的取值与这里的词表同源 锁住 ——
# 加值时两处一起加，否则契约与实现分家、前端会漏处理某个值。
SCORE_STATUSES = (
    "ok",
    "partial_baseline",
    "no_skills",
    "no_baseline",
    "no_weight",
    "no_evidence",
)

# 「岗位配置不全」的降级阈值：缺要求档的技能权重占岗位全部技能权重**超过**这个百分比时，
# 分数照给（已配置的那部分是真实依据，不该丢），但 `score_status` 降级成 partial_baseline。
#
# 为什么是 30 而不是别的数：
#
# 1. 匹配度的可信度就是「有基准的权重占比」。缺 30% 权重时，真实分数的不确定区间宽达
#    30 分（缺的那部分从全不达标到全达标都可能），已经跨过展示上惯用的两个 20 分区间
#    （<60 待提升 / 60–80 基本匹配 / >80 高度匹配）——数字连排序意义都不剩了，
#    必须明确告诉学员「这个数只能参考」。
# 2. 岗位 `requires` 边的权重 Σ≈1、单项典型 0.1–0.3（见 CLAUDE.md 边模型），
#    缺口过 30% 意味着**不止一项主要技能**没有基准，不再是可以忽略的零星缺配。
# 3. 阈值再低（如 10%）会让几乎每个岗位都挂上提示——权重归一容差本身就是 ±15%
#    （`weight_sum_ok` 的 0.85–1.15），零星缺配长期存在，满屏提示等于没有提示。
#    再高（如 60%）则「82% 没配、只按 18% 算出 50%」这类最容易误读的岗位会漏过，
#    而这正是引入本档要解决的场景。
#
# 阈值必须留在服务端：放前端就成了每个页面各定一次的魔数，迟早不一致。
PARTIAL_BASELINE_PCT = 30.0


def is_partial_baseline(no_baseline_weight: float | None) -> bool:
    """基准缺口是否已大到「分数只能当参考」。

    **边界：正好 30.0% 不算超过**（严格大于），与 `PARTIAL_BASELINE_PCT` 的注释、
    契约描述里的「超过 30%」字面一致。

    比较用的是响应里那个**已四舍五入到 1 位小数**的 `no_baseline_weight`，
    而不是未舍入的原始占比：否则会出现响应里写着 `no_baseline_weight=30.0`、
    状态却是 partial_baseline 的自相矛盾，前端对着两个字段无法解释给学员。

    `None`（历史数据缺该字段）当作「不知道缺多少」，不降级：宁可少提示，
    不要凭空给一个没有依据的警告。
    """
    return no_baseline_weight is not None and no_baseline_weight > PARTIAL_BASELINE_PCT


def degrade_for_baseline_gap(status: str, no_baseline_weight: float | None) -> str:
    """`ok` + 基准缺口超阈值 → `partial_baseline`；其余状态原样返回。

    只降级 `ok`：另外四种本来就已经在说「这个数不能当结论」，再套一层反而模糊焦点
    （`no_evidence` 缺的是证据、`no_weight` 是脏权重，都比基准缺口更该先说）。
    `no_baseline` 是本档的极端情形（缺口 100%），保留它自己的值——那时连分数都没有。

    报告侧（`report.build_report`）与画像侧（`biz_store.match_with_profile`）都必须
    调它，两处口径分家的话，同一个岗位在报告页和列表页会一个带提示一个不带。
    """
    if status == "ok" and is_partial_baseline(no_baseline_weight):
        return "partial_baseline"
    return status


def weighted_score(
    *,
    skill_total: int,
    scorable_total: int,
    total_w: float,
    got_w: float,
    has_evidence: bool,
) -> tuple[float | None, str]:
    """加权达标率 + 「为什么算不出来」。**报告侧与画像侧共用，不许各写一份。**

    `scorable_total` 只数「要求档在 1–5 内」的技能：库内 608 个有技能构成的岗位里
    490 个（80%）要求档全缺（老国标 skill_level 节点只有 `attrs.level_code`、
    没有产品档 `attrs.level`），这些岗位算不出任何达标率。

    返回 None 而不是 0.0 的只有 `no_baseline` 一种：0% 会被学员读成「完全不匹配」，
    而真相是「这个岗位没配能力要求」。其余三种沿用既有的 0.0 —— 它们的调用方
    （`routes_student.student_position_match` 的级联、报告页的 coverage）已经在
    用别的字段把「未评估」表达出去了。

    这里只判「算不算得出来」，**不判「算出来的数够不够可信」**：部分缺基准
    （`partial_baseline`）要看缺口占多少权重，那个占比在调用方才算得出来，
    所以由调用方紧接着过一遍 `degrade_for_baseline_gap()`。
    """
    if not skill_total:
        return 0.0, "no_skills"          # 岗位没有技能构成，无从算起
    if not scorable_total:
        return None, "no_baseline"       # 有技能但一项都没配可信要求档 → 无法评分
    if not total_w:
        return 0.0, "no_weight"          # 可评分技能的权重全为 0（脏数据），算不出加权分
    if not has_evidence:
        return 0.0, "no_evidence"        # 有基准有权重，但一项证据都没有
    return round(100 * got_w / total_w, 1), "ok"
