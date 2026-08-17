"""简历/对话的**规则兜底**技能识别 —— 全仓唯一一份词表。

为什么单独一个模块：这份「关键词 → 技能标签 + 档位/分值」的兜底规则原来抄了两份
（`agent/diagnose._rule_parse` 与 `kg/pg_store/biz_store._SKILL_KW`），
而且已经漂移：前者 10 条（含 `C#`、汽车维修、航标作业）并且会先查技能库召回，
后者只有 8 条、直接上关键词。后果是实打实的行为分叉 —— 一份写着「汽车维修」的简历，
走对话诊断能命中，走简历诊断命不中，而且两条路径各测各的，没有任何测试会发现。

依赖方向注意：本模块**不在模块级 import 任何 backend 子模块**。
`kg/pg_store/biz_store` 要调它，而 `agent/` 反过来又依赖 `kg/pg_store/`，
模块级互相 import 就是环；把库访问放进函数体即可，两边都能安全引用。
"""
from __future__ import annotations

import re
from typing import Any

# 命中即 level=2 / score=40（"有相关经历，达到基本要求"）。
# 这是两份拷贝的**并集**：开发类保留 C#，汽车维修 / 航标作业来自 diagnose 那份。
SKILL_KEYWORDS: list[tuple[str, str]] = [
    (r"直播|带货|话术", "直播"),
    (r"投放|ROI|千川|广告", "投放"),
    (r"数据分析|SQL|看板|指标", "数据"),
    (r"脚本|短视频|内容", "内容"),
    (r"运营|私域|用户", "运营"),
    (r"python|java|开发|编程|C#", "开发"),
    (r"护理|医疗|康复", "护理"),
    (r"会计|财务|审计", "财务"),
    (r"汽车|维修|涂装", "汽车维修"),
    (r"航标|航海|海事", "航标作业"),
]

# 全不命中时的保底条目
FALLBACK_SKILL_NAME = "通用职业素养"
FALLBACK_LEVEL = 1
FALLBACK_SCORE = 20

HIT_LEVEL = 2
HIT_SCORE = 40


def kg_recall(text: str, limit: int = 12) -> list[dict[str, Any]]:
    """从库内真实技能表召回：文本里直接出现的 skill_key 即算命中。

    比关键词表准得多——那套词表是互联网口径（直播/投放/千川…），与库内国标口径的
    技能名（生产准备/设备维护与保养/安全风险辨识与管控…）对不上，
    一份写满真实技能的简历也只能解析出「通用职业素养」。

    库不可达时返回空列表，由调用方退化到 `SKILL_KEYWORDS`。
    """
    t = (text or "").strip()
    if not t:
        return []
    try:
        # 函数内 import：见模块 docstring 的依赖方向说明
        from backend.kg.pg_store.client import connect
        from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL

        with connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT ({SKILL_KEY_SQL}) AS k, n.category AS cat
                FROM kg_node n
                WHERE n.type='skill_level' AND COALESCE(n.status,'published')='published'
                  AND length({SKILL_KEY_SQL}) >= 2
                  AND position({SKILL_KEY_SQL} in %s) > 0
                LIMIT %s
                """,
                (t, limit * 3),
            ).fetchall()
    except Exception:
        return []
    # 长名优先：命中「设备维护与保养」时不再重复计入其子串「设备维护」
    hits = sorted(({"k": r["k"], "cat": r["cat"]} for r in rows), key=lambda x: -len(x["k"]))
    out: list[dict[str, Any]] = []
    taken: list[str] = []
    for h in hits:
        k = h["k"]
        if any(k in t2 for t2 in taken):
            continue
        taken.append(k)
        out.append(
            {
                "skill_name": k,
                "level": HIT_LEVEL,
                "score": HIT_SCORE,
                "evidence": f"简历文本命中技能库条目「{k}」"
                + (f"（{h['cat']}）" if h["cat"] else ""),
            }
        )
        if len(out) >= limit:
            break
    return out


def rule_parse_skills(
    text: str,
    *,
    use_kg_recall: bool = True,
    evidence_fmt: str = "规则命中：{pat}",
    fallback_evidence: str = "未识别领域关键词",
) -> list[dict[str, Any]]:
    """规则兜底解析：先查技能库召回，命不中再退化为粗粒度关键词，仍不中给保底条目。

    `evidence_fmt` / `fallback_evidence` 只影响 evidence 文案，
    两个调用点各自保留了原有措辞，避免改动前端已展示的文字。
    """
    hits: list[dict[str, Any]] = kg_recall(text) if use_kg_recall else []
    if hits:
        return hits
    for pat, label in SKILL_KEYWORDS:
        if re.search(pat, text or "", re.I):
            hits.append(
                {
                    "skill_name": label,
                    "level": HIT_LEVEL,
                    "score": HIT_SCORE,
                    "evidence": evidence_fmt.format(pat=pat),
                }
            )
    if not hits:
        hits.append(
            {
                "skill_name": FALLBACK_SKILL_NAME,
                "level": FALLBACK_LEVEL,
                "score": FALLBACK_SCORE,
                "evidence": fallback_evidence,
            }
        )
    return hits
