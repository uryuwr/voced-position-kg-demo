"""国标等级刻度 → 产品档的**入库期归一**。三条写入路径共用的唯一真源。

为什么要单独一个模块
--------------------
这张映射表曾经有三份拷贝（采集端、迁移脚本、校验脚本），更糟的是三条写入路径
产出的形态**互相不认**：

- `crawlers/cn/ingest_skill_standards.py` 只补 `attrs.level` 就收工，
  仍留着 `level_zh`，`name` 还是「制备 · 三级/高级工」
- `scripts/migrate_skill_level_to_product.py` 还要再剥掉 `level_zh`，
  把 name 改写成「制备 · L3」
- `backend/kg/pg_store/migrate.py` 从 SQLite 灌库时 `attrs = EXCLUDED.attrs`
  **无条件整体覆盖**

三者叠加的实际后果（2026-08-14 → 08-18 真实发生过）：手工回填把 8919 个节点补上
产品档、可评分岗位从 117 涨到 608；四天后有人重灌了一次库，数字**逐位退回**回填前。
没有任何报错——覆盖是「成功」的。

所以归一必须发生在**入库的必经之路上**，而不是靠谁记得跑一次性脚本。
现在 `pg_store/migrate.py` 每次灌库都过一遍这里，采集端建节点时也过一遍，
于是这件事**自愈**：无论数据从哪条路进库，落地形态一致。

刻度方向（唯一容易搞错的地方）
------------------------------
国标技能五级制**一级最高**，与产品档方向相反：

    国标 L1 = 一级/高级技师 = 最高   →   产品档 5（专家）
    国标 L5 = 五级/初级工   = 最低   →   产品档 1（了解）

搞反了不报错、不崩，只会把高级技师判成入门水平，而且分数看着挺正常。
所以 `scripts/verify_backfill.py` 另存了一份独立的期望表逐档核对——
那份**故意不 import 这里**，否则校验就成了自己比自己。改这里记得同步改那里。

边界
----
这是**入库期的一次性转换**。读路径不做任何刻度换算，`attrs.level` 是唯一真源
（见 CLAUDE.md「技能等级」）。放在 `backend/kg/` 是因为 crawlers 可以 import
backend，反之不行；本模块是纯 Python，不碰文件与数据库，不影响 backend 独立部署。
"""
from __future__ import annotations

import json
import re
from typing import Any, MutableMapping

# 国标等级原文名 → 产品档。精确匹配：库里 level_zh 就是这 8 个规范值，
# 不用子串匹配——「高级」会误伤「高级工」「高级技师」，一个字之差差两档。
ZH_TO_LEVEL: dict[str, int] = {
    "五级/初级工": 1,
    "四级/中级工": 2,
    "三级/高级工": 3,
    "二级/技师": 4,
    "一级/高级技师": 5,
    # 专业技术人才三级制：三档均匀铺到产品五档（L2/L4 因源数据无此粒度而空缺）
    "初级": 1,
    "中级": 3,
    "高级": 5,
}

# level_zh 缺失时的兜底。L 码需反转（见上方「刻度方向」），T 码按三级制铺开。
CODE_TO_LEVEL: dict[str, int] = {
    "L1": 5, "L2": 4, "L3": 3, "L4": 2, "L5": 1,
    "T1": 1, "T2": 3, "T3": 5,
}

# 只替换被「·」包住或位于串尾的等级词，避免误伤国标正文里的「初级工应能…」
_GRADE_IN_TEXT = re.compile(
    r"·\s*(一级/高级技师|二级/技师|三级/高级工|四级/中级工|五级/初级工|初级|中级|高级)"
    r"(?=\s*·|\s*$)"
)


def product_level(level_zh: str | None, level_code: str | None) -> int | None:
    """国标等级名 / 原码 → 产品档 1–5。判定不了返回 None（调用方不许瞎猜）。

    等级名优先：它是人写的规范值，比原码可信；原码只在等级名缺失时兜底。
    """
    zh = (level_zh or "").strip()
    if zh in ZH_TO_LEVEL:
        return ZH_TO_LEVEL[zh]
    return CODE_TO_LEVEL.get((level_code or "").strip().upper())


