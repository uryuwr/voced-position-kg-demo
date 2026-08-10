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


# ── 状态可见性三级规则 ────────────────────────────────────────
# archived  = 逻辑删除：**任何接口都不返回**，只留在库里，恢复需直接改库
# draft      = 草稿：仅管理台可见
# disabled   = 停用：仅管理台可见（停用后可再发布）
# published  = 发布态：前台与管理台都可见
PUBLIC_STATUSES = ("published",)
ADMIN_STATUSES = ("published", "draft", "disabled")
ARCHIVED_STATUS = "archived"


def edge_published(alias: str = "e") -> str:
    """【前台】边可见性：仅 published。

    `status` 为 NULL 的历史数据视为 published。
    读路径的每个 kg_edge 查询都要带上，否则归档形同虚设——只过滤节点挡不住边，
    比如 industry→industry 的 parent_of 两端都是正常节点。
    """
    return f"COALESCE({alias}.status, 'published') = 'published'"


def edge_not_archived(alias: str = "e") -> str:
    """【管理台】边可见性：除 archived 外都可见（含 draft / disabled）。"""
    return f"COALESCE({alias}.status, 'published') <> '{ARCHIVED_STATUS}'"


def node_published(alias: str = "") -> str:
    """【前台】节点可见性：仅 published。alias 为空时用于无别名的 kg_node 查询。"""
    col = f"{alias}.status" if alias else "status"
    return f"COALESCE({col}, 'published') = 'published'"


def node_not_archived(alias: str = "") -> str:
    """【管理台】节点可见性：除 archived 外都可见。"""
    col = f"{alias}.status" if alias else "status"
    return f"COALESCE({col}, 'published') <> '{ARCHIVED_STATUS}'"
