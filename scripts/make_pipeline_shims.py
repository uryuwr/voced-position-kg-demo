"""Generate thin pipelines.* shims pointing to backend/crawlers (backward compatible)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (shim module path relative to root, import path of real module)
SHIMS = [
    ("pipelines/api/main.py", "backend.api.main"),
    ("pipelines/neo4j_store/migrate.py", "backend.kg.neo4j_store.migrate"),
    ("pipelines/neo4j_store/query.py", "backend.kg.neo4j_store.query"),
    ("pipelines/neo4j_store/client.py", "backend.kg.neo4j_store.client"),
    ("pipelines/neo4j_store/config.py", "backend.kg.neo4j_store.config"),
    ("pipelines/common/paths.py", "backend.kg.paths"),
    ("pipelines/common/provenance.py", "backend.kg.provenance"),
    ("pipelines/common/graph_store.py", "backend.kg.graph_store"),
    ("pipelines/common/download.py", "crawlers.common.download"),
]


def shim_body(target: str) -> str:
    return f'''"""Compatibility shim → `{target}`. Prefer the new path in new code."""
from __future__ import annotations

import runpy
from importlib import import_module

_mod = import_module("{target}")
globals().update({{k: v for k, v in vars(_mod).items() if not k.startswith("_")}})

if __name__ == "__main__":
    runpy.run_module("{target}", run_name="__main__")
'''


def main() -> None:
    # auto cn/eu/us/maintenance/seed modules
    for pkg, prefix in [
        ("cn", "crawlers.cn"),
        ("eu", "crawlers.eu"),
        ("us", "crawlers.us"),
        ("maintenance", "crawlers.maintenance"),
        ("seed", "crawlers.seed"),
    ]:
        src = ROOT / "crawlers" / pkg
        if not src.is_dir():
            continue
        for py in src.glob("*.py"):
            if py.name == "__init__.py":
                continue
            mod = py.stem
            SHIMS.append((f"pipelines/{pkg}/{mod}.py", f"{prefix}.{mod}"))

    for rel, target in SHIMS:
        path = ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(shim_body(target), encoding="utf-8")
        print("shim", rel, "->", target)

    for pkg in ("pipelines", "pipelines/api", "pipelines/common", "pipelines/neo4j_store",
                "pipelines/cn", "pipelines/eu", "pipelines/us", "pipelines/maintenance", "pipelines/seed"):
        init = ROOT / pkg / "__init__.py"
        init.parent.mkdir(parents=True, exist_ok=True)
        if not init.exists() or "Compatibility" not in init.read_text(encoding="utf-8", errors="ignore"):
            init.write_text(
                '"""Compatibility package. Prefer `backend.*` / `crawlers.*`."""\n',
                encoding="utf-8",
            )
    print("done", len(SHIMS), "shims")


if __name__ == "__main__":
    main()