def skill_key_of(attrs: dict[str, Any], name: str) -> str:
    """与 `SKILL_KEY_SQL` 一致的技能标识：attrs.skill_key → skill_name → name 首段。

    重写 name 时要用它——直接截 name 会把已经归一过的「制备 · L3」再截一次。
    """
    for k in ("skill_key", "skill_name"):
        v = str(attrs.get(k) or "").strip()
        if v:
            return v
    return (name or "").split(" · ")[0].split("·")[0].strip() or (name or "")


def _as_attrs(v: Any) -> tuple[dict[str, Any], bool]:
    """attrs 可能是 dict（采集端在内存里造的）或 JSON 串（SQLite / PG 读出来的）。

    返回 (dict, 原本是不是串)，好按原样写回去——把串换成 dict 会让下游
    `_norm_json_field` 之类的处理拿到意外类型。
    """
    if isinstance(v, str):
        try:
            d = json.loads(v)
        except Exception:
            return {}, True
        return (d if isinstance(d, dict) else {}), True
    return (v if isinstance(v, dict) else {}), False


def normalize_skill_level_node(node: MutableMapping[str, Any]) -> str:
    """就地归一一个 skill_level 节点，返回 `ok` / `skip` / `unresolved`。

    - `ok`         有改动，已归一
    - `skip`       本来就是目标形态，没动
    - `unresolved` 既无等级名也无可识别原码，**保持原样不动**

    `unresolved` 不抛异常：这是批量灌库路径，一条脏数据不能打死整批
    （见 CLAUDE.md）。调用方负责统计并把数量报出来。

    归一后的目标形态：
        attrs.level             int 1–5，产品语义，唯一真源
        attrs.source_level_code 'L4'/'T3'，国标原码，仅溯源、不对外输出
        attrs 不再有 level_code / level_zh / product_level_int
        name        「制备 · L3」
        description 里「· 四级/中级工 ·」→「· L2 ·」（国标正文描述不受影响）

    id / source_id 不动：它们含源系统原码，是不透明标识，改了会产生重复节点。
    """
    attrs, was_str = _as_attrs(node.get("attrs"))
    name = node.get("name") or ""
    desc = node.get("description") or ""

    code = str(attrs.get("level_code") or attrs.get("source_level_code") or "").strip().upper()
    zh = attrs.get("level_zh")

    # —— 刻度 ——
    lv = attrs.get("level")
    need_scale = lv is None or "level_code" in attrs or "product_level_int" in attrs
    if need_scale:
        lv = product_level(zh, code)
        if lv is None:
            return "unresolved"
        attrs["level"] = lv
        if code:
            attrs["source_level_code"] = code   # 溯源留在 attrs 内
        attrs.pop("level_code", None)           # 语义已由 level 承担，留着必然再被误读
        attrs.pop("product_level_int", None)    # 与 level 重复

    # —— 文案 ——
    # 国标等级名与产品档是两套说法且数字方向相反（国标四级 = 产品 L2），
    # 同屏出现必然打架，所以入库时就把国标说法剥掉。
    need_text = ("level_zh" in attrs) or bool(_GRADE_IN_TEXT.search(name)) \
        or bool(_GRADE_IN_TEXT.search(desc))
    if need_text:
        attrs.pop("level_zh", None)
        key = skill_key_of(attrs, name)
        name = f"{key} · L{lv}" if lv else key
        desc = _GRADE_IN_TEXT.sub(f"· L{lv}" if lv else "", desc)

    if not (need_scale or need_text):
        return "skip"

    node["attrs"] = json.dumps(attrs, ensure_ascii=False) if was_str else attrs
    node["name"] = name
    node["description"] = desc
    # name_zh 与 name 同源，一起改，否则两个字段各说各的
    if node.get("name_zh") is not None:
        node["name_zh"] = name
    return "ok"


def normalize_nodes(nodes: list[MutableMapping[str, Any]]) -> dict[str, int]:
    """批量归一，只处理 `type == 'skill_level'` 的节点。返回各结果的计数。"""
    stats = {"ok": 0, "skip": 0, "unresolved": 0}
    for n in nodes:
        if n.get("type") != "skill_level":
            continue
        stats[normalize_skill_level_node(n)] += 1
    return stats
