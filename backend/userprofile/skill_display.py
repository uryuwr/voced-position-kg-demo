"""`biz_user_skill.skill_name` 的两种形态，以及读它时该怎么办。

## 这一列装着两种东西

`biz_user_skill` 是学员技能画像，`skill_name` 列同时承担**身份**与**展示**：

- **测评来的行**（`source='assessment'`）：装的是 `skill_key`。
  2026-08-19 之前 key 就是中文名，所以这么用没问题；改造后它变成
  `SKxxxxxxxxxx`，于是 `/v1/student/me/skills`、`/v1/student/profile`
  直接把一串哈希显示给学员看。
- **简历 / 对话来的行**（`source='resume'|'chat'`）：装的是模型抽出来的**自由文本
  技能名**（「汽车故障诊断」），本来就不是 key。

## 为什么不能简单把它改成名字

`biz_store.match_with_profile._user_level_for` 有**两条匹配路径**：

1. 按 key **精确**匹配 —— 测评来的行靠这条（要求侧的 `skill_key` 现在是 code）
2. 按名字**模糊**包含匹配 —— 简历/对话那些自由文本靠这条

把测评行的值换成名字，第 1 条就断了（拿名字去和 code 比永远不等），而症状是
「测过的技能显示未测、匹配度虚低」—— 不报错。

## 所以这里的做法

- **画像 map 同时按两者建键**（`profile_levels`）：code 与名字都指向同一档位，
  两条匹配路径都成立。
- **展示一律用 `display_name`**：是 code 就查名字，不是 code 就原样用。
"""

from __future__ import annotations

from typing import Any, Iterable

from backend.kg.skill_key import is_valid_key


def resolve_names(codes: Iterable[str], conn: Any | None = None) -> dict[str, str]:
    """code → 展示名。一次查库，不做 N+1。"""
    keys = sorted({str(c).strip() for c in codes if is_valid_key(str(c).strip())})
    if not keys:
        return {}
    from backend.kg.pg_store.client import use_conn
    from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL, SKILL_NAME_SQL

    out: dict[str, str] = {}
    with use_conn(conn) as c:
        for r in c.execute(
            f"""
            SELECT DISTINCT ({SKILL_KEY_SQL}) AS k, ({SKILL_NAME_SQL}) AS nm
            FROM kg_node n
            WHERE n.type = 'skill_level' AND NOT n.is_draft
              AND ({SKILL_KEY_SQL}) = ANY(%s)
            """,
            (keys,),
        ).fetchall():
            if r["k"] and r["nm"]:
                out.setdefault(r["k"], r["nm"])
    return out


def display_name(raw: str | None, name_map: dict[str, str]) -> str:
    """展示名：是 code 就查表，查不到或本来就是名字就原样用。

    查不到时回落到 code 而不是留空 —— 指向已删技能的历史画像项仍要看得见，
    只是显示成一串 code，比整行消失好排查。
    """
    v = str(raw or "").strip()
    if not v:
        return ""
    return name_map.get(v) or v


def profile_levels(rows: Iterable[dict[str, Any]], conn: Any | None = None) -> dict[str, int]:
    """画像行 → {标识: 档位}，**code 与名字都建键**。

    两条匹配路径都要喂到（见模块 docstring）：测评行按 code 精确命中，
    简历/对话行按名字模糊命中。同一技能两个键指向同一档位，不会重复计权 ——
    `_user_level_for` 命中一次就返回。
    """
    raws = [str(r.get("skill_name") or "").strip() for r in rows]
    names = resolve_names(raws, conn=conn)
    out: dict[str, int] = {}
    for r in rows:
        v = str(r.get("skill_name") or "").strip()
        if not v:
            continue
        try:
            lv = int(r.get("level") or 0)
        except (TypeError, ValueError):
            continue
        for key in {v, names.get(v) or v}:
            out[key] = max(out.get(key, 0), lv)
    return out


def normalize_stored_report_skills(rep: dict[str, Any], conn) -> None:
    """就地修补**落库快照**里的技能标识。

    报告是快照：一次测评的结论按当时的形态存进 `biz_diagnosis_result.report_json`，
    之后不再重算。skill_key 在 2026-08-19 改成 ASCII code，于是库里存着两代形态：

    - 迁移前的（29 个里 27 个）：`skill_key` 存的是**中文技能名**，没有 skill_name
    - 迁移后的：`skill_key` 是 code，早期的几个也还没带 skill_name

    只补展示名不够。前端把报告项与技能构成对比用的是
    `report.items[].skill_key === composition[].skill_key` —— 一边中文、一边 code，
    **这个匹配会静默失败**，表现是「我的等级」整列空白，而不是报错。
    所以老形态的 key 也要换成当前 code。

    换 key 的依据是**按名字查库**，不是 `derive_key(名字)` 反推：技能若在管理台
    改过名，反推出来的 code 与库里存的不是一个（见 backend/kg/skill_key.py）。
    查不到就退回反推，再退回原样 —— 一个查不到的历史技能不该让整份报告读不出来。

    一次查库解决全部项，不做 N+1。
    """
    from backend.kg.pg_store.skill_aggregate import (
        SKILL_KEY_SQL as _KEY,
    )
    from backend.kg.pg_store.skill_aggregate import (
        SKILL_NAME_SQL as _NAME,
    )
    from backend.kg.skill_key import derive_key, is_valid_key

    # **递归收集，不枚举桶名**：第一版写死了 items/gaps/strengths/no_baseline，
    # 结果漏了 `untested`（闸门扫出来的）。报告的结构会随功能加桶，
    # 枚举名字这件事本身就是漏的根源 —— 凡带 skill_key 的对象一律修。
    rows: list[dict[str, Any]] = []

    def _collect(o: Any) -> None:
        if isinstance(o, dict):
            if "skill_key" in o:
                rows.append(o)
            for v in o.values():
                _collect(v)
        elif isinstance(o, list):
            for v in o:
                _collect(v)

    _collect(rep)
    if not rows:
        return

    legacy_names = {
        str(x.get("skill_key") or "").strip()
        for x in rows
        if str(x.get("skill_key") or "").strip()
        and not is_valid_key(str(x.get("skill_key") or "").strip())
    }
    need_name_codes = {
        str(x.get("skill_key") or "").strip()
        for x in rows
        if is_valid_key(str(x.get("skill_key") or "").strip())
        and not str(x.get("skill_name") or "").strip()
    }

    name2key: dict[str, str] = {}
    key2name: dict[str, str] = {}
    if legacy_names or need_name_codes:
        for r in conn.execute(
            f"""
            SELECT DISTINCT ({_KEY}) AS k, ({_NAME}) AS nm
            FROM kg_node n
            WHERE n.type = 'skill_level' AND NOT n.is_draft
              AND (({_NAME}) = ANY(%s) OR ({_KEY}) = ANY(%s))
            """,
            (list(legacy_names), list(need_name_codes)),
        ).fetchall():
            if r["k"] and r["nm"]:
                name2key.setdefault(r["nm"], r["k"])
                key2name.setdefault(r["k"], r["nm"])

    for x in rows:
        k = str(x.get("skill_key") or "").strip()
        if not k:
            continue
        if is_valid_key(k):
            if not str(x.get("skill_name") or "").strip():
                x["skill_name"] = key2name.get(k) or k
            continue
        # 老形态：key 就是名字
        x["skill_name"] = x.get("skill_name") or k
        x["skill_key"] = name2key.get(k) or derive_key(k)
