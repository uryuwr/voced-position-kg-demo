"""One-shot import rewrite after monorepo split."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REPLACEMENTS = [
    (r"from pipelines\.common\.paths", "from backend.kg.paths"),
    (r"import pipelines\.common\.paths", "import backend.kg.paths"),
    (r"from pipelines\.common\.provenance", "from backend.kg.provenance"),
    (r"from pipelines\.common\.graph_store", "from backend.kg.graph_store"),
    (r"from pipelines\.common\.download", "from crawlers.common.download"),
    (r"from pipelines\.neo4j_store", "from backend.kg.neo4j_store"),
    (r"import pipelines\.neo4j_store", "import backend.kg.neo4j_store"),
    (r"from pipelines\.cn\.", "from crawlers.cn."),
    (r"from pipelines\.eu\.", "from crawlers.eu."),
    (r"from pipelines\.us\.", "from crawlers.us."),
    (r"from pipelines\.maintenance\.", "from crawlers.maintenance."),
    (r"from pipelines\.seed\.", "from crawlers.seed."),
    (r"pipelines\.api\.main", "backend.api.main"),
    (r"pipelines\.neo4j_store", "backend.kg.neo4j_store"),
    (r"pipelines\.cn\.", "crawlers.cn."),
    (r"pipelines\.eu\.", "crawlers.eu."),
    (r"pipelines\.us\.", "crawlers.us."),
    (r"pipelines\.maintenance\.", "crawlers.maintenance."),
]


def main() -> None:
    count = 0
    for root_name in ("backend", "crawlers", "scripts"):
        root = ROOT / root_name
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            text = p.read_text(encoding="utf-8")
            orig = text
            for a, b in REPLACEMENTS:
                text = re.sub(a, b, text)
            text = text.replace("backend.api.main:app", "backend.api.main:app")
            if text != orig:
                p.write_text(text, encoding="utf-8")
                count += 1
                print("updated", p.relative_to(ROOT))
    print("total", count)


if __name__ == "__main__":
    main()
