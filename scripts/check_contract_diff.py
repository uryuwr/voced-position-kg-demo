"""对比当前 `app.openapi()` 与基线快照，确认重构没有改动对外契约。

内部重构（连接池、分层、去重）不该让任何接口的入参或出参变形。
肉眼 review diff 看不出这个——openapi.json 有几万行，改动可能藏在某个
嵌套模型的一个字段上。这里逐路径、逐字段机械比对。

用法：
    python -X utf8 scripts/check_contract_diff.py [基线文件]

默认基线 `.baseline_openapi.json`（仓库根，不入库）。
退出码非 0 表示契约有变，需要人工确认是有意为之还是回归。

采基线：
    python -X utf8 -c "import warnings,json,io;warnings.filterwarnings('ignore');\
import backend.api.main as m;\
io.open('.baseline_openapi.json','w',encoding='utf-8').write(\
json.dumps(m.app.openapi(),ensure_ascii=False,sort_keys=True))"
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

METHODS = ("get", "post", "put", "patch", "delete")


# 这两个键是**无序集合**语义，必须按成员比而不是按位置比。
# 否则往 enum 里插一个新值，会让它后面所有值的下标位移，
# 工具报成一串「取值变化」——每次加枚举都虚假告警一次，久了就没人看了。
SET_LIKE = ("enum", "required")


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """把嵌套结构摊平成 {点分路径: 标量}，便于逐字段比对。"""
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if k in SET_LIKE and isinstance(v, list):
                # 每个成员单独成一条 {路径}<成员>，成员增删才会体现为新增/消失
                for m in v:
                    out[f"{path}<{json.dumps(m, ensure_ascii=False, sort_keys=True)}>"] = True
                continue
            out |= flatten(v, path)
    elif isinstance(obj, list):
        # 列表按内容排序后再编号：字段顺序变化不该算契约变更
        try:
            items = sorted(obj, key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False))
        except TypeError:
            items = obj
        for i, v in enumerate(items):
            out |= flatten(v, f"{prefix}[{i}]")
    else:
        out[prefix] = obj
    return out


def endpoints(spec: dict) -> set[str]:
    return {
        f"{m.upper()} {p}"
        for p, ms in (spec.get("paths") or {}).items()
        for m in ms
        if m in METHODS
    }


def main() -> int:
    base_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".baseline_openapi.json"
    if not base_path.exists():
        print(f"缺少基线 {base_path}；先按本文件 docstring 里的命令采一份")
        return 2

    import warnings

    warnings.filterwarnings("ignore")
    import backend.api.main as m

    base = json.loads(io.open(base_path, encoding="utf-8").read())
    now = m.app.openapi()

    # ① 端点增删
    b_eps, n_eps = endpoints(base), endpoints(now)
    gone, added = sorted(b_eps - n_eps), sorted(n_eps - b_eps)

    # ② 模型增删
    b_sch = set((base.get("components") or {}).get("schemas") or {})
    n_sch = set((now.get("components") or {}).get("schemas") or {})
    sch_gone, sch_added = sorted(b_sch - n_sch), sorted(n_sch - b_sch)

    # ③ 逐字段比对（忽略纯文案：description / summary 改了不算契约变更）
    def strip_prose(d: dict[str, Any]) -> dict[str, Any]:
        return {
            k: v for k, v in d.items()
            if not k.endswith((".description", ".summary", ".title"))
        }

    fb = strip_prose(flatten(base.get("paths") or {}, "paths"))
    fb |= strip_prose(flatten((base.get("components") or {}).get("schemas") or {}, "schemas"))
    fn = strip_prose(flatten(now.get("paths") or {}, "paths"))
    fn |= strip_prose(flatten((now.get("components") or {}).get("schemas") or {}, "schemas"))

    changed = sorted(k for k in fb.keys() & fn.keys() if fb[k] != fn[k])
    removed = sorted(fb.keys() - fn.keys())
    new = sorted(fn.keys() - fb.keys())

    def show(title: str, items: list, fmt=lambda x: f"  {x}") -> None:
        if items:
            print(f"\n## {title}（{len(items)}）")
            for x in items[:40]:
                print(fmt(x))
            if len(items) > 40:
                print(f"  …… 另有 {len(items) - 40} 条")

    show("端点消失（破坏性）", gone)
    show("端点新增", added)
    show("模型消失（破坏性）", sch_gone)
    show("模型新增", sch_added)
    show("字段取值变化", changed, lambda k: f"  {k}\n      基线 {fb[k]!r}  →  现在 {fn[k]!r}")
    show("字段消失（破坏性）", removed, lambda k: f"  {k}  (基线值 {fb[k]!r})")
    show("字段新增", new, lambda k: f"  {k}  (现值 {fn[k]!r})")

    breaking = len(gone) + len(sch_gone) + len(removed) + len(changed)
    total = breaking + len(added) + len(sch_added) + len(new)
    print(f"\n{'=' * 56}")
    if total == 0:
        print("契约零变更 ✔")
        return 0
    print(f"契约有变：破坏性 {breaking} 处，新增 {len(added) + len(sch_added) + len(new)} 处")
    print("纯新增通常安全；破坏性变更必须确认是有意为之。")
    return 1 if breaking else 0


if __name__ == "__main__":
    raise SystemExit(main())
