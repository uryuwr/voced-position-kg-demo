"""静态闸门：源码里凡是构造了 `skill_key`，同层就得有展示名。

    PYTHONPATH=. python -X utf8 scripts/verify_skill_name_in_code.py

## 为什么要**再**加一个闸门

`verify_skill_name_exposed.py` 是动态的：起服务、拿真实 id 打 61 个 GET 接口、
扫响应 JSON。它抓到过四轮漏网，但有个天生的盲区 —— **探测样本没覆盖到的分支
等于没测**。2026-08-20 那次就是：专业详情的 `skills[]`（专业直连技能 `covers`）
整列没有 `skill_name`，而库里 `covers` 边只有两条、且全是草稿态，闸门挑的已发布
专业拿到的是空数组，一路 PASS。是人打开页面看见 `SKabd68031c5` 才发现的。

静态判据不依赖数据：只要源码里写了一个带 `skill_key` 的 dict 而同层没有展示名，
不管那条分支跑不跑得到、库里有没有数据，都会红。两个闸门是互补的：
动态那个能抓到「声明了但被 Pydantic 丢掉」，静态这个能抓到「压根没写」。

## 判据

- **dict 字面量**：键里有 `skill_key`/`prereq_skill_key`，就得有 `skill_name`/
  `prereq_skill_name`/`name`/`display_name`/`*_name` 之一。
- **Pydantic 模型**：同理，但请求体（`*Body` / `*In`）豁免 —— 客户端传 code 是对的，
  要求它传名字反而是把展示名当输入。
- 白名单在 `ALLOW` 里，每条都要写清「为什么这里不需要名字」。空口豁免会让这个
  闸门在一年内退化成永远绿灯。
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOTS = ("backend",)

CODE_KEYS = {"skill_key", "prereq_skill_key"}
# `name` 也算：有些结构是 {id, name, skill_key}，name 就是那个技能的展示名
NAME_KEYS = {"skill_name", "prereq_skill_name", "name", "display_name"}

# 豁免：(文件后缀匹配, 行号, 原因)。行号会漂，所以再带一个键集合特征做二次确认。
ALLOW: dict[tuple[str, str], str] = {
    # ── 测评运行期的内部结构，不出接口 ────────────────────────────
    ("agent/assessment/service.py", "level|skill_key"): (
        "profile_levels 的入参形状（{skill_key, level}），只喂给匹配算法，不出接口"
    ),
    ("agent/assessment/stages.py", "level|skill_key"): (
        "同上：会话状态投影里的档位 map，前端拿 items[] 显示，不读这个"
    ),
    # ── 请求体 / 内部标识集合 ──────────────────────────────────
    ("api/main.py", "levels|occ_count|occupation_ids|skill_key"): (
        "技能→岗位倒排索引，键是 code、值是 id 列表；名字在同响应的技能列表里"
    ),
    ("api/routes_admin_biz.py", "level|skill_key|weight"): (
        "CompositionSkillBody —— 请求体，客户端传 code 是对的"
    ),
    # ── 名字已经在别处，显式再写一遍反而会抹掉 ──────────────────
    (
        "agent/assessment/store.py",
        "category|index|required_level|skill_key|type|variant|weight",
    ): (
        "题目行投影：skill_name 只在 payload 里（表没这一列），`**payload` 已经带进来了；"
        "在这里显式写一遍等于用 None 覆盖掉它（源码注释同址）"
    ),
    (
        "agent/assessment/grading.py",
        "level|skill_key|source|type",
    ): (
        "merge_measured 给「只有画像值、没实测」的技能补的占位项。这里的 key 来自"
        "画像 map，简历行本身就是名字；而 merge_measured 是纯函数、不持有 conn，"
        "为查名字给它加库访问会破坏可测性。展示名由 report.py 统一回落"
    ),
    (
        "kg/pg_store/biz_store.py",
        "available_levels|level_descriptions|missing_levels|node_ids|skill_key",
    ): (
        "SkillOut 的 `attrs` 子对象（运维元数据），同一响应的外层已有 skill_name；"
        "动态闸门也把 `attrs` 列入放行键"
    ),
    (
        "kg/pg_store/skill_write.py",
        "is_required_level|level_code|required_level|skill_key",
    ): (
        "写进 kg_edge.attrs 的内容，不是出参。边的 attrs 里存 code 正是当身份用，"
        "冗余一份会随改名漂移"
    ),
}


def _keys_of(d: ast.Dict) -> set[str]:
    return {k.value for k in d.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _allowed(path: pathlib.Path, keys: set[str]) -> str | None:
    posix = path.as_posix()
    sig = "|".join(sorted(keys))
    for (suffix, want_sig), reason in ALLOW.items():
        if posix.endswith(suffix) and want_sig == sig:
            return reason
    return None


def scan_dicts() -> tuple[list[tuple[str, int, list[str]]], int]:
    bad: list[tuple[str, int, list[str]]] = []
    skipped = 0
    for root in ROOTS:
        for p in sorted(pathlib.Path(root).rglob("*.py")):
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                ks = _keys_of(node)
                if not (ks & CODE_KEYS) or (ks & NAME_KEYS):
                    continue
                # 后缀是 _name 的键也算配了展示名（from_name / to_name 这类）
                if any(k.endswith("_name") for k in ks):
                    continue
                if _allowed(p, ks):
                    skipped += 1
                    continue
                bad.append((p.as_posix(), node.lineno, sorted(ks)))
    return bad, skipped


def scan_models() -> list[tuple[str, int, str, list[str]]]:
    """Pydantic 模型：声明了 code 字段却没声明展示名字段。

    响应模型漏声明比数据层漏挑更隐蔽：数据层给了名字也会被 Pydantic **静默丢弃**，
    接口看着就像数据层没给。
    """
    bad: list[tuple[str, int, str, list[str]]] = []
    for root in ROOTS:
        for p in sorted(pathlib.Path(root).rglob("*.py")):
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                # 请求体豁免：客户端传 code 是对的
                if node.name.endswith(("Body", "In", "Request")):
                    continue
                fields = {
                    st.target.id
                    for st in node.body
                    if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name)
                }
                if not (fields & CODE_KEYS):
                    continue
                if (fields & NAME_KEYS) or any(f.endswith("_name") for f in fields):
                    continue
                bad.append((p.as_posix(), node.lineno, node.name, sorted(fields & CODE_KEYS)))
    return bad


dict_bad, dict_skipped = scan_dicts()
model_bad = scan_models()

print("=" * 62)
print("静态扫描：源码里带 skill_key 的结构是否都配了展示名")
print("=" * 62)
print(f"\ndict 字面量（白名单豁免 {dict_skipped} 处）：")
if dict_bad:
    print(f"★ {len(dict_bad)} 处有 code、无展示名：")
    for f, ln, ks in dict_bad:
        print(f"    {f}:{ln}")
        print(f"        键集合 = {ks}")
else:
    print("    PASS")

print("\nPydantic 模型（请求体 *Body / *In / *Request 豁免）：")
if model_bad:
    print(f"★ {len(model_bad)} 个声明了 code 字段却没声明展示名：")
    for f, ln, cls, cs in model_bad:
        print(f"    {f}:{ln}  class {cls}  code 字段={cs}")
else:
    print("    PASS")

print()
if dict_bad or model_bad:
    print("修法：从数据源把展示名挑出来（`entity_skill_composition` 等本来就返回 "
          "skill_name），拿不到就用 `skill_aggregate.resolve_skill_names` 批量查；"
          "真不需要名字的加进本文件 ALLOW 并写明理由。")
sys.exit(1 if (dict_bad or model_bad) else 0)
