"""Neo4j connection config from env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from backend.kg.paths import ROOT

load_dotenv(ROOT / ".env")

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
SQLITE_PATH = Path(os.getenv("SQLITE_PATH", str(ROOT / "data" / "graph" / "kg.sqlite")))
if not SQLITE_PATH.is_absolute():
    SQLITE_PATH = ROOT / SQLITE_PATH

# SQLite type -> Neo4j label
TYPE_TO_LABEL = {
    "industry": "Industry",
    "major": "Major",
    "occupation": "Occupation",
    "skill_level": "SkillLevel",
    "course": "Course",
    "credential": "Credential",
}

# SQLite rel_type -> Neo4j relationship type
REL_TO_TYPE = {
    "prepares_for": "PREPARES_FOR",
    "requires": "REQUIRES",
    "covers": "COVERS",
    "belongs_to": "BELONGS_TO",
    "parent_of": "PARENT_OF",
    "taught_by": "TAUGHT_BY",
    "leads_to": "LEADS_TO",
    "recognized_by": "RECOGNIZED_BY",
    "articulates_to": "ARTICULATES_TO",
    "related_to": "RELATED_TO",
    "same_as": "SAME_AS",
}
