"""psycopg connection helpers（进程级连接池）。"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from types import TracebackType
from typing import Any, Iterator

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.string import StrDumper
from psycopg_pool import ConnectionPool

from backend import settings
from backend.kg.pg_store.config import DATABASE_URL

SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS kg_node (
  id TEXT PRIMARY KEY,
  region TEXT NOT NULL,
  type TEXT NOT NULL,
  name TEXT NOT NULL,
  name_en TEXT,
  name_zh TEXT,
  aliases TEXT,
  description TEXT,
  attrs TEXT,
  source_system TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_url TEXT NOT NULL,
  license TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  confidence TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kg_edge (
  id TEXT PRIMARY KEY,
  src_id TEXT NOT NULL REFERENCES kg_node(id),
  dst_id TEXT NOT NULL REFERENCES kg_node(id),
  rel_type TEXT NOT NULL,
  region TEXT NOT NULL,
  weight DOUBLE PRECISION,
  evidence TEXT,
  attrs TEXT,
  source_system TEXT NOT NULL,
  source_id TEXT,
  source_url TEXT NOT NULL,
  license TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  confidence TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kg_node_region_type ON kg_node(region, type);
CREATE INDEX IF NOT EXISTS idx_kg_node_name_lower ON kg_node(lower(name));
CREATE INDEX IF NOT EXISTS idx_kg_node_source ON kg_node(source_system, source_id);
CREATE INDEX IF NOT EXISTS idx_kg_edge_src ON kg_edge(src_id);
CREATE INDEX IF NOT EXISTS idx_kg_edge_dst ON kg_edge(dst_id);
CREATE INDEX IF NOT EXISTS idx_kg_edge_rel ON kg_edge(rel_type);
CREATE INDEX IF NOT EXISTS idx_kg_edge_region ON kg_edge(region);

-- 发布状态：published | draft | archived（迁移数据默认 published）
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'published';
ALTER TABLE kg_edge ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'published';
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS updated_by TEXT;
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS updated_by_name TEXT;
ALTER TABLE kg_edge ADD COLUMN IF NOT EXISTS updated_by TEXT;
ALTER TABLE kg_edge ADD COLUMN IF NOT EXISTS updated_by_name TEXT;

-- 图布局/懒加载：同层稳定序 + 下级数量（能力图「向下」语义）
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS sort_order INT;
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS child_count INT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_kg_node_type_sort
  ON kg_node(region, type, sort_order NULLS LAST, name);

-- 能力全景重构：岗位层级(晋升递进) / 技能分类 / 边结构层
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS level INT;         -- occupation: 岗位层级 1..N；skill_level 可复用为 L 序
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS category TEXT;     -- skill: 技能大类（运营/数据/内容/商业/技术/通用…）
ALTER TABLE kg_edge ADD COLUMN IF NOT EXISTS structure_layer TEXT; -- tree(归属) | net(关联) | chain(进阶)
CREATE INDEX IF NOT EXISTS idx_kg_edge_layer ON kg_edge(structure_layer);
CREATE INDEX IF NOT EXISTS idx_kg_node_category ON kg_node(type, category);

-- 创建时间：管理台列表要「最新建的排最前」。
-- 原先只能按 sort_order/name 排，而新建节点 sort_order 为 NULL，
-- 配合 NULLS LAST 会被甩到最后一页，运营看不到刚建的数据。
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE kg_edge ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
-- 历史数据用采集时间回填（fetched_at 为 TEXT ISO8601，转换失败则留空）
UPDATE kg_node SET created_at = NULLIF(fetched_at, '')::timestamptz
 WHERE created_at IS NULL AND fetched_at ~ '^\d{4}-\d{2}-\d{2}';
UPDATE kg_edge SET created_at = NULLIF(fetched_at, '')::timestamptz
 WHERE created_at IS NULL AND fetched_at ~ '^\d{4}-\d{2}-\d{2}';
CREATE INDEX IF NOT EXISTS idx_kg_node_created ON kg_node(type, created_at DESC);

-- 原型「专业管理 / 技能库」列表有「版本」「负责人」两列，库内此前没有对应字段。
-- version：发布版本号，从 1 起，每次成功发布（status → published）+1
-- owner / owner_name：业务负责人，新建时默认取创建人，可在编辑页改
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 1;
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS owner TEXT;
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS owner_name TEXT;

-- ── 草稿态：同一条记录最多两行 ─────────────────────────────
-- 方案见 docs/方案-管理台草稿态与发布.md。线上行 is_draft=false，草稿行 is_draft=true
-- 且 **status 恒为 'draft'** —— 全仓约 120 处前台查询已经在过滤 status='published'，
-- 靠这条不变量自动把草稿挡在前台之外，那些查询一处都不用改。
-- 一旦有人把「发布后应变成什么」写进草稿行的 status，草稿当场泄漏到前台，
-- 所以那个意图存在 target_status 里。
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS is_draft BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS target_status TEXT;
ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS base_version INT;
ALTER TABLE kg_edge ADD COLUMN IF NOT EXISTS is_draft BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE kg_edge ADD COLUMN IF NOT EXISTS target_status TEXT;
-- 发布单元 = 一个节点草稿行 + 所有 unit_id 指向它的草稿边（约定 unit_id = src_id）
ALTER TABLE kg_edge ADD COLUMN IF NOT EXISTS unit_id TEXT;

-- 主键从 id 变成 (id, is_draft)，两个外键因此**无法成立，只能删**：PostgreSQL 的外键
-- 只能引用完整主键或唯一约束，引用不了 `UNIQUE(id) WHERE NOT is_draft` 这种部分索引。
-- 代价是「边的两端一定存在」从此靠应用层保证（发布时校验端点 + scripts/check_orphan_edges.py）。
ALTER TABLE kg_edge DROP CONSTRAINT IF EXISTS kg_edge_src_id_fkey;
ALTER TABLE kg_edge DROP CONSTRAINT IF EXISTS kg_edge_dst_id_fkey;

-- ADD PRIMARY KEY 天生不幂等，而这份 DDL 每次进程启动都跑 —— 必须先判断有没有换过，
-- 否则第二次启动就 "multiple primary keys are not allowed"，服务起不来。
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_index i
    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
    WHERE i.indrelid = 'kg_node'::regclass AND i.indisprimary AND a.attname = 'is_draft'
  ) THEN
    ALTER TABLE kg_node DROP CONSTRAINT IF EXISTS kg_node_pkey;
    ALTER TABLE kg_node ADD PRIMARY KEY (id, is_draft);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_index i
    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
    WHERE i.indrelid = 'kg_edge'::regclass AND i.indisprimary AND a.attname = 'is_draft'
  ) THEN
    ALTER TABLE kg_edge DROP CONSTRAINT IF EXISTS kg_edge_pkey;
    ALTER TABLE kg_edge ADD PRIMARY KEY (id, is_draft);
  END IF;
END $$;

-- 业务编码 attrs.code 唯一性：同 region+同 type 内不得重复（跨区域/跨类型允许重复，
-- 因为教育部专业码、大典职业码、BOSS 行业码是三套独立体系）。
-- 应用层在写入前已校验并返回 409；这里是并发兜底，避免两个请求同时通过检查。
-- 归档节点不参与占用，便于「归档后用同一编码重建」。
--
-- **草稿行必须排除在外**，否则「把编码从 A 改成 B」时草稿行会和自己的线上行相撞。
-- 于是草稿之间不互斥，两个草稿可以同时占 B —— 那道校验补在发布事务里（见 draft_publish）。
-- 用 DO 块而不是 CREATE UNIQUE INDEX IF NOT EXISTS：老库里已经有一个**不含 is_draft 条件**
-- 的同名索引，IF NOT EXISTS 会直接跳过、旧定义留在库里，改编码就报唯一冲突。
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = current_schema()
      AND indexname = 'uq_kg_node_region_type_code'
      AND indexdef LIKE '%is_draft%'
  ) THEN
    DROP INDEX IF EXISTS uq_kg_node_region_type_code;
    CREATE UNIQUE INDEX IF NOT EXISTS uq_kg_node_region_type_code
      ON kg_node(region, type, (attrs::json->>'code'))
      WHERE attrs::json->>'code' IS NOT NULL
        AND attrs::json->>'code' <> ''
        AND COALESCE(status, 'published') <> 'archived'
        AND NOT is_draft;
  END IF;
END $$;

-- 草稿清单与「这条记录有没有草稿」的判定（config.prefer_draft 的反连接）走这两个部分索引
CREATE INDEX IF NOT EXISTS idx_kg_node_draft ON kg_node(id) WHERE is_draft;
CREATE INDEX IF NOT EXISTS idx_kg_edge_draft_unit ON kg_edge(unit_id) WHERE is_draft;

-- **把「草稿行的 status 恒为 draft」这条不变量交给数据库**。
-- 它是整个方案唯一的静默失效点：草稿行的 status 一旦被写成 'published'，
-- 前台那 ~120 处 `status='published'` 查询当场命中它，不报错、不崩，只是草稿对外可见。
-- 代码评审看不出来（INSERT 的 status 是参数化传值），只有 CHECK 拦得住。
--
-- 已有违规行时**跳过**而不是让 ALTER 失败：这份 DDL 在每次进程启动的必经路径上，
-- 失败等于服务起不来。跳过会打一条 WARNING 到 PG 日志，用它排查。
DO $$
DECLARE bad int;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_kg_node_draft_status') THEN
    SELECT count(*) INTO bad FROM kg_node WHERE is_draft AND status <> 'draft';
    IF bad = 0 THEN
      ALTER TABLE kg_node ADD CONSTRAINT ck_kg_node_draft_status
        CHECK (NOT is_draft OR status = 'draft');
    ELSE
      RAISE WARNING 'kg_node 有 % 行草稿的 status 不是 draft，ck_kg_node_draft_status 未建立 —— 草稿可能已泄漏到前台', bad;
    END IF;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_kg_edge_draft_status') THEN
    SELECT count(*) INTO bad FROM kg_edge WHERE is_draft AND status <> 'draft';
    IF bad = 0 THEN
      ALTER TABLE kg_edge ADD CONSTRAINT ck_kg_edge_draft_status
        CHECK (NOT is_draft OR status = 'draft');
    ELSE
      RAISE WARNING 'kg_edge 有 % 行草稿的 status 不是 draft，ck_kg_edge_draft_status 未建立', bad;
    END IF;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS kg_proposal (
  id BIGSERIAL PRIMARY KEY,
  kind TEXT NOT NULL,
  payload JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  reason TEXT,
  created_by TEXT NOT NULL,
  created_by_name TEXT NOT NULL,
  reviewed_by TEXT,
  reviewed_by_name TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  reviewed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_kg_proposal_status ON kg_proposal(status);
"""


