"""PostgreSQL connection config from env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from backend.kg.paths import ROOT

load_dotenv(ROOT / ".env")

# Default local docker: docker run ... -e POSTGRES_USER=voced ...
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://voced:<your-password>@localhost:5432/voced_kg",
)
SQLITE_PATH = Path(os.getenv("SQLITE_PATH", str(ROOT / "data" / "graph" / "kg.sqlite")))
if not SQLITE_PATH.is_absolute():
    SQLITE_PATH = ROOT / SQLITE_PATH

# API / migrate default region scope
DEFAULT_REGION = os.getenv("KG_REGION", "CN")


def edge_published(alias: str = "e") -> str:
    """边的对外可见性过滤片段。

    `status` 为 NULL 的历史数据视为 published；`draft` / `archived` / `disabled`
    一律不对外返回——归档的边只留在库里，接口不吐。

    读路径的每个 kg_edge 查询都要带上，否则归档形同虚设（只过滤节点挡不住边，
    比如 industry→industry 的 parent_of 两端都是正常节点）。
    管理端要看全量走各自的 scope=manage 分支，不用这个片段。
    """
    return f"COALESCE({alias}.status, 'published') = 'published'"
