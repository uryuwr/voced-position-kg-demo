"""管理台草稿态（`docs/方案-管理台草稿态与发布.md`）的**不连库**契约。

连库能验的都在 `tests/db/`（DDL 实际状态、泄漏扫描、发布事务）。这里只留三类
「不跑库也必须成立」的东西，它们的共同点是**出错时不报错**：

1. **§0.2 的前提**：草稿之所以不用改那 ~120 处前台查询，全靠「`draft` 不在前台
   状态词表里」。哪天有人往 `PUBLIC_STATUSES` 里加一个值，草稿当天泄漏，
   而所有连库用例都可能因为那一刻库里没有草稿行而全绿。
2. **片段函数的语义**：`prefer_draft` 缺省别名写成裸 `id` 的话，子查询里的
   `kg_node __pd` 也有 `id` 列、内层作用域优先，条件恒真、去重**静默失效**。
   这种错跑起来不报错，只是列表里偶尔多一行。
3. **§4 的写入点分类**：新增一处裸 SQL 写 `kg_node` / `kg_edge` 时，
   要么它该草稿化而漏了（改动绕过草稿直接对外），要么不该而做了（派生数据永不更新）。
   分类只能靠一份白名单闸门守住，靠人记必漏。

DDL 相关的断言这里只做「写了没有」，「库里真的变成这样了没有」在
`tests/db/test_draft_schema.py` —— 读 SQL 文本证明不了迁移在老库上跑成功了。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from backend.kg.pg_store.client import SCHEMA_SQL
from backend.kg.pg_store.config import (
    ADMIN_STATUSES,
    ARCHIVED_STATUS,
    DRAFT_STATUS,
    PUBLIC_STATUSES,
    edge_published,
    node_published,
    online_only,
    online_only_edge,
    prefer_draft,
    prefer_draft_edge,
)

PG_STORE = Path(__file__).resolve().parents[2] / "backend" / "kg" / "pg_store"


# ── §0.2 的前提：前台状态口径不能被放宽 ────────────────────────


class Test前台状态口径:
    def test_草稿不在前台状态词表里(self):
        """整个方案的地基：草稿行 status='draft' → 前台的 `= 'published'` 自动排除它。

        往 `PUBLIC_STATUSES` 里加 'draft' 会让 ~120 处查询同时开始返回草稿，
        且**没有任何一处会报错**。
        """
        assert PUBLIC_STATUSES == ("published",)
        assert DRAFT_STATUS not in PUBLIC_STATUSES

    def test_管理台看得见草稿(self):
        """反面：管理台口径必须含 draft，否则运营看不到自己刚改的东西。"""
        assert DRAFT_STATUS in ADMIN_STATUSES
        assert ARCHIVED_STATUS not in ADMIN_STATUSES

    def test_前台可见性片段仍然只认published(self):
        """草稿态没有、也不该放宽这两个片段 —— §0.1：前台的规则只有「读已发布版本」。"""
        assert node_published("n") == "COALESCE(n.status, 'published') = 'published'"
        assert edge_published("e") == "COALESCE(e.status, 'published') = 'published'"


# ── §6.2 的去重片段 ──────────────────────────────────────────


class TestPreferDraft片段:
    def test_有草稿取草稿否则取线上行(self):
        """语义：`是草稿行` OR `不存在同 id 的草稿行`。两个分支缺一不可 ——
        少了前者草稿永远不显示，少了后者列表里同一记录出现两行。"""
        f = prefer_draft("n")
        assert "n.is_draft OR NOT EXISTS" in f.replace("(", "").replace(")", "")
        assert "is_draft" in f

    def test_缺省别名必须表名限定(self):
        """写成裸 `id = __pd.id` 时子查询内层的 `id` 优先，条件恒真、去重静默失效。"""
        f = prefer_draft()
        assert "kg_node.id" in f, f
        assert not re.search(r"__pd\.id\s*=\s*id\b", f), f"别名丢了，去重会恒真：{f}"

    def test_边也有一份(self):
        f = prefer_draft_edge("e")
        assert "e.is_draft" in f and "kg_edge" in f
        assert "__pd." not in f.replace("__pde.", ""), "边的子查询别名要和节点那份区分开"

    def test_它不是前台过滤器(self):
        """`prefer_draft` 只做去重，**不带任何 status 条件**。

        拿它当前台条件用等于把草稿放进前台：它对草稿行的判定是 true。
        这条断言存在的意义是防止有人把两件事合成一个片段。
        """
        assert "published" not in prefer_draft()


class TestOnlineOnly片段:
    def test_只读线上行(self):
        assert online_only("n") == "NOT n.is_draft"
        assert online_only() == "NOT kg_node.is_draft"

    def test_边同理(self):
        assert online_only_edge("e") == "NOT e.is_draft"
        assert online_only_edge() == online_only_edge("e"), "边片段的缺省别名是 e"


# ── §2 的 DDL：写了没有 ──────────────────────────────────────


class TestDDL迁移写进了建表脚本:
    @pytest.mark.parametrize(
        "col", ["is_draft", "target_status", "base_version"]
    )
    def test_节点控制列(self, col):
        assert re.search(rf"ALTER TABLE kg_node ADD COLUMN IF NOT EXISTS {col}\b", SCHEMA_SQL)

    @pytest.mark.parametrize("col", ["is_draft", "target_status", "unit_id"])
    def test_边控制列(self, col):
        assert re.search(rf"ALTER TABLE kg_edge ADD COLUMN IF NOT EXISTS {col}\b", SCHEMA_SQL)

    def test_is_draft有非空默认值(self):
        """默认成 true 的话，那些不显式写 is_draft 的写入点（离线灌库、派生元数据）
        产出的全是草稿行，前台会整片消失。"""
        for tbl in ("kg_node", "kg_edge"):
            m = re.search(
                rf"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS is_draft ([^;]+);", SCHEMA_SQL
            )
            assert m, tbl
            assert "NOT NULL" in m.group(1).upper()
            assert "DEFAULT FALSE" in m.group(1).upper()

    @pytest.mark.parametrize("fk", ["kg_edge_src_id_fkey", "kg_edge_dst_id_fkey"])
    def test_两个外键被显式删掉(self, fk):
        """§1.2：外键引用不了部分唯一索引，换主键后只能删。

        `CREATE TABLE` 里还留着 `REFERENCES kg_node(id)`（新库先建后删，无所谓），
        所以这里断言的是**有没有那两条 DROP**。
        """
        assert f"DROP CONSTRAINT IF EXISTS {fk}" in SCHEMA_SQL

    def test_换主键包在幂等守卫里(self):
        """`ADD PRIMARY KEY` 天生不幂等，而这份 DDL 每次进程启动都整段跑。

        漏守卫的表现是**第二次启动就起不来**（multiple primary keys are not allowed）
        —— 开发机上第一次总是成功的，很容易漏到线上。
        """
        naked = [
            s for s in _statements_outside_do_blocks(SCHEMA_SQL)
            if "ADD PRIMARY KEY" in s.upper()
        ]
        assert not naked, f"这些 ADD PRIMARY KEY 不在 DO 守卫里：{naked}"
        for tbl in ("kg_node", "kg_edge"):
            assert re.search(
                rf"ADD PRIMARY KEY \(id, is_draft\)", SCHEMA_SQL
            ), tbl

    def test_业务编码唯一索引排除草稿(self):
        """§2 ④：不排除的话「把编码从 A 改成 B」时草稿行会撞自己的线上行。"""
        m = re.search(
            r"CREATE UNIQUE INDEX (IF NOT EXISTS )?uq_kg_node_region_type_code(.*?);",
            SCHEMA_SQL, re.S,
        )
        assert m, "唯一索引不见了"
        assert "NOT is_draft" in m.group(2), m.group(2)

    def test_老库上的旧索引会被换掉(self):
        """老库里已经有一个**不含 is_draft 条件**的同名索引。

        只写 `CREATE UNIQUE INDEX IF NOT EXISTS` 会直接跳过、旧定义留在库里，
        于是改编码照旧报唯一冲突 —— 而 DDL 一句错都不报。所以必须有 DROP 或条件判断。
        """
        assert (
            "DROP INDEX IF EXISTS uq_kg_node_region_type_code" in SCHEMA_SQL
            or "indexdef LIKE '%is_draft%'" in SCHEMA_SQL
        )

    @pytest.mark.parametrize("idx", ["idx_kg_node_draft", "idx_kg_edge_draft_unit"])
    def test_草稿部分索引(self, idx):
        assert f"CREATE INDEX IF NOT EXISTS {idx}" in SCHEMA_SQL


class TestOnConflict冲突目标:
    """§2.1：主键变成 `(id, is_draft)` 后，冲突目标写 `(id)` 的 upsert 会当场抛
    `there is no unique or exclusion constraint matching`。"""

    def test_没有残留的裸id冲突目标(self):
        bad = []
        for py in sorted(PG_STORE.glob("*.py")):
            src = py.read_text(encoding="utf-8")
            for sql in _sql_literals(src):
                if not re.search(r"INSERT INTO kg_(node|edge)", sql):
                    continue
                for m in re.finditer(r"ON CONFLICT\s*\(([^)]*)\)", sql):
                    cols = {c.strip() for c in m.group(1).split(",")}
                    if cols == {"id"}:
                        bad.append(f"{py.name}: ON CONFLICT ({m.group(1)})")
        assert not bad, "冲突目标没跟着主键改：" + "; ".join(bad)


# ── §0.2 的静态守卫：能静态看出来的那几处 ─────────────────────


class Test草稿行的status不能写别的:
    """「发布后应变成什么」只能存在 `target_status` 里。

    写进 `status` 的那一刻草稿就被前台的 `status='published'` 命中（§0.2）。
    参数化传值的写入点静态查不了（由 `tests/db` 的运行时不变量兜），
    但 SQL 字面量里把 is_draft 置真的那几处能查，而且它们正是最容易写错的地方 ——
    「复制线上行到草稿行」的那两条。
    """

    def test_把is_draft置真的SQL必须同时把status钉成draft(self):
        bad = []
        for py in sorted(PG_STORE.glob("*.py")):
            for sql in _sql_literals(py.read_text(encoding="utf-8")):
                if not re.search(r"INSERT INTO kg_(node|edge)", sql):
                    continue
                sets_draft = re.search(r"'is_draft'\s*,\s*true|is_draft\s*=\s*true", sql)
                if not sets_draft:
                    continue
                if "'draft'" not in sql:
                    bad.append(f"{py.name}: {sql.strip()[:120]}")
        assert not bad, "这些 SQL 造草稿行却没把 status 钉成 'draft'：" + "; ".join(bad)

    def test_发布意图词表里没有draft(self):
        """`target_status` 的取值是「发布后线上行变成什么」，`draft` 不是一个发布结果。"""
        from backend.kg.pg_store.write import _TARGET_STATUSES

        assert DRAFT_STATUS not in _TARGET_STATUSES
        # 2026-08-19 需求收窄：停用/启用/删除改成立即生效，`deleted` 这个意图已撤销。
        # 现在 target_status 只剩两个来源：新建记录发布时该落成什么状态、边的墓碑。
        assert set(_TARGET_STATUSES) == {"published", "disabled", "archived"}

    @pytest.mark.parametrize(
        "req,want",
        [("published", "published"), ("disabled", "disabled"),
         ("archived", "archived"), ("draft", None), (None, None), ("", None)],
    )
    def test_请求里的status被翻成target_status(self, req, want):
        """运营点「保存并发布」→ 意图落在 `target_status`，草稿行的 status 不动。"""
        from backend.kg.pg_store.write import _target_status_of

        assert _target_status_of(req) == want


# ── §4 写入点分类 ────────────────────────────────────────────

# 方案 §4 的四类白名单。**新增模块必须先在这里分类**，否则这条闸门会红。
WRITE_POINT_CLASSES = {
    # 运营编辑内容 → 写草稿行
    "write.py": "运营编辑",
    "skill_composition.py": "运营编辑",
    "skill_write.py": "运营编辑",
    # 发布 / 审批落地 → 写线上行（它们就是发布侧）
    "review.py": "发布侧",
    "publish_rules.py": "发布侧",
    "draft_publish.py": "发布侧",
    # 派生展示元数据 → 写线上行，但必须带 NOT is_draft
    "node_layout_meta.py": "派生元数据",
    # 离线灌库 / DDL → 写线上行，显式 is_draft=false
    "migrate.py": "离线/DDL",
    "client.py": "离线/DDL",
}


class Test写入点白名单:
    def test_没有未分类的裸SQL写入点(self):
        """§4：全仓 31 处直接写 `kg_node`/`kg_edge` 的 SQL，分布 9 个模块。

        分错会坏两种事：该草稿化的漏了 → 改动绕过草稿直接对外；
        不该草稿化的做了 → 派生数据永不更新。所以新增一处写入点必须先分类。
        """
        found = set()
        # 只扫 backend/ —— 交付物就是这一个目录（CLAUDE.md 第一原则）。
        # scripts/ 与 crawlers/ 也在直连改库，那属于「直连绕得过应用层」（§10.4），
        # 由闸门脚本兜，不是这条闸门的范围。
        root = PG_STORE.parents[1]
        assert root.name == "backend", root
        for py in sorted(root.rglob("*.py")):
            src = py.read_text(encoding="utf-8")
            if re.search(
                r"(INSERT INTO|UPDATE|DELETE FROM)\s+kg_(node|edge)", src
            ):
                found.add(py.name)
        unknown = sorted(found - set(WRITE_POINT_CLASSES))
        assert not unknown, (
            f"这些模块新增了直接写 kg_node/kg_edge 的 SQL，但没在 §4 里分类：{unknown}"
        )

    def test_派生元数据重算必须排除草稿行(self):
        """§10.3 点名的两种错法之一：忘了 `NOT is_draft`，
        排序/子级计数会把草稿行也算进去 —— 前台的 `sort_order` 与 `child_count`
        于是随「有没有人在编辑」而变，而这两列是前台读的。"""
        src = (PG_STORE / "node_layout_meta.py").read_text(encoding="utf-8")
        bad = []
        for stmt in re.split(r";\s*\n|\"\"\"|'''", src):
            if not re.search(r"UPDATE\s+kg_node", stmt):
                continue
            if "is_draft" not in stmt:
                bad.append(" ".join(stmt.split())[:120])
        assert not bad, "这些布局重算语句没排除草稿行：" + "; ".join(bad)

    def test_离线灌库显式写线上行(self):
        """§10.5：`migrate` 不是运营编辑，必须显式 `is_draft=false`；
        靠列默认值的话，upsert 会撞到草稿行（冲突目标一带上 is_draft 就更明显）。"""
        src = (PG_STORE / "migrate.py").read_text(encoding="utf-8")
        for tbl in ("kg_node", "kg_edge"):
            m = re.search(rf"INSERT INTO {tbl}[^;]*?ON CONFLICT", src, re.S)
            assert m, f"{tbl} 的灌库 upsert 不见了"
            assert "is_draft" in m.group(0), f"{tbl} 灌库没显式写 is_draft"


# ── 小工具 ───────────────────────────────────────────────────


def _sql_literals(src: str) -> list[str]:
    """取出源码里的字符串常量（含 f-string 的字面片段）。

    用 `ast` 而不是正则扫全文：正则会把注释、docstring 里的示例 SQL 也算进来，
    于是「文档里写了一句反例」都能让闸门变红。
    """
    out: list[str] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:  # pragma: no cover
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            out.append(
                "".join(
                    v.value for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
            )
    return out


def _statements_outside_do_blocks(sql: str) -> list[str]:
    """把 `DO $$ … END $$;` 整块抠掉后按 `;` 切句。

    朴素的 `split(";")` 会把 DO 块切碎，于是块内那些「被守卫保护着的」语句
    看起来像裸语句 —— 既有的 `test_建表语句是幂等的` 就是这么误报的。
    """
    stripped = re.sub(r"DO \$\$.*?END \$\$;", "", sql, flags=re.S)
    stripped = re.sub(r"--[^\n]*", "", stripped)   # 注释里也写着 ADD PRIMARY KEY
    return [s.strip() for s in stripped.split(";") if s.strip()]