class _NulSafeStrDumper(StrDumper):
    """写库前剔除 NUL（0x00）。

    PostgreSQL 的 text 类型**物理上无法存 NUL**，psycopg 遇到就抛 DataError。
    而 NUL 能从三个方向进来，且都不是攻击才有的极端情况：
      1. 查询串 —— `?q=%00` 任何一个 str 参数都能触发，实测 49 个只读接口全数 500
      2. 请求体 —— 前端把二进制片段误当文本提交
      3. 简历解析 —— 部分 PDF 抽出的文本天然带 NUL
    NUL 在文本里没有任何语义，逐个接口挡既漏又散；统一在**存储层**剔除：
    它是"PG 存不了"这件事的正确高度，一处生效，覆盖上面三条全部路径。
    """

    def dump(self, obj: str, *args: Any, **kwargs: Any) -> Any:
        if "\x00" in obj:
            obj = obj.replace("\x00", "")
        return super().dump(obj, *args, **kwargs)


psycopg.adapters.register_dumper(str, _NulSafeStrDumper)


# ── 进程级连接池 ────────────────────────────────────────────────
#
# 原先 connect() 是裸 psycopg.connect()：每个 helper 一次 TCP + 认证握手
# （实测 20.07 ms/次，池化后 0.95 ms/次）。一次学员列表要开 3 条连接，
# 光握手就白扔 57 ms；并发上来先耗尽的是 PG 的 max_connections 而不是 CPU。
#
# 三个落地约束：
#   1. 池是**进程级**的 —— 多 worker 时 max_size × worker 数必须 < PG max_connections
#   2. 建池时 open=False，真正 open() 放在 FastAPI 启动事件里（见 api/main.py）。
#      gunicorn `--preload` 会先 import 再 fork，import 期就建好的连接会被多个
#      worker 共享同一批 socket，表现是随机的协议错乱。
#   3. 我们是同步栈（psycopg + 同步端点），用 ConnectionPool 而非 AsyncConnectionPool
_POOL: ConnectionPool | None = None
_POOL_LOCK = threading.Lock()


