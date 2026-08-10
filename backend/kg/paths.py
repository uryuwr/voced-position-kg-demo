"""Project paths."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
RAW = DATA / "raw"
STAGING = DATA / "staging"
GRAPH = DATA / "graph"
SEEDS = DATA / "seeds"
SCHEMAS = ROOT / "schemas"
REPORTS = ROOT / "reports"


def ensure_dirs() -> None:
    for p in (
        RAW / "US" / "onet",
        RAW / "EU" / "esco",
        RAW / "CN" / "moe",
        RAW / "CN" / "mohrss",
        STAGING,
        GRAPH,
        SEEDS / "it_ai",
        REPORTS,
    ):
        p.mkdir(parents=True, exist_ok=True)
