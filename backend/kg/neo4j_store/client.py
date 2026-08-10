"""Neo4j driver helpers."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from neo4j import GraphDatabase, Driver

from backend.kg.neo4j_store.config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER

_driver: Driver | None = None


def get_driver() -> Driver:
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    return _driver


def close_driver() -> None:
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None


@contextmanager
def session(database: str = "neo4j") -> Iterator[Any]:
    drv = get_driver()
    s = drv.session(database=database)
    try:
        yield s
    finally:
        s.close()


def verify_connectivity() -> dict[str, Any]:
    drv = get_driver()
    drv.verify_connectivity()
    with session() as s:
        rec = s.run("RETURN 1 AS ok").single()
        return {"ok": bool(rec and rec["ok"] == 1), "uri": NEO4J_URI}


def ensure_constraints() -> None:
    """Idempotent constraints / indexes for entity gid."""
    stmts = [
        "CREATE CONSTRAINT entity_gid IF NOT EXISTS FOR (n:Entity) REQUIRE n.gid IS UNIQUE",
        "CREATE INDEX entity_region IF NOT EXISTS FOR (n:Entity) ON (n.region)",
        "CREATE INDEX entity_type IF NOT EXISTS FOR (n:Entity) ON (n.type)",
        "CREATE INDEX entity_name IF NOT EXISTS FOR (n:Entity) ON (n.name)",
        "CREATE INDEX major_name IF NOT EXISTS FOR (n:Major) ON (n.name)",
        "CREATE INDEX occupation_name IF NOT EXISTS FOR (n:Occupation) ON (n.name)",
    ]
    with session() as s:
        for q in stmts:
            s.run(q)
