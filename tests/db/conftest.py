"""连库测试的装置（`tests/db/`）。

为什么单独一个目录：`tests/unit/conftest.py` 刻意把 `DATABASE_URL` 指到死地址，
好让所有 try/except 兜底路径确定地走降级分支。而草稿态的核心断言全是「库里两行、
接口只看见一行」，**必须真连库**。两种要求互斥，只能分目录。

    python -X utf8 -m pytest tests/db -q      # 连 backend/.env 里那个库

pytest.ini 的 `testpaths = tests/unit`，所以默认全量跑**不会**碰到这里；
要跑得显式给路径。这是有意的：CI 上没有库的环境不该因为连不上而红。

安全闸门
--------
这些用例会 INSERT / UPDATE / DELETE `kg_node` / `kg_edge`。误连到共享库
（`voced_kg` / 预生产那个与 bcs-ai-agent 共用的实例）会污染别人的数据，
所以库名不含 `draft` / `test` 时**直接 SKIP 整个目录**，除非显式
`VOCED_ALLOW_DB_TESTS=1`。宁可少跑，不要写脏共享库。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):  # pragma: no cover
        pass


def _db_name() -> str:
    from backend.kg.pg_store.config import DATABASE_URL

    return (DATABASE_URL.rsplit("/", 1)[-1] or "").split("?")[0]


def pytest_collection_modifyitems(config, items):  # noqa: ANN001, ARG001
    name = _db_name()
    if os.getenv("VOCED_ALLOW_DB_TESTS") == "1":
        return
    if any(k in name.lower() for k in ("draft", "test", "tmp")):
        return
    skip = pytest.mark.skip(
        reason=(
            f"DATABASE_URL 指向 {name!r}，不像隔离库。这些用例会写 kg_node/kg_edge，"
            "误连共享库会污染别人的数据。确认无误请置 VOCED_ALLOW_DB_TESTS=1"
        )
    )
    for it in items:
        it.add_marker(skip)


@pytest.fixture(scope="session")
def db_ready() -> str:
    """跑一遍启动期的幂等 DDL，返回库名。

    这也是「服务起来时会发生什么」的真实复现：本项目没有 migration 框架，
    表结构靠每次启动的 `ensure_schema()` 演进（见 CLAUDE.md）。
    """
    from backend.kg.pg_store.client import ensure_schema

    ensure_schema(force=True)
    return _db_name()


@pytest.fixture(scope="session")
def caps(db_ready) -> dict:  # noqa: ANN001
    from tests._draft_probe import db_capabilities

    return db_capabilities()


@pytest.fixture(scope="session")
def client(db_ready):  # noqa: ANN001
    from tests._draft_probe import make_client

    return make_client()


@pytest.fixture(scope="session")
def app(db_ready):  # noqa: ANN001
    from tests._draft_probe import get_app

    return get_app()


@pytest.fixture(scope="session")
def real_ids(db_ready) -> dict:  # noqa: ANN001
    from tests._draft_probe import pick_real_ids

    ids = pick_real_ids()
    if not ids.get("occupation_id"):
        pytest.skip("库里挑不到「有 3–8 条 requires 边的已发布岗位」，没有可用受试对象")
    return ids


@pytest.fixture
def draft_rows(caps):  # noqa: ANN001
    """直接插草稿行的低层工具（不经应用层，模拟「库里就是这个形态」）。

    `try/finally` 清理写在 fixture 里，用例怎么失败都不留残留 —— 否则第二次跑撞主键。
    """
    if not caps["has_is_draft"]:
        pytest.skip("kg_node/kg_edge 没有 is_draft 列：方案 §2 的 DDL 尚未落地")
    from tests import _draft_probe as probe

    made: list[probe.DraftFixture] = []

    def _make(mode: str = "row", shadow_of: str | None = None) -> probe.DraftFixture:
        fx = probe.install_draft_fixture(mode, shadow_of=shadow_of)
        made.append(fx)
        return fx

    try:
        yield _make
    finally:
        probe.remove_draft_fixture()
