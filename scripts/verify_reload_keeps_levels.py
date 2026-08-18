"""证明「重灌图数据不会再抹掉产品档」——2026-08-18 那次事故的回归闸门。

事故复盘
--------
8-14 手工回填 8919 个 skill_level 节点，可评分岗位 117 → 608。
8-18 有人重灌了一次库，`pg_store/migrate.py` 的 `attrs = EXCLUDED.attrs`
把源 SQLite 里的旧形态原样盖了回去，数字**逐位退回**回填前。
全程零报错——覆盖是「成功」的，只是产品档没了。

补丁是把归一挂到 `migrate.py` 的必经之路上（`backend/kg/level_scale`）。
本脚本验证那个补丁**真的生效**，而不是只验证归一函数单独跑得对
（那部分由 `tests/unit/test_level_scale.py` 锁）。

做法：临时库 + 旧形态源
-----------------------
1. 建一个临时 PG 库（不碰正式库）
2. 复制一份 `kg.sqlite`，把 CN 的 skill_level 节点**改回旧形态**（删掉 attrs.level）
3. 对临时库跑一次完整的 `migrate --clear`
4. 断言临时库里的产品档是齐的 —— 说明灌库路径自己把它补上了
5. 删临时库与副本

补丁生效前跑这个脚本会红（临时库里 0 个节点有产品档）。

用法：python -X utf8 scripts/verify_reload_keeps_levels.py
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import warnings
from pathlib import Path
from urllib.parse import urlparse, urlunparse

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from backend.kg.pg_store.config import DATABASE_URL, SQLITE_PATH  # noqa: E402

SCRATCH_DB = "voced_kg_reloadtest"
SCRATCH_SQLITE = ROOT / ".kg_oldshape.sqlite"


def _dsn_for(db: str) -> str:
    p = urlparse(DATABASE_URL)
    return urlunparse(p._replace(path=f"/{db}"))


def _admin_conn():
    """连 `postgres` 维护库来建/删临时库；CREATE DATABASE 不能在事务里跑。"""
    c = psycopg.connect(_dsn_for("postgres"), row_factory=dict_row)
    c.autocommit = True
    return c


def make_old_shape_copy() -> int:
    """复制 SQLite 并把国标来源的 skill_level 改回「没有产品档」的旧形态。

    只删 `attrs.level` / `source_level_code`、把原码写回 `level_code` —— 这正是
    事故当天源文件的形态，也是覆盖之所以能得逞的唯一条件。

    **只削有国标原码的节点**（MOHRSS）。LLM_CN 那批本来就没有原码、产品档是直接
    生成的，把它们的 level 也删掉就真的找不回来了 —— 那不是事故形态，是在考归一
    「凭空猜一个档位」，而它本就不该猜。
    """
    if SCRATCH_SQLITE.exists():
        SCRATCH_SQLITE.unlink()
    shutil.copy2(SQLITE_PATH, SCRATCH_SQLITE)

    conn = sqlite3.connect(str(SCRATCH_SQLITE))
    conn.row_factory = sqlite3.Row
    try:
        ups = []
        for r in conn.execute(
            "SELECT id, attrs FROM nodes WHERE type='skill_level' AND region='CN'"
        ):
            try:
                a = json.loads(r["attrs"]) if isinstance(r["attrs"], str) else (r["attrs"] or {})
            except Exception:
                continue
            if not isinstance(a, dict):
                continue
            code = a.pop("source_level_code", None) or a.get("level_code")
            if not code:
                continue                      # 无原码：不是国标来源，跳过
            a.pop("level", None)
            a["level_code"] = code
            ups.append((json.dumps(a, ensure_ascii=False), r["id"]))
        conn.executemany("UPDATE nodes SET attrs=? WHERE id=?", ups)
        conn.commit()
    finally:
        conn.close()
    return len(ups)


def count_levels(dsn: str) -> tuple[int, int]:
    """(带产品档的 skill_level 数, skill_level 总数)"""
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        r = c.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN (attrs::json->>'level') ~ '^[1-5]$' THEN 1 ELSE 0 END) AS lv "
            "FROM kg_node WHERE type='skill_level'"
        ).fetchone()
    return int(r["lv"] or 0), int(r["total"])


def main() -> int:
    scratch_dsn = _dsn_for(SCRATCH_DB)

    print(f"1) 建临时库 {SCRATCH_DB}")
    try:
        with _admin_conn() as c:
            c.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')
            c.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
    except Exception as e:
        print(f"   [跳过] 建不了临时库：{e}")
        print("   这个脚本需要 CREATEDB 权限；没有的话只能靠单测锁归一逻辑。")
        return 2

    try:
        n = make_old_shape_copy()
        print(f"2) 造旧形态源：{SCRATCH_SQLITE.name}，{n} 个国标 skill_level 已删掉 attrs.level")

        # 事故当天的源就是这个样子：国标那批一个产品档都没有
        sc = sqlite3.connect(str(SCRATCH_SQLITE))   # 注意：with 只管事务，不关连接
        try:
            src_bad = sc.execute(
                "SELECT COUNT(*) FROM nodes WHERE type='skill_level' AND region='CN' "
                "AND json_extract(attrs,'$.level_code') IS NOT NULL "
                "AND json_extract(attrs,'$.level') IS NOT NULL"
            ).fetchone()[0]
        finally:
            sc.close()
        print(f"   源里「有国标原码却已带产品档」的：{src_bad}（应为 0，否则这次验证不成立）")
        if src_bad:
            print("   [中止] 旧形态没造干净，验证无意义")
            return 1

        print(f"3) 对临时库跑 migrate --clear（源用旧形态副本）")
        # client 在 import 时把 DSN 绑进了模块命名空间，改这里即可指向临时库；
        # 不能用环境变量——settings 以 override=True 加载 .env，会盖掉（见 CLAUDE.md）
        from backend.kg.pg_store import client as pg_client

        pg_client.DATABASE_URL = scratch_dsn
        from backend.kg.pg_store import migrate as mig

        sys.argv = ["migrate", "--clear", "--sqlite", str(SCRATCH_SQLITE), "--region", "CN"]
        mig.main()

        print("4) 校验临时库")
        lv, total = count_levels(scratch_dsn)
        print(f"   skill_level 带产品档 {lv}/{total}")

        print(f"\n{'=' * 56}")
        if total and lv == total:
            print("通过：源里一个产品档都没有，灌完库全齐 —— 灌库路径自愈，重灌不再抹掉回填")
            return 0
        print(f"失败：{total - lv} 个节点灌完仍无产品档 —— 归一没挂在灌库路径上")
        print("  这正是 8-18 那次回填消失的形状，别放过")
        return 1
    finally:
        # 连接池还握着临时库的连接，不关就 DROP 不掉（"being accessed by other users"）
        try:
            from backend.kg.pg_store import client as _c
            _c.close_pool()
        except Exception:
            pass
        try:
            with _admin_conn() as c:
                c.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (SCRATCH_DB,),
                )
                c.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')
            print(f"\n已清理临时库 {SCRATCH_DB}")
        except Exception as e:
            print(f"\n[注意] 临时库没删掉，手动清理：DROP DATABASE {SCRATCH_DB};  ({e})")
        try:
            SCRATCH_SQLITE.unlink(missing_ok=True)
        except OSError as e:
            print(f"[注意] 副本没删掉：{SCRATCH_SQLITE}  ({e})")


if __name__ == "__main__":
    raise SystemExit(main())
