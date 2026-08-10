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