def _new_pool() -> ConnectionPool:
    return ConnectionPool(
        conninfo=DATABASE_URL,
        kwargs={"row_factory": dict_row},
        min_size=settings.DB_POOL_MIN_SIZE,
        max_size=settings.DB_POOL_MAX_SIZE,
        timeout=settings.DB_POOL_TIMEOUT,
        max_idle=settings.DB_POOL_MAX_IDLE,
        check=ConnectionPool.check_connection if settings.DB_POOL_CHECK else None,
        name="voced-kg",
        open=False,  # 见上方第 2 点，不要改成 True
    )


def get_pool() -> ConnectionPool:
    """取（必要时建并 open）进程池。

    懒开是为了脚本入口（migrate / 各类 CLI）—— 它们不走 FastAPI 启动事件。
    服务进程仍应在启动事件里显式 `open_pool()`，让建连失败在启动阶段就暴露。
    """
    global _POOL
    pool = _POOL
    if pool is None or pool.closed:
        with _POOL_LOCK:
            if _POOL is None or _POOL.closed:
                _POOL = _new_pool()
                _POOL.open(wait=False)
            return _POOL
    return pool


def open_pool(*, wait: bool = False, timeout: float | None = None) -> ConnectionPool:
    """FastAPI 启动事件调用：把池 open 在 fork 之后。"""
    pool = get_pool()
    if wait:
        pool.wait(timeout=timeout or settings.DB_POOL_TIMEOUT)
    return pool


