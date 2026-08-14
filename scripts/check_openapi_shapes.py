"""扫 /openapi.json，揪出「文档上看不到实际数据形状」的地方。

判定的四类问题（都会在 Swagger 上渲染成 `any` 或一片空白）：

- `no_response_model` —— 路由没挂 response_model，响应体在文档里根本没有 schema
- `free_object`       —— 对象没有 properties（`{}` 或 additionalProperties:true），
                          Swagger 显示 `any`；这正是 user_skills 那个截图的成因
- `free_array`        —— 数组的 items 是自由对象，同上
- `no_description`    —— 字段有类型但没注释，看得见形状看不懂含义

用法：
    python -X utf8 scripts/check_openapi_shapes.py            # 起进程内 app 直接取
    python -X utf8 scripts/check_openapi_shapes.py --strict   # 有问题时退出码 1（供 CI 用）

只读，不改任何东西。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 这些字段天生自由：attrs 是无约束 JSON 列，raw/response 是外部服务原样透传。
# 它们仍应有 description 说明「为什么自由」，但不算 free_object 缺陷。
#  这些字段的「自由」是事实而非疏漏，逐个记明原因；不在此表内的一律算缺陷。
#  加新条目前先问：真的没有稳定形状，还是只是懒得查生产代码？
ALLOW_FREE = {
    "attrs",           # kg_node.attrs 是无约束 TEXT/JSON 列，键随数据来源而异
    "raw",             # 外部画像服务的原始响应，原样透传供调试核对
    "response",        # 同上
    "facet_details",   # 五维记忆各维的专属字段，由画像平台定义，本服务不该替它收敛
    "detail",          # 门禁规则的取证细节，BR-02~BR-08 每条结构都不同
    "output",          # 测评阶段产出，已按 key 给出联合类型，dict 分支是未完成态的 {}
    "levels",          # 技能 bundle 的 L1–L5 内容，键是档位号、值随档位模板而变
    "gate",            # 嵌套的门禁结果，等同 PublishValidateOut，避免循环引用
    "link_ids",        # {industry_ids|major_ids|occupation_ids: [...]}，键按维度动态
    "extra",
    "json_schema_extra",
}

# FastAPI 内置模型，我们改不了它的字段注释
SKIP_MODELS = {"ValidationError", "HTTPValidationError"}


def _is_free_object(sch: dict) -> bool:
    if not isinstance(sch, dict):
        return False
    if sch.get("$ref") or sch.get("allOf") or sch.get("anyOf") or sch.get("oneOf"):
        return False
    if sch.get("type") == "object":
        if sch.get("properties"):
            return False
        # dict[str, int] 这类映射：additionalProperties 是个 schema 而非 true，
        # 值类型是明确的，Swagger 不会显示 any
        ap = sch.get("additionalProperties")
        return not isinstance(ap, dict)
    # 连 type 都没有、也没 ref/枚举/组合 —— Swagger 渲染为 any
    return not any(
        k in sch for k in ("type", "$ref", "enum", "const", "allOf", "anyOf", "oneOf")
    )


def walk(name: str, sch: dict, path: str, issues: list, seen: set) -> None:
    if not isinstance(sch, dict):
        return
    key = id(sch)
    if key in seen:
        return
    seen.add(key)

    for sub in ("allOf", "anyOf", "oneOf"):
        for i, s in enumerate(sch.get(sub) or []):
            walk(name, s, f"{path}", issues, seen)

    if sch.get("type") == "array":
        items = sch.get("items") or {}
        if _is_free_object(items) and name not in ALLOW_FREE:
            issues.append(("free_array", path, "数组元素是自由对象，Swagger 显示 any"))
        else:
            walk(name, items, f"{path}[]", issues, seen)
        return

    props = sch.get("properties")
    if props:
        for fname, fsch in props.items():
            fpath = f"{path}.{fname}"
            if not isinstance(fsch, dict):
                continue
            # 描述：allOf/$ref 包一层时描述可能挂在外层
            has_desc = bool(fsch.get("description")) or bool(
                any((s or {}).get("description") for s in (fsch.get("allOf") or []))
            )
            if _is_free_object(fsch) and fname not in ALLOW_FREE:
                issues.append(("free_object", fpath, "对象无 properties，Swagger 显示 any"))
            elif not has_desc and not fsch.get("$ref"):
                issues.append(("no_description", fpath, "字段无注释"))
            walk(fname, fsch, fpath, issues, seen)
    elif _is_free_object(sch) and name not in ALLOW_FREE:
        issues.append(("free_object", path, "对象无 properties，Swagger 显示 any"))


def main() -> int:
    import backend.api.main as m

    spec = m.app.openapi()
    issues: list[tuple[str, str, str]] = []

    # ① 路由缺 response_model：200 响应没有 schema，或 schema 是自由对象
    for route, methods in (spec.get("paths") or {}).items():
        for method, op in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            ok = ((op.get("responses") or {}).get("200") or {})
            content = (ok.get("content") or {}).get("application/json") or {}
            sch = content.get("schema")
            if ok and not content:
                continue                      # SSE / 文件下载等非 JSON 响应
            if sch is None or _is_free_object(sch):
                issues.append((
                    "no_response_model", f"{method.upper()} {route}",
                    "响应体在文档上没有 schema",
                ))

    # ② 模型内部的自由对象与缺注释字段
    seen: set = set()
    for name, sch in ((spec.get("components") or {}).get("schemas") or {}).items():
        if name in SKIP_MODELS:
            continue
        walk(name, sch, name, issues, seen)

    by_kind = Counter(k for k, _, _ in issues)
    order = ["no_response_model", "free_object", "free_array", "no_description"]
    for kind in order:
        rows = [(p, why) for k, p, why in issues if k == kind]
        if not rows:
            continue
        print(f"\n## {kind}（{len(rows)}）")
        for p, why in sorted(rows)[:200]:
            print(f"  {p}  —— {why}")
        if len(rows) > 200:
            print(f"  …… 另有 {len(rows) - 200} 条")

    print(f"\n{'=' * 60}")
    print("合计：" + "，".join(f"{k}={by_kind.get(k, 0)}" for k in order) or "无问题")
    print(f"端点 {sum(1 for _ in (spec.get('paths') or {}))} 个，"
          f"模型 {len((spec.get('components') or {}).get('schemas') or {})} 个")

    if "--strict" in sys.argv and issues:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
