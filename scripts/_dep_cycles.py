"""一次性：扫 backend/ 的模块依赖，找双向依赖（2 环）与被跨模块引用的私有名。

判断「分层守没守住」不能靠读代码印象，得看实际的 import 图。
"""
from __future__ import annotations

import ast
import collections
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
BE = ROOT / "backend"


def modname(p: pathlib.Path) -> str:
    rel = p.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def main() -> None:
    top: dict[str, set[str]] = collections.defaultdict(set)   # 模块级 import
    local: dict[str, set[str]] = collections.defaultdict(set)  # 函数内 import
    private: list[tuple[str, str, str]] = []

    for f in sorted(BE.rglob("*.py")):
        if "__pycache__" in str(f):
            continue
        mod = modname(f)
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        # 标记哪些节点在函数体内
        inner: set[int] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef):
                for sub in ast.walk(n):
                    inner.add(id(sub))
        for n in ast.walk(tree):
            if not isinstance(n, ast.ImportFrom) or not n.module:
                continue
            if not n.module.startswith("backend"):
                continue
            (local if id(n) in inner else top)[mod].add(n.module)
            for a in n.names:
                if a.name.startswith("_") and not a.name.startswith("__"):
                    private.append((mod, n.module, a.name))

    allm: dict[str, set[str]] = collections.defaultdict(set)
    for d in (top, local):
        for k, v in d.items():
            allm[k] |= v

    cyc = {
        tuple(sorted((a, b)))
        for a, deps in allm.items() for b in deps
        if a in allm.get(b, set())
    }
    print(f"=== 双向依赖 {len(cyc)} 对 ===")
    for a, b in sorted(cyc):
        ta = "模块级" if b in top.get(a, set()) else "函数内"
        tb = "模块级" if a in top.get(b, set()) else "函数内"
        print(f"  {a.replace('backend.', '')} ({ta})  ⇄  {b.replace('backend.', '')} ({tb})")

    print(f"\n=== 被跨模块 import 的私有名 {len(private)} 处 ===")
    byname: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for src, dst, name in private:
        byname[(dst, name)].append(src.replace("backend.", ""))
    for (dst, name), srcs in sorted(byname.items(), key=lambda kv: -len(kv[1])):
        print(f"  {dst.replace('backend.', '')}.{name}  ← {len(srcs)} 处：{', '.join(sorted(set(srcs)))}")


if __name__ == "__main__":
    main()