def close_pool(timeout: float = 5.0) -> None:
    """进程退出时归还并关闭所有连接。"""
    global _POOL
    with _POOL_LOCK:
        pool, _POOL = _POOL, None
    if pool is not None and not pool.closed:
        try:
            pool.close(timeout=timeout)
        except Exception:  # noqa: BLE001 —— 退出路径不因清理失败而报错
            pass


def pool_stats() -> dict[str, Any]:
    """运维自检用；池未建时返回空 dict（不顺手把池建出来）。"""
    pool = _POOL
    if pool is None:
        return {}
    try:
        return dict(pool.get_stats())
    except Exception:  # noqa: BLE001
        return {}


class PooledConnection:
    """池化连接的薄包装：`with` 退出时**归还**而不是关闭。

    为什么不能直接把 `pool.getconn()` 的连接交出去：仓库里 100 多处写的是
    `with connect() as conn:`，而 psycopg 的 `Connection.__exit__` 语义是
    「提交/回滚 + close()」。真 close 掉的连接永远回不了池，一个请求就能把池抽干。
    这里只改 `__exit__` / `close()` 两处语义（改成 putconn），其余属性全部透传，
    调用方的写法和事务语义都不变。
    """

    __slots__ = ("_conn", "_pool", "_released")

    def __init__(self, conn: psycopg.Connection, pool: ConnectionPool) -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_pool", pool)
        object.__setattr__(self, "_released", False)

    # —— 属性透传 ——
    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in PooledConnection.__slots__:
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_conn"), name, value)

    def __repr__(self) -> str:
        return f"<PooledConnection {object.__getattribute__(self, '_conn')!r}>"

    # —— 上下文管理：与 psycopg.Connection 同语义，只是结尾归还而非关闭 ——
    def __enter__(self) -> "PooledConnection":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self.close()
        return False

    def close(self) -> None:
        """归还给池（幂等）。真正的 socket 关闭由池决定。"""
        if self._released:
            return
        object.__setattr__(self, "_released", True)
        conn = self._conn
        try:
            # 归还前把没结束的事务收掉。有些只读 helper 是 `c = connect()` …
            # `finally: c.close()` 的写法（migrate.stats 就是），从不 commit；
            # 裸连接时无所谓，进了池就会带着 INTRANS 回去，
            # psycopg_pool 只好替我们回滚并打一条 warning。自己收干净。
            if conn.pgconn.transaction_status != TransactionStatus.IDLE:
                conn.rollback()
        except Exception:  # noqa: BLE001 —— 连接已坏时交给池去丢弃
            pass
        self._pool.putconn(conn)


