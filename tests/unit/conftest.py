"""单元测试公共装置。

按顺序做四件事，顺序不能换（都必须赶在 `backend.settings` 被首次 import 之前）：

1. **把仓库根放进 sys.path** —— `import backend.*` 才找得到。
2. **把 DATABASE_URL 指到一个必然连不上的地址** —— 仓库根 `.env` 里躺着一串**真实**
   连接串，`settings._load_env_files()` 会把它读进环境变量。不覆盖的话，凡是走
   `try/except` 兜底的读路径（`skill_keywords.kg_recall`、`biz_store._parse_resume_skills`
   …）都会**真的连上库**，单测结果随线上数据而变，还可能写脏数据。
   指到 127.0.0.1:1 后连接立即被拒，这些路径确定性地走降级分支。
3. **掐断 SDP 配置中心的网络拉取** —— `backend/settings.py` 在 import 期就会去
   `sdp-cloud-config-server` 拉业务配置键。单测必须离线、确定：没有内网的机器上不能挂，
   也不能因为配置中心改了个值就让断言飘。
4. **stdout 转 UTF-8** —— 断言信息里全是中文。注意 pytest 的 TerminalWriter 在
   conftest 之前就抓好了原始流，所以这里只能兜住一部分；**推荐用
   `python -X utf8 -m pytest`**（见 pytest.ini 顶部注释）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ── 1) 编码（尽力而为；权威做法是 python -X utf8 -m pytest）──────
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):  # pragma: no cover
        pass

# ── 2) 任何用例都不许连库 ──────────────────────────────────────
# 端口 1 上不会有 postgres，connect 立即被拒（不是超时挂住）。
UNREACHABLE_DB = "postgresql://unit:test@127.0.0.1:1/unit_tests_must_not_touch_db?connect_timeout=1"
os.environ["DATABASE_URL"] = UNREACHABLE_DB


# ── 3) 离线：SDP 配置中心不参与单测 ────────────────────────────
class _SdpOfflineInUnitTests(RuntimeError):
    """让 settings 的 bootstrap 走 except 分支（SDP_CONFIG_FAIL_FAST=False → 返回 None）。"""


def _no_network(**_kwargs):  # noqa: ANN003
    raise _SdpOfflineInUnitTests(
        "单元测试禁止访问 SDP 配置中心（见 tests/unit/conftest.py）"
    )


import backend.sdp_config as _sdp_config  # noqa: E402

_sdp_config.fetch_config = _no_network  # type: ignore[assignment]

import backend.settings as _settings  # noqa: E402,F401  触发 .env 加载并固化模块常量

assert _settings.config_source() == "local", (
    "SDP 拉取没有被掐断，单测将依赖内网配置中心；请检查 conftest 的 patch 顺序"
)

import backend.kg.pg_store.config as _pg_config  # noqa: E402

assert _pg_config.DATABASE_URL == UNREACHABLE_DB, (
    f"单测的 DATABASE_URL 被 .env / 配置中心盖掉了（当前 {_pg_config.DATABASE_URL[:40]}…）；"
    "这会让 try/except 兜底的读路径真的连上库"
)


# ── 3) 共享的小工具 ────────────────────────────────────────────
import pytest  # noqa: E402


@pytest.fixture
def req_item():
    """构造一条「岗位技能要求」，字段名与 occupation_skill_bundles 的输出一致。"""

    def _make(
        skill_key: str,
        *,
        weight: float = 0.25,
        required_level: int | None = 3,
        category: str = "生产准备",
        **extra,
    ) -> dict:
        return {
            "skill_key": skill_key,
            "category": category,
            "required_level": required_level,
            "weight": weight,
            **extra,
        }

    return _make


class FakeConn:
    """极简 psycopg 连接替身：记录 SQL，按队列吐 fetchone/fetchall 结果。

    只用于「断言查询里带没带可见性条件」这类不需要真库的用例。
    """

    def __init__(self, rows: list | None = None, one=None):
        self.sql: list[str] = []
        self.params: list = []
        self._rows = rows if rows is not None else []
        self._one = one

    # psycopg: conn.execute(...) -> cursor
    def execute(self, sql, params=None):
        self.sql.append(str(sql))
        self.params.append(params)
        return self

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        if self._one is not None:
            return self._one
        return self._rows[0] if self._rows else None

    def commit(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_conn():
    return FakeConn


SETTINGS_PY = REPO_ROOT / "backend" / "settings.py"

# 这些键由 settings 探针接管：每次求值前先清空，避免本机 .env / 进程环境污染断言
_PROBE_KEYS = (
    "DEBUG", "AUTH_DEBUG", "AUTH_BYPASS",
    "VERIFY_TLS", "TLS_CA_BUNDLE",
    "SERVE_DEV_UI", "REVIEW_REQUIRED",
)


@pytest.fixture
def settings_probe(monkeypatch):
    """用**完全受控**的环境变量重新执行一遍 backend/settings.py，返回新模块对象。

    settings 的配置是模块级常量、import 即固化，想测「换个环境变量会怎样」只能重执行。
    两处隔离缺一不可：

    - `.env` 加载整个掐掉。`backend/.env` 是 `override=True` 的，本机把
      `VERIFY_TLS=0` 写在里面，不掐的话「默认开启 TLS 校验」这条断言在本机永远是红的，
      而在干净镜像里是绿的 —— 测试结论取决于谁的机器，没有意义。
    - SDP 配置中心已在 conftest 顶部掐断，重执行不走网络。

    用法：`settings_probe(AUTH_BYPASS="1")`；未显式给的 `_PROBE_KEYS` 一律按未设置处理。
    """
    import dotenv

    def _probe(**env):
        monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: True)
        monkeypatch.setattr(dotenv, "dotenv_values", lambda *a, **k: {})
        for k in _PROBE_KEYS:
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)

        import importlib.util

        spec = importlib.util.spec_from_file_location("_settings_probe", SETTINGS_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    return _probe


@pytest.fixture
def no_kg_recall(monkeypatch):
    """关掉「先查技能库召回」这一层，只测关键词兜底。

    两条诊断路径（`biz_store._parse_resume_skills` / `diagnose._rule_parse`）现在都
    经由 `agent.skill_keywords.rule_parse_skills` → 模块级 `kg_recall`，
    patch 这一个点两边都覆盖到。
    """
    from backend.agent import skill_keywords

    monkeypatch.setattr(skill_keywords, "kg_recall", lambda text, limit=12: [])
