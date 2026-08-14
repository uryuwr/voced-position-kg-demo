"""openapi.json → 单篇 Markdown 接口文档（用于导入飞书云文档）。

`/docs` 是给人试调的，但要评审、要转飞书、要贴进需求文档时得有一份线性文本。
这里从 **同一个 openapi.json** 生成，不手写——手写的那份必然会和代码走散。

模型只在文末「数据模型」里展开一次，端点处用锚点链接引用——早先每个端点都把
嵌套模型摊平重复一遍，8000 行里绝大部分是同样的表，飞书 API 直接写不进去。

输出：docs/API接口文档.md
用法：python -X utf8 scripts/openapi_to_md.py [输出路径]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 每个端点都带的鉴权头，只在「公共约定」里讲一次
_COMMON_HEADERS = {
    "Authorization", "X-User-Name", "X-Test-Uid", "X-Test-Uname", "sdp-app-id",
}

# 只导出前端真正会调的接口。给前端看的文档里塞进运维/离线工具接口，
# 只会让人分不清哪些是契约、哪些是内部实现。
#
# 名单按「前端页面 → 它调什么」整理，不是按 tag 拍脑袋分的；
# 新增页面功能时在这里加一条，并在下面 EXCLUDE 里确认没被挡掉。
INCLUDE_PREFIXES: tuple[str, ...] = (
    # ── /admin 运营看板 ──
    "/v1/admin/dashboard/summary",
    "/v1/admin/ai-gateway",
    # ── /admin 行业 / 专业 / 岗位 / 技能 四维管理（列表、详情、增删改） ──
    "/v1/kg/nodes",
    "/v1/kg/node-detail",
    "/v1/kg/edges",
    "/v1/node",
    "/v1/nodes/",
    "/v1/admin/changes",
    # ── /admin 技能库与技能构成 ──
    "/v1/admin/skills",
    "/v1/admin/composition",
    "/v1/admin/edges/review",
    "/v1/occupations/skills",
    # ── /admin 知识图谱（行业入口） ──
    "/v1/industry-graph",
    "/v1/occupation-skills-graph",
    "/v1/industries/tree",
    "/v1/industries/search",
    # ── 前端启动必读 ──
    "/v1/config",
    "/v1/capability",
    # ── /student 学员端 ──
    "/v1/student/",
)

# 命中 INCLUDE 但仍要排除的。
EXCLUDE_PREFIXES: tuple[str, ...] = (
    "/v1/student/profile",      # 我的画像：调试用，不对外暴露
)


def _exported(path: str) -> bool:
    if any(path.startswith(p) for p in EXCLUDE_PREFIXES):
        return False
    return any(path.startswith(p) for p in INCLUDE_PREFIXES)


def _ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def _type_of(sch: dict, spec: dict, depth: int = 0) -> str:
    """schema → 一句人类可读的类型描述。"""
    if not isinstance(sch, dict):
        return "any"
    if "$ref" in sch:
        return _ref_name(sch["$ref"])
    for key in ("anyOf", "oneOf"):
        if key in sch:
            parts = [_type_of(s, spec, depth) for s in sch[key]]
            parts = [p for p in parts if p != "null"]
            seen: list[str] = []
            for p in parts:
                if p not in seen:
                    seen.append(p)
            return " | ".join(seen) or "any"
    if "allOf" in sch and sch["allOf"]:
        return _type_of(sch["allOf"][0], spec, depth)
    t = sch.get("type")
    if t == "array":
        return f"{_type_of(sch.get('items') or {}, spec, depth)}[]"
    if t == "object":
        ap = sch.get("additionalProperties")
        if isinstance(ap, dict):
            return f"map<string, {_type_of(ap, spec, depth)}>"
        return "object"
    if sch.get("enum"):
        return " \\| ".join(f"`{v}`" for v in sch["enum"])
    if sch.get("const") is not None:
        return f"`{sch['const']}`"
    return t or "any"


def _constraints(sch: dict) -> str:
    bits = []
    for k, label in (
        ("minimum", "≥"), ("maximum", "≤"),
        ("exclusiveMinimum", ">"), ("exclusiveMaximum", "<"),
        ("minLength", "长度≥"), ("maxLength", "长度≤"),
    ):
        if sch.get(k) is not None:
            bits.append(f"{label}{sch[k]}")
    if sch.get("default") is not None:
        bits.append(f"默认 `{sch['default']}`")
    return "，".join(bits)


def _schema_model(sch: dict) -> str | None:
    """从响应/请求体 schema 里挖出模型名。

    不能只认顶层 `$ref`：可空响应（`GoalOut | None`）在 OpenAPI 里是
    `anyOf: [$ref, null]`，列表响应是 `array.items.$ref`，
    只认 `$ref` 会让这些端点在文档上显示成一个光秃秃的「响应」。
    """
    if not isinstance(sch, dict):
        return None
    if sch.get("$ref"):
        return _ref_name(sch["$ref"])
    if sch.get("type") == "array":
        return _schema_model(sch.get("items") or {})
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in sch.get(key) or []:
            if isinstance(sub, dict) and sub.get("type") == "null":
                continue
            got = _schema_model(sub)
            if got:
                return got
    return None


def _is_array(sch: dict) -> bool:
    """顶层是不是数组（含 `array | null` 这种可空数组）。"""
    if not isinstance(sch, dict):
        return False
    if sch.get("type") == "array":
        return True
    for key in ("anyOf", "oneOf"):
        for sub in sch.get(key) or []:
            if isinstance(sub, dict) and sub.get("type") == "array":
                return True
    return False


def _anchor(name: str) -> str:
    """引用附录里那份模型定义。

    这里**不生成 Markdown 链接**：飞书文档的文内跳转要求 `文档URL#block_id`，
    而 block_id 要等内容写完才存在，`[x](#name)` 导进去就是一个点不动的死链。
    模型在附录里是三级标题，用飞书左侧「大纲」跳转即可。
    """
    return f"`{name}`"


def _model_link(sch: dict, spec: dict) -> str:
    """类型描述，遇到已注册模型就换成锚点链接。"""
    schemas = (spec.get("components") or {}).get("schemas") or {}
    txt = _type_of(sch, spec)
    for name in sorted(schemas, key=len, reverse=True):
        if name in txt:
            txt = txt.replace(name, _anchor(name))
    return txt


def _field_table(name: str, spec: dict, out: list[str]) -> None:
    """展开一个模型的字段表（**只列一层**，嵌套模型用链接指向附录）。"""
    schemas = (spec.get("components") or {}).get("schemas") or {}
    sch = schemas.get(name)
    if not isinstance(sch, dict):
        return
    props = sch.get("properties") or {}
    if not props:
        out.append("_该模型无固定字段（自由对象，说明见字段注释）_")
        out.append("")
        return
    required = set(sch.get("required") or [])

    if sch.get("description"):
        for ln in str(sch["description"]).strip().split("\n"):
            out.append(f"> {ln}" if ln.strip() else ">")
        out.append("")

    out.append("| 字段 | 类型 | 必填 | 说明 |")
    out.append("| --- | --- | --- | --- |")
    for fname, fsch in props.items():
        if not isinstance(fsch, dict):
            continue
        desc = (fsch.get("description") or "").replace("\n", " ").replace("|", "\\|")
        cons = _constraints(fsch)
        if cons:
            desc = f"{desc}（{cons}）" if desc else cons
        out.append(
            f"| `{fname}` | {_model_link(fsch, spec)} | "
            f"{'是' if fname in required else ''} | {desc} |"
        )
    out.append("")


def _collect_refs(sch: dict) -> list[str]:
    found: list[str] = []
    if not isinstance(sch, dict):
        return found
    if "$ref" in sch:
        found.append(_ref_name(sch["$ref"]))
    for k in ("items", "additionalProperties"):
        if isinstance(sch.get(k), dict):
            found += _collect_refs(sch[k])
    for k in ("anyOf", "oneOf", "allOf"):
        for s in sch.get(k) or []:
            found += _collect_refs(s)
    for s in (sch.get("properties") or {}).values():
        found += _collect_refs(s)
    return found


def build(spec: dict) -> str:
    info = spec.get("info") or {}
    out: list[str] = [
        f"# {info.get('title') or 'API 接口文档'}",
        "",
        "",
        "> 本文由 `scripts/openapi_to_md.py` 从 `/openapi.json` 生成，**不要手工编辑**。",
        "> 契约的唯一真源是代码里的 Pydantic 模型与路由注解；改代码后重新生成即可。",
        ">",
        "> 只收录**前端页面实际调用**的接口；运维、离线工具、内部调试接口不在此列。",
        "",
    ]

    used: set[str] = set()

    # 鉴权头在 82 个端点上一模一样，逐个重复只会把文档冲淡
    out += [
        "## 公共约定",
        "",
        "**鉴权**　所有 `/v1/**` 端点都走 UC MAC Token，请求头：",
        "",
        "| 请求头 | 必填 | 说明 |",
        "| --- | --- | --- |",
        "| `Authorization` | 是 | UC MAC Token；签名基于 `raw_path`（保留 %XX 编码） |",
        "| `X-User-Name` | | 可选展示名，中文需 `encodeURIComponent` |",
        "| `sdp-app-id` | 是 | 应用标识，由前端透传，服务端不写死 |",
        "",
        "以下端点文档中不再重复列出这几项。",
        "",
        "**状态可见性**　`published` 前后台都可见；`draft`/`disabled` 仅管理台可见；",
        "`archived` 是逻辑删除，任何接口都不返回。",
        "",
        "**技能等级**　产品档 1–5（1 了解 → 5 专家）。一个技能在库里是 5 个 `skill_level`",
        "节点，读路径按 `attrs.skill_key` 聚合成逻辑 bundle；「要求哪一档」由边指向哪个",
        "等级节点表达。",
        "",
        "---",
        "",
    ]

    # 按 tag 分组
    by_tag: dict[str, list[tuple[str, str, dict]]] = {}
    skipped = 0
    for path, methods in (spec.get("paths") or {}).items():
        if not _exported(path):
            skipped += len([k for k in methods if k in ("get", "post", "put", "patch", "delete")])
            continue
        for method, op in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            tags = op.get("tags") or ["未分类"]
            by_tag.setdefault(tags[0], []).append((method, path, op))
    total = sum(len(v) for v in by_tag.values())

    out.insert(2, f"版本 {info.get('version') or '-'}　·　{total} 个端点（另有 {skipped} 个内部接口未收录）")
    out.append("## 目录")
    out.append("")
    for tag in by_tag:
        out.append(f"- {tag}（{len(by_tag[tag])}）")
    out.append("- 数据模型")
    out.append("")

    for tag, ops in by_tag.items():
        out.append(f"## {tag}")
        out.append("")
        for method, path, op in sorted(ops, key=lambda x: x[1]):
            summary = op.get("summary") or ""
            out.append(f"### `{method.upper()}` {path}")
            out.append("")
            if summary:
                out.append(f"**{summary}**")
                out.append("")
            if op.get("description"):
                out.append(str(op["description"]).strip())
                out.append("")

            # 入参
            params = [
                p for p in (op.get("parameters") or [])
                if p.get("name") not in _COMMON_HEADERS
            ]
            if params:
                out.append("**请求参数**")
                out.append("")
                out.append("| 参数 | 位置 | 类型 | 必填 | 说明 |")
                out.append("| --- | --- | --- | --- | --- |")
                for p in params:
                    psch = p.get("schema") or {}
                    desc = (p.get("description") or "").replace("\n", " ").replace("|", "\\|")
                    cons = _constraints(psch)
                    if cons:
                        desc = f"{desc}（{cons}）" if desc else cons
                    out.append(
                        f"| `{p.get('name')}` | {p.get('in')} | {_type_of(psch, spec)} | "
                        f"{'是' if p.get('required') else ''} | {desc} |"
                    )
                out.append("")

            # 请求体
            body = ((op.get("requestBody") or {}).get("content") or {}).get("application/json")
            if body:
                bname = _schema_model(body.get("schema") or {})
                out.append(
                    "**请求体**" + (f"：{_anchor(bname)}" if bname else "")
                )
                out.append("")
                if bname:
                    used.add(bname)

            # 响应
            ok = (op.get("responses") or {}).get("200") or {}
            content = (ok.get("content") or {})
            js = content.get("application/json") or {}
            if js.get("schema"):
                rsch = js["schema"]
                rname = _schema_model(rsch)
                if rname and _is_array(rsch):
                    out.append(f"**响应**：{_anchor(rname)} 数组")
                elif rname:
                    out.append(f"**响应**：{_anchor(rname)}")
                else:
                    # 没有具名模型（如 map<string,int>），至少把类型写出来，
                    # 不能只留一个「响应」二字
                    out.append(f"**响应**：{_type_of(rsch, spec)}")
                out.append("")
                if rname:
                    used.add(rname)

            elif "text/event-stream" in content:
                out.append("**响应**：`text/event-stream`（SSE 长连接）")
                out.append("")
                if ok.get("description"):
                    out.append(str(ok["description"]).strip())
                    out.append("")

            out.append("---")
            out.append("")

    # 附录：把端点引用到的模型（含其嵌套引用）各展开一次
    schemas = (spec.get("components") or {}).get("schemas") or {}
    queue, wanted = list(used), set()
    while queue:
        n = queue.pop()
        if n in wanted or n not in schemas:
            continue
        wanted.add(n)
        queue += [x for x in _collect_refs(schemas[n]) if x not in wanted]

    out += [
        "## 数据模型",
        "",
        f"共 {len(wanted)} 个模型，按名称排序。端点处的类型链接都指向这里。",
        "",
    ]
    for name in sorted(wanted):
        out.append(f"### {name}")
        out.append("")
        _field_table(name, spec, out)

    return "\n".join(out)


def main() -> int:
    import warnings

    warnings.filterwarnings("ignore")
    import backend.api.main as m

    spec = m.app.openapi()
    md = build(spec)
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "API接口文档.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(md, encoding="utf-8", newline="\n")
    print(f"已生成 {dest}（{len(md):,} 字符，{md.count(chr(10)):,} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