def connect() -> PooledConnection:
    """从池 checkout 一条连接。**签名与语义与原来一致**：

        with connect() as conn:   # 正常退出提交，异常回滚，最后归还
            conn.execute(...)
    """
    pool = get_pool()
    conn = pool.getconn(timeout=settings.DB_POOL_TIMEOUT)
    return PooledConnection(conn, pool)


@contextmanager
def use_conn(conn: Any | None = None) -> Iterator[Any]:
    """「有现成连接就用它，没有才自己 checkout」。

    让 pg_store 的公开函数能加一个可选 `conn=None` 参数而不动调用方：
    路由层一个 `with session() as conn:` 往下传，一次请求就落在**一条连接、
    一个事务**里；不传的老调用点照旧各自 checkout，行为完全不变。

    注意：传入 conn 时**不提交也不关闭**——事务边界属于开这条连接的人。
    """
    if conn is not None:
        yield conn
        return
    with connect() as c:
        yield c


@contextmanager
def session() -> Iterator[PooledConnection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_SCHEMA_DONE = False


def ensure_schema(conn: Any | None = None, *, force: bool = False) -> None:
    """跑一遍幂等 DDL。**同一进程只真正执行一次**。

    SCHEMA_SQL 里不止是 `CREATE TABLE IF NOT EXISTS`：还有两条 `created_at` 回填
    `UPDATE` 和一个表达式唯一索引。启动时 `ensure_schema` / `ensure_biz_schema` /
    `ensure_review_schema` 都会走到这里，不去重就是把回填 UPDATE 跑三遍。

    显式传 `conn`（migrate 脚本）或 `force=True` 时无条件执行。
    """
    global _SCHEMA_DONE
    own = conn is None
    if own and _SCHEMA_DONE and not force:
        return
    c = conn or connect()
    try:
        c.execute(SCHEMA_SQL)
        if own:
            c.commit()
            _SCHEMA_DONE = True
    finally:
        if own:
            c.close()


def verify_connectivity() -> dict[str, Any]:
    """健康检查探针 —— 只做 `SELECT 1`。

    原先对 kg_node / kg_edge 各来一次 `COUNT(*)`：探活定时器每隔几秒就把全图扫一遍，
    图越大越慢，还会和写入抢 IO。健康检查要回答的只有「连得上吗」。
    `nodes` / `edges` 字段保留（契约里是 `int | None`），值固定为 None ——
    真要计数走 `/v1/stats`。
    """
    try:
        with connect() as conn:
            # 一次往返拿到「活着」+ 版本号；version() 是常量函数，不碰任何表
            row = conn.execute("SELECT 1 AS ok, version() AS version").fetchone()
            return {
                "ok": True,
                "engine": "postgresql",
                "version": ((row or {}).get("version") or "")[:80],
                "nodes": None,
                "edges": None,
            }
    except Exception as ex:
        return {"ok": False, "engine": "postgresql", "error": str(ex)}
