"""草稿态隔离的公共探针：造草稿受试对象 + **按真实路由表**扫前台响应。

为什么不手写一份「前台接口清单」
--------------------------------
`docs/方案-管理台草稿态与发布.md` §10.2 说得很直白：草稿行的 `status` 一旦不是
`'draft'`，前台查询立刻命中它，而这是**整个方案唯一的静默失效点**——不报错、不崩，
只是草稿数据出现在学员端。这类泄漏面必须用路由表遍历来兜：手写清单漏掉的那一个
接口，就是泄漏点本身。所以这里从 `app.openapi()` 现摘 GET 路由，按 **tag** 分前台
（`前台 · …`）与管理台（`管理台 · …`），新增接口自动进扫描范围。

两种草稿形态（`mode`）
----------------------
- `row`    —— §0/§1 的目标形态：同一记录两行，草稿行 `is_draft=true, status='draft'`。
             需要 §2 的 DDL 已落地（`is_draft` 列 + 复合主键），否则造不出来。
- `status` —— 迁移前的单行形态：`status='draft'` 的线上行（BR-07）。今天就能造，
             也是**迁移后必须继续成立**的回归项：库里还留着 credential 那批
             `status='draft'` 的线上行（见 §5 的注），它们同样一行都不能进前台。

两种形态用同一套断言，`install_draft_fixture()` 挑得到哪种就跑哪种。

清理
----
`remove_draft_fixture()` 按 id 前缀删，**幂等**，任何时候都能安全调用。所有受试对象
的 id 都以 `ZZ:draftprobe:` 开头、名字都含哨兵串，不与真实数据重名，
所以第二次跑不会撞主键（本项目的连库测试栽过这个）。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── 哨兵：泄漏检测靠「响应里出不出现这些串」 ──────────────────
SENTINEL = "ZZ草稿哨兵"
ID_PREFIX = "ZZ:draftprobe:"
DRAFT_SKILL_KEY = f"{SENTINEL}技能"
DRAFT_CODE_PREFIX = "ZZDRAFTPROBE"

# 前台/管理台的划分口径：路由 tag 的前缀。改 tag 命名的话这里跟着改。
FRONT_TAG_PREFIX = "前台"
ADMIN_TAG_PREFIX = "管理台"

# 扫描时统一压小的分页/规模参数——不压的话 `/v1/capability`、`/v1/graph/*`
# 会把整张图拉出来，一轮扫描要几分钟，快照对比更是不可用。
SMALL = {
    "limit": "5",
    "page_size": "5",
    "page": "1",
    "max_nodes": "20",
    "depth": "1",
    "limit_occupations": "3",
    "limit_skills_per_occ": "3",
    "limit_majors": "3",
    "limit_occupations_per_major": "3",
    "limit_per_skill": "2",
    "shared_skill_min_occ": "2",
}


class DraftUnsupported(RuntimeError):
    """库里造不出这种草稿形态（通常是 §2 的 DDL 还没落地）。"""


# ── 进程内 app 客户端 ────────────────────────────────────────


def make_client(uid: str = "9201", uname: str = "draft-probe") -> Any:
    """进程内 TestClient（带鉴权旁路）。

    不起 uvicorn 子进程：端口冲突、启动等待、日志散在别处都是噪声，而这里要测的
    是**路由表 + 读路径**，TestClient 走的是同一条 ASGI 栈（中间件也跑），够用且确定。
    旁路只改内存值（照 `tests/_e2e_server.py` 的做法），不碰 `.env`。
    """
    import backend.settings as settings

    settings.AUTH_BYPASS = True
    settings.AUTH_DEBUG = True
    import backend.api.auth as auth

    auth.AUTH_BYPASS = True
    auth.AUTH_DEBUG = True

    from fastapi.testclient import TestClient

    from backend.api.main import app

    # raise_server_exceptions=False：路由抛异常时**照常返回 500 响应**而不是把异常
    # 甩到测试进程里。扫描要的是「有没有 5xx / 有没有泄漏」的全量结论，
    # 一个接口炸了不该让整轮扫描中断（实现在途时这非常常见）。
    client = TestClient(
        app,
        headers={"X-Test-Uid": uid, "X-Test-Uname": uname},
        raise_server_exceptions=False,
    )
    return client


def get_app() -> Any:
    from backend.api.main import app

    return app


# ── 库能力探测（DDL 落到什么程度）────────────────────────────


def db_capabilities() -> dict[str, Any]:
    """当前库对草稿态的支持程度。**测试据此判断「实现没做」还是「实现做错」。**"""
    from backend.kg.pg_store.client import connect

    out: dict[str, Any] = {}
    with connect() as c:
        cols = {
            (r["table_name"], r["column_name"])
            for r in c.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_name IN ('kg_node','kg_edge')"
            ).fetchall()
        }
        out["node_columns"] = {t for (tbl, t) in cols if tbl == "kg_node"}
        out["edge_columns"] = {t for (tbl, t) in cols if tbl == "kg_edge"}
        out["has_is_draft"] = ("kg_node", "is_draft") in cols and ("kg_edge", "is_draft") in cols

        out["pk"] = {}
        for tbl in ("kg_node", "kg_edge"):
            rows = c.execute(
                """
                SELECT a.attname AS col
                FROM pg_index i
                JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = %s::regclass AND i.indisprimary
                ORDER BY a.attnum
                """,
                (tbl,),
            ).fetchall()
            out["pk"][tbl] = [r["col"] for r in rows]
        out["pk_has_is_draft"] = all(
            "is_draft" in out["pk"].get(t, []) for t in ("kg_node", "kg_edge")
        )

        out["foreign_keys"] = [
            r["conname"]
            for r in c.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid='kg_edge'::regclass AND contype='f'"
            ).fetchall()
        ]
        row = c.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename='kg_node' AND indexname='uq_kg_node_region_type_code'"
        ).fetchone()
        out["code_unique_indexdef"] = (row or {}).get("indexdef") or ""
    return out


# ── 真实受试对象（不写死 id，见 tests/_stable_data.py 的理由）──


def pick_real_ids() -> dict[str, str]:
    """按不变量现挑真实的已发布受试对象。挑不到的键直接缺席，调用方要能容忍。

    写死 id 会在数据被重灌后假红；这里的每一项都由「有几条边、边指向什么类型」
    这类不变量筛出来。
    """
    from backend.kg.pg_store.client import connect

    out: dict[str, str] = {}
    with connect() as c:
        occ = c.execute(
            """
            SELECT o.id, o.name, COUNT(*) AS n
            FROM kg_edge e
            JOIN kg_node o ON o.id = e.src_id AND o.type = 'occupation'
                 AND COALESCE(o.status,'published') = 'published'
            JOIN kg_node s ON s.id = e.dst_id AND s.type = 'skill_level'
                 AND COALESCE(s.status,'published') = 'published'
            WHERE e.rel_type = 'requires' AND COALESCE(e.status,'published') = 'published'
              AND o.name NOT LIKE 'ZZ%%'
            GROUP BY 1, 2
            HAVING COUNT(*) BETWEEN 3 AND 8
            ORDER BY COUNT(*), o.id
            LIMIT 2
            """
        ).fetchall()
        if occ:
            out["occupation_id"] = occ[0]["id"]
            out["occupation_name"] = occ[0]["name"]
        if len(occ) > 1:
            out["spare_occupation_id"] = occ[1]["id"]
            out["spare_occupation_name"] = occ[1]["name"]

        mj = c.execute(
            """
            SELECT m.id, m.name FROM kg_edge e
            JOIN kg_node m ON m.id = e.src_id AND m.type='major'
                 AND COALESCE(m.status,'published')='published'
            WHERE e.rel_type='prepares_for' AND COALESCE(e.status,'published')='published'
              AND m.name NOT LIKE 'ZZ%%'
            ORDER BY m.id LIMIT 1
            """
        ).fetchone()
        if mj:
            out["major_id"], out["major_name"] = mj["id"], mj["name"]

        ind = c.execute(
            "SELECT id, name FROM kg_node WHERE type='industry' "
            "AND COALESCE(status,'published')='published' AND name NOT LIKE 'ZZ%%' "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        if ind:
            out["industry_id"], out["industry_name"] = ind["id"], ind["name"]

        sk = c.execute(
            """
            SELECT s.id, s.name, s.attrs::json->>'skill_key' AS sk
            FROM kg_edge e
            JOIN kg_node s ON s.id = e.src_id AND s.type='skill_level'
                 AND COALESCE(s.status,'published')='published'
            WHERE e.rel_type='taught_by' AND COALESCE(e.status,'published')='published'
            ORDER BY s.id LIMIT 1
            """
        ).fetchone()
        if sk:
            out["skill_id"] = sk["id"]
            out["skill_key"] = sk["sk"] or sk["name"]

        co = c.execute(
            "SELECT id, name FROM kg_node WHERE type='course' "
            "AND COALESCE(status,'published')='published' ORDER BY id LIMIT 1"
        ).fetchone()
        if co:
            out["course_id"] = co["id"]
    return out


# ── 草稿受试对象 ─────────────────────────────────────────────

_NODE_SPECS: list[tuple[str, str, str, dict[str, Any]]] = [
    # kind, type, name, attrs
    ("occupation", "occupation", f"{SENTINEL}岗位", {"code": f"{DRAFT_CODE_PREFIX}OCC"}),
    ("major", "major", f"{SENTINEL}专业", {"code": f"{DRAFT_CODE_PREFIX}MAJ"}),
    ("industry", "industry", f"{SENTINEL}行业", {"code": f"{DRAFT_CODE_PREFIX}IND"}),
    (
        "skill_level",
        "skill_level",
        f"{DRAFT_SKILL_KEY} · L3",
        {"skill_key": DRAFT_SKILL_KEY, "level": 3},
    ),
    ("course", "course", f"{SENTINEL}课程", {"playable": True, "url": "http://zz.invalid"}),
]

_NODE_BASE = {
    "region": "CN",
    "source_system": "MANUAL",
    "source_url": "manual://draft-probe",
    "license": "internal",
    "fetched_at": "2026-08-18T00:00:00Z",
    "confidence": "manual_seed",
}


@dataclass
class DraftFixture:
    """一组草稿受试对象。`mode` 说明它是哪种草稿形态。"""

    mode: str
    node_ids: dict[str, str] = field(default_factory=dict)
    edge_ids: list[str] = field(default_factory=list)
    shadow_id: str | None = None
    shadow_orig_name: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> list[str]:
        """出现在响应里就算泄漏的串。"""
        t = [SENTINEL, ID_PREFIX, DRAFT_CODE_PREFIX, "draftprobe"]
        return t


def _insert_node(conn, nid: str, ntype: str, name: str, attrs: dict, *, is_draft: bool) -> None:
    cols = dict(_NODE_BASE)
    cols.update(
        {
            "id": nid,
            "type": ntype,
            "name": name,
            "attrs": json.dumps(attrs, ensure_ascii=False),
            "source_id": nid,
            "status": "draft",  # §0.2 的不变量：草稿行 status 恒为 draft
        }
    )
    if is_draft:
        cols["is_draft"] = True
        cols["target_status"] = "published"
    keys = list(cols)
    conn.execute(
        f"INSERT INTO kg_node ({', '.join(keys)}) "
        f"VALUES ({', '.join('%(' + k + ')s' for k in keys)})",
        cols,
    )


def _insert_edge(
    conn,
    eid: str,
    src: str,
    dst: str,
    rel: str,
    *,
    is_draft: bool,
    weight: float | None = 0.2,
    unit_id: str | None = None,
) -> None:
    cols = {
        "id": eid,
        "src_id": src,
        "dst_id": dst,
        "rel_type": rel,
        "region": "CN",
        "weight": weight,
        "attrs": "{}",
        "source_system": "MANUAL",
        "source_id": eid,
        "source_url": "manual://draft-probe",
        "license": "internal",
        "fetched_at": "2026-08-18T00:00:00Z",
        "confidence": "manual_seed",
        "status": "draft",
    }
    if is_draft:
        cols["is_draft"] = True
        cols["unit_id"] = unit_id or src   # §3：发布单元 = src 节点
    keys = list(cols)
    conn.execute(
        f"INSERT INTO kg_edge ({', '.join(keys)}) "
        f"VALUES ({', '.join('%(' + k + ')s' for k in keys)})",
        cols,
    )


def remove_draft_fixture() -> None:
    """幂等清理。按 id 前缀删草稿受试对象，并抹掉所有影子草稿行。"""
    from backend.kg.pg_store.client import connect

    caps = db_capabilities()
    with connect() as c:
        c.execute(f"DELETE FROM kg_edge WHERE id LIKE '{ID_PREFIX}%'")
        c.execute(
            f"DELETE FROM kg_edge WHERE src_id LIKE '{ID_PREFIX}%' OR dst_id LIKE '{ID_PREFIX}%'"
        )
        if caps["has_is_draft"]:
            # 影子草稿行：id 与线上行相同，只能靠 is_draft 区分
            c.execute(f"DELETE FROM kg_edge WHERE is_draft AND unit_id LIKE '{ID_PREFIX}%'")
            c.execute(
                "DELETE FROM kg_node WHERE is_draft AND name LIKE %s", (f"%{SENTINEL}%",)
            )
            c.execute(
                "DELETE FROM kg_edge WHERE is_draft AND id LIKE %s", (f"{ID_PREFIX}%",)
            )
        c.execute(f"DELETE FROM kg_node WHERE id LIKE '{ID_PREFIX}%'")
        c.commit()


def install_draft_fixture(mode: str = "auto", *, shadow_of: str | None = None) -> DraftFixture:
    """造一组草稿受试对象。

    `mode='auto'` 时优先造目标形态（`row`），DDL 没落地才退回 `status`。
    `shadow_of` 给一个**已发布节点 id**，会为它多造一个「改了名的草稿行」——
    这是最锋利的探针：任何一处读路径忘了过滤 `is_draft` / `status`，
    前台就会显示哨兵名字，而不是像新建那样只是「多出一条记录」。
    """
    from backend.kg.pg_store.client import connect

    caps = db_capabilities()
    if mode == "auto":
        mode = "row" if caps["has_is_draft"] else "status"
    if mode == "row" and not caps["has_is_draft"]:
        raise DraftUnsupported(
            "kg_node/kg_edge 上没有 is_draft 列 —— 方案 §2 的 DDL 尚未落地，"
            "造不出「同表两行」的草稿行"
        )

    remove_draft_fixture()
    fx = DraftFixture(mode=mode)
    is_draft = mode == "row"
    real = pick_real_ids()
    with connect() as c:
        for kind, ntype, name, attrs in _NODE_SPECS:
            nid = f"{ID_PREFIX}{kind}:1"
            _insert_node(c, nid, ntype, name, attrs, is_draft=is_draft)
            fx.node_ids[kind] = nid

        occ, sk = fx.node_ids["occupation"], fx.node_ids["skill_level"]
        mj, co = fx.node_ids["major"], fx.node_ids["course"]
        ind = fx.node_ids["industry"]
        edges: list[tuple[str, str, str, str]] = [
            # 草稿节点内部：新建岗位的技能构成
            (f"{ID_PREFIX}edge:occ-requires-skill", occ, sk, "requires"),
            (f"{ID_PREFIX}edge:major-prepares-occ", mj, occ, "prepares_for"),
            (f"{ID_PREFIX}edge:major-belongs-industry", mj, ind, "belongs_to"),
            (f"{ID_PREFIX}edge:skill-taught-course", sk, co, "taught_by"),
        ]
        # 一端是**已发布**真实节点的草稿边：泄漏时前台会多出一项技能/一个岗位，
        # 这是「边模型」下最容易漏过滤的形状（两端节点都正常，只有边是草稿）
        if real.get("occupation_id"):
            edges.append(
                (f"{ID_PREFIX}edge:realocc-requires-draftskill",
                 real["occupation_id"], sk, "requires")
            )
        if real.get("major_id"):
            edges.append(
                (f"{ID_PREFIX}edge:realmajor-prepares-draftocc",
                 real["major_id"], occ, "prepares_for")
            )
        if real.get("industry_id"):
            edges.append(
                (f"{ID_PREFIX}edge:draftmajor-belongs-realindustry",
                 mj, real["industry_id"], "belongs_to")
            )
        for eid, s, d, rel in edges:
            # §3：约定 unit_id = src_id（运营是「进入 A 的编辑页改它的关系」）
            _insert_edge(c, eid, s, d, rel, is_draft=is_draft, unit_id=s)
            fx.edge_ids.append(eid)

        if shadow_of and is_draft:
            row = c.execute(
                "SELECT * FROM kg_node WHERE id=%s AND NOT is_draft", (shadow_of,)
            ).fetchone()
            if row:
                d = dict(row)
                fx.shadow_orig_name = d["name"]
                d["name"] = f"{SENTINEL}改名 · {d['name']}"
                d["is_draft"] = True
                d["status"] = "draft"
                d["target_status"] = None
                d["base_version"] = d.get("version")
                keys = [k for k in d if k in caps["node_columns"]]
                c.execute(
                    f"INSERT INTO kg_node ({', '.join(keys)}) "
                    f"VALUES ({', '.join('%(' + k + ')s' for k in keys)})",
                    {k: d[k] for k in keys},
                )
                fx.shadow_id = shadow_of
            else:
                fx.notes.append(f"影子草稿行没造：{shadow_of} 不存在或不是线上行")
        elif shadow_of:
            fx.notes.append("影子草稿行没造：单行形态下 id 相同的两行造不出来")
        c.commit()
    return fx


# ── 线上行守卫：改完能原样还回去 ──────────────────────────────


class LiveRowGuard:
    """记住若干节点及其边的**完整行**，退出时逐列写回、把新增的行删掉。

    为什么要做到这一步：核心断言（§12）要求「做一组真实编辑，再取一次前台快照」。
    真实编辑就是真的改库，而受试对象是库里的真数据；不能原样还回去的话，
    第二次跑的基线已经不是第一次那个基线，测试就成了一次性的。

    `skill_composition` 那处是**裸 DELETE**（§4 特别点名），边会真的消失而不是变状态，
    所以边不能只靠 UPDATE 复原 —— 作用域内全删再按捕获重建。
    """

    def __init__(self, node_ids: Iterable[str] = (), edge_scope: Iterable[str] = ()):
        self.node_ids = [x for x in node_ids if x]
        self.edge_scope = [x for x in edge_scope if x] or list(self.node_ids)
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self._caps: dict[str, Any] = {}

    def capture(self) -> "LiveRowGuard":
        from backend.kg.pg_store.client import connect

        self._caps = db_capabilities()
        with connect() as c:
            self.nodes = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM kg_node WHERE id = ANY(%s)", (self.node_ids,)
                ).fetchall()
            ]
            self.edges = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM kg_edge WHERE src_id = ANY(%s) OR dst_id = ANY(%s)",
                    (self.edge_scope, self.edge_scope),
                ).fetchall()
            ]
        return self

    def live_node(self, nid: str) -> dict | None:
        for r in self.nodes:
            if r["id"] == nid and not r.get("is_draft", False):
                return r
        return None

    def restore(self) -> None:
        from backend.kg.pg_store.client import connect

        has_draft = self._caps.get("has_is_draft")
        with connect() as c:
            # ① 节点：捕获过的逐列写回；期间新增的（含草稿行）删掉
            kept = {(r["id"], bool(r.get("is_draft", False))) for r in self.nodes}
            cur = c.execute(
                "SELECT * FROM kg_node WHERE id = ANY(%s)", (self.node_ids,)
            ).fetchall()
            for r in cur:
                key = (r["id"], bool(dict(r).get("is_draft", False)))
                if key in kept:
                    continue
                if has_draft:
                    c.execute(
                        "DELETE FROM kg_node WHERE id=%s AND is_draft=%s", (key[0], key[1])
                    )
                else:
                    c.execute("DELETE FROM kg_node WHERE id=%s", (key[0],))
            for row in self.nodes:
                cols = [k for k in row if k != "id"]
                sets = ", ".join(f"{k} = %({k})s" for k in cols)
                where = "id = %(id)s"
                if has_draft:
                    where += " AND is_draft = %(is_draft)s"
                n = c.execute(
                    f"UPDATE kg_node SET {sets} WHERE {where}", row
                ).rowcount
                if n == 0:
                    keys = list(row)
                    c.execute(
                        f"INSERT INTO kg_node ({', '.join(keys)}) "
                        f"VALUES ({', '.join('%(' + k + ')s' for k in keys)})",
                        row,
                    )
            # ② 边：作用域内全删再重建（裸 DELETE 会让边真的消失，UPDATE 复原不了）
            c.execute(
                "DELETE FROM kg_edge WHERE src_id = ANY(%s) OR dst_id = ANY(%s)",
                (self.edge_scope, self.edge_scope),
            )
            for row in self.edges:
                keys = list(row)
                c.execute(
                    f"INSERT INTO kg_edge ({', '.join(keys)}) "
                    f"VALUES ({', '.join('%(' + k + ')s' for k in keys)})",
                    row,
                )
            c.commit()

    def __enter__(self) -> "LiveRowGuard":
        return self.capture()

    def __exit__(self, *exc: Any) -> bool:
        self.restore()
        return False


def make_draft_of(node_id: str, **overrides: Any) -> dict:
    """给一个已发布节点造一份草稿行（复制线上行 + 覆盖若干列）。

    发布相关的用例要精确控制 `base_version`（并发 409）、`attrs.code`（编码冲突）、
    `target_status`（下架意图）。走应用层写路径造不出这些形态，也不该走 ——
    那样测的是写路径而不是发布逻辑。直接插行、形态完全可控。
    """
    from backend.kg.pg_store.client import connect

    caps = db_capabilities()
    if not caps["has_is_draft"]:
        raise DraftUnsupported("没有 is_draft 列")
    with connect() as c:
        row = c.execute(
            "SELECT * FROM kg_node WHERE id=%s AND NOT is_draft", (node_id,)
        ).fetchone()
        if not row:
            raise DraftUnsupported(f"{node_id} 没有线上行")
        d = dict(row)
        d["is_draft"] = True
        d["status"] = "draft"
        d["base_version"] = d.get("version")
        d["target_status"] = None
        d.update(overrides)
        if isinstance(d.get("attrs"), dict):
            d["attrs"] = json.dumps(d["attrs"], ensure_ascii=False)
        keys = [k for k in d if k in caps["node_columns"]]
        c.execute("DELETE FROM kg_node WHERE id=%s AND is_draft", (node_id,))
        c.execute(
            f"INSERT INTO kg_node ({', '.join(keys)}) "
            f"VALUES ({', '.join('%(' + k + ')s' for k in keys)})",
            {k: d[k] for k in keys},
        )
        c.commit()
    return d


def make_draft_edge(
    eid: str, src: str, dst: str, rel: str, *, weight: float | None = 0.2,
    unit_id: str | None = None,
) -> None:
    from backend.kg.pg_store.client import connect

    with connect() as c:
        c.execute("DELETE FROM kg_edge WHERE id=%s AND is_draft", (eid,))
        _insert_edge(c, eid, src, dst, rel, is_draft=True, weight=weight, unit_id=unit_id)
        c.commit()


def live_row(nid: str) -> dict | None:
    """取某 id 的**线上行**（`is_draft=false`）。"""
    from backend.kg.pg_store.client import connect

    caps = db_capabilities()
    sql = "SELECT * FROM kg_node WHERE id=%s"
    if caps["has_is_draft"]:
        sql += " AND NOT is_draft"
    with connect() as c:
        r = c.execute(sql, (nid,)).fetchone()
    return dict(r) if r else None


def draft_rows_of(nid: str) -> list[dict]:
    from backend.kg.pg_store.client import connect

    caps = db_capabilities()
    if not caps["has_is_draft"]:
        return []
    with connect() as c:
        return [
            dict(r)
            for r in c.execute(
                "SELECT * FROM kg_node WHERE id=%s AND is_draft", (nid,)
            ).fetchall()
        ]


def bad_status_draft_rows() -> dict[str, list[tuple[str, str]]]:
    """§0.2 的不变量：全库任何 `is_draft=true` 的行，`status` 都必须是 `'draft'`。

    这个不变量一破，前台那 ~120 处 `status='published'` 的查询立刻命中草稿行 ——
    方案里写明这是**唯一的静默失效点**。所以每个写路径用例跑完都过一遍它。
    """
    from backend.kg.pg_store.client import connect

    caps = db_capabilities()
    if not caps["has_is_draft"]:
        return {"kg_node": [], "kg_edge": []}
    out: dict[str, list[tuple[str, str]]] = {}
    with connect() as c:
        for tbl in ("kg_node", "kg_edge"):
            out[tbl] = [
                (r["id"], r["status"])
                for r in c.execute(
                    f"SELECT id, status FROM {tbl} "
                    "WHERE is_draft AND COALESCE(status,'published') <> 'draft' LIMIT 20"
                ).fetchall()
            ]
    return out


# ── 路由表遍历 ───────────────────────────────────────────────


@dataclass(frozen=True)
class Case:
    label: str
    method: str
    path: str            # 已填好 path 参数的具体路径
    params: tuple[tuple[str, str], ...] = ()
    kind: str = "front"  # front | admin
    body_json: str | None = None   # POST 读接口（/v1/graph/expand）用


def _tag_kind(tags: Iterable[str]) -> str | None:
    for t in tags or ():
        if str(t).startswith(FRONT_TAG_PREFIX):
            return "front"
        if str(t).startswith(ADMIN_TAG_PREFIX):
            return "admin"
    return None


def get_routes(app: Any, kind: str = "front") -> list[tuple[str, dict]]:
    """从真实路由表摘 GET 接口：返回 [(path_format, operation)]。"""
    spec = app.openapi()
    out = []
    for path, ops in sorted(spec["paths"].items()):
        op = ops.get("get")
        if not op:
            continue
        if _tag_kind(op.get("tags") or []) != kind:
            continue
        out.append((path, op))
    return out


def _fill_path(path: str, values: dict[str, str]) -> str | None:
    """把 `{node_id}` 之类换成实参；有换不掉的就返回 None（跳过该路由）。"""
    out = path
    while "{" in out:
        i, j = out.index("{"), out.index("}")
        name = out[i + 1 : j]
        val = values.get(name)
        if val is None:
            return None
        out = out[:i] + val + out[j + 1 :]
    return out


def _query_defaults(op: dict, values: dict[str, str]) -> dict[str, str]:
    """按参数名把已知实参都填上。

    **不能只填 required**：`/v1/capability`、`/v1/majors/occupations`、
    `/v1/occupations/skills`、`/v1/graph/by-industry`、`/v1/industry-graph` 的
    「二选一」参数在 schema 里都是 optional，只填 required 会全部拿到 400 ——
    于是整轮扫描都跑在「参数不合法」的浅路径上，一条数据都没读，扫不出任何泄漏。
    """
    q: dict[str, str] = {}
    for p in op.get("parameters", []):
        if p.get("in") != "query":
            continue
        name = p["name"]
        if name in SMALL:
            q[name] = SMALL[name]
        elif values.get(name):
            q[name] = values[name]
        elif p.get("required"):
            return {}  # 必填参数没有可用实参 → 该路由跳过（返回空表示放弃）
    return q


def real_values(real: dict[str, str]) -> dict[str, str]:
    """真实 id / 名字 → 参数名。一份，baseline 与 leak 两组用例共用。"""
    v = dict(real)
    v.setdefault("node_id", real.get("occupation_id", ""))
    v.setdefault("id", real.get("occupation_id", ""))
    v.setdefault("position_id", real.get("occupation_id", ""))
    v.setdefault("profession_id", real.get("major_id", ""))
    v.setdefault("session_id", "999999999")
    v.setdefault("q", "工")
    v.setdefault("name", real.get("major_name", "工"))
    v.setdefault("industry", real.get("industry_name", ""))
    v.setdefault("major", real.get("major_name", ""))
    v.setdefault("src_id", real.get("occupation_id", ""))
    v.setdefault("dst_id", real.get("skill_id", ""))
    return v


def baseline_cases(app: Any, real: dict[str, str], kind: str = "front") -> list[Case]:
    """基线：每个 GET 一条，用真实 id。**快照对比用这一组。**"""
    values = real_values(real)
    cases: list[Case] = []
    for path, op in get_routes(app, kind):
        concrete = _fill_path(path, values)
        if concrete is None:
            continue
        need = [p["name"] for p in op.get("parameters", [])
                if p.get("in") == "query" and p.get("required")]
        if any(not values.get(n) for n in need):
            continue
        q = _query_defaults(op, values)
        cases.append(
            Case(f"baseline {path}", "GET", concrete, tuple(sorted(q.items())), kind)
        )
    if kind == "front":
        cases.extend(post_read_cases(values))
    return cases


# `/v1/graph/expand` 是**读**接口却是 POST（对齐 Graph Explorer 的 expand 语义），
# 遍历 GET 路由摘不到它。方案 §12 点名「图检索也搜不到」，expand 就是图检索的另一半，
# 漏了它等于把「点开某个真实岗位的邻居」这条最常用的图操作排除在泄漏扫描外。
_POST_READ = [("/v1/graph/expand", "node_id")]


def post_read_cases(values: dict[str, str]) -> list[Case]:
    out = []
    for path, key in _POST_READ:
        v = values.get(key)
        if not v:
            continue
        out.append(
            Case(
                f"baseline POST {path}[{key}]",
                "POST",
                path,
                (),
                "front",
                json.dumps({key: v, "limit": 25}, ensure_ascii=False),
            )
        )
    return out


def leak_cases(app: Any, real: dict[str, str], fx: DraftFixture, kind: str = "front") -> list[Case]:
    """泄漏探针：把草稿 id / 哨兵关键字塞进每个「像 id」的参数与每个 path 参数。

    逐个参数替换而不是只查列表接口 —— §6.1 里那两处「按 id 点查且完全没有状态过滤」
    的既有 bug 就只有按 id 点查才踩得到。
    """
    n = fx.node_ids
    all_kinds = [n["occupation"], n["major"], n["industry"], n["skill_level"], n["course"]]
    draft_by_param: dict[str, list[str]] = {
        "id": all_kinds,
        "node_id": all_kinds,
        "position_id": [n["occupation"]],
        "occupation_id": [n["occupation"]],
        "profession_id": [n["major"]],
        "major_id": [n["major"]],
        "industry_id": [n["industry"]],
        "skill_id": [n["skill_level"]],
        "src_id": [n["occupation"]],
        "dst_id": [n["skill_level"]],
        "skill_key": [DRAFT_SKILL_KEY],
        "prereq_key": [DRAFT_SKILL_KEY],
        "q": [SENTINEL],
        "name": [f"{SENTINEL}专业"],
    }
    base_values = real_values(real)

    cases: list[Case] = []
    for path, op in get_routes(app, kind):
        qparams = [p for p in op.get("parameters", []) if p.get("in") == "query"]
        pnames = [
            path[i + 1 : path.index("}", i)]
            for i, ch in enumerate(path)
            if ch == "{"
        ]
        # ① path 参数逐个换成草稿 id
        for pn in pnames:
            for dv in draft_by_param.get(pn, []):
                vals = {**base_values, pn: dv}
                concrete = _fill_path(path, vals)
                if concrete is None:
                    continue
                q = _query_defaults(op, vals)
                cases.append(
                    Case(f"draft {pn}={dv} {path}", "GET", concrete,
                         tuple(sorted(q.items())), kind)
                )
        # ② query 参数逐个换成草稿 id / 哨兵关键字
        concrete = _fill_path(path, base_values)
        if concrete is None:
            continue
        for p in qparams:
            for dv in draft_by_param.get(p["name"], []):
                vals = {**base_values, p["name"]: dv}
                q = _query_defaults(op, vals)
                if not q and any(x.get("required") for x in qparams):
                    continue
                q[p["name"]] = dv
                cases.append(
                    Case(f"draft {p['name']}={dv} {path}", "GET", concrete,
                         tuple(sorted(q.items())), kind)
                )
    if kind == "front":
        for path, key in _POST_READ:
            for dv in all_kinds:
                cases.append(
                    Case(
                        f"draft {key}={dv} POST {path}",
                        "POST",
                        path,
                        (),
                        "front",
                        json.dumps({key: dv, "limit": 25}, ensure_ascii=False),
                    )
                )
    return cases


def admin_control_cases(fx: DraftFixture) -> list[Case]:
    """**正对照**：管理台必须看得见这批草稿。

    只断言「前台看不见」会有一种假绿：受试对象根本没造成功 / 造在了任何接口都读不到的
    地方，于是所有响应里当然没有哨兵串，测试全绿而泄漏面一寸没测。所以每轮扫描都要有
    一组「必须命中」的用例。
    """
    occ, sk = fx.node_ids["occupation"], fx.node_ids["skill_level"]
    return [
        Case("正对照 管理台节点列表(草稿)", "GET", "/v1/kg/nodes",
             (("q", SENTINEL), ("scope", "manage"), ("page_size", "50")), "admin"),
        Case("正对照 管理台节点详情", "GET", "/v1/kg/node-detail", (("id", occ),), "admin"),
        Case("正对照 管理台边列表", "GET", "/v1/kg/edges",
             (("node_id", occ), ("scope", "manage"), ("page_size", "50")), "admin"),
        Case("正对照 管理台技能库", "GET", "/v1/admin/skills",
             (("q", DRAFT_SKILL_KEY), ("status", "all"), ("page_size", "50")), "admin"),
        Case("正对照 管理台技能构成", "GET", "/v1/admin/composition",
             (("node_id", occ),), "admin"),
        Case("正对照 管理台节点详情(技能)", "GET", "/v1/kg/node-detail", (("id", sk),), "admin"),
    ]


@dataclass
class Result:
    case: Case
    status: int
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def _call(client: Any, c: Case) -> Any:
    if c.method == "POST":
        return client.post(c.path, params=dict(c.params), json=json.loads(c.body_json or "{}"))
    return client.get(c.path, params=dict(c.params))


def run_cases(client: Any, cases: list[Case]) -> list[Result]:
    out = []
    for c in cases:
        r = _call(client, c)
        out.append(Result(c, r.status_code, r.content))
    return out


def find_leaks(results: list[Result], tokens: list[str]) -> list[str]:
    """响应里出现哨兵串即为泄漏 —— 但**请求里自己带进去的那个串不算**。

    多数接口会把入参回显（`/v1/student/learn/resources` 的 `skill_hint`、
    `/v1/student/diagnosis/report` 的 `target_occupation_id`），把回显算成泄漏会
    刷出一片假红，真泄漏就淹没了。判定口径：出现了**没随请求送出去过**的哨兵串。

    两组用例正好互补：`?id=<草稿 id>` 这组排掉 id、仍能抓到草稿的**名字**；
    `?q=<哨兵名>` 那组排掉名字、仍能抓到草稿的 **id**。
    """
    bad = []
    for r in results:
        sent = f"{r.case.path} {dict(r.case.params)} {r.case.body_json or ''}"
        hit = [t for t in tokens if t in r.text and t not in sent]
        if hit:
            bad.append(
                f"{r.case.method} {r.case.path}?{dict(r.case.params)} "
                f"→ HTTP {r.status} 命中 {hit}"
            )
    return bad


def snapshot(client: Any, cases: list[Case]) -> dict[str, tuple[int, bytes]]:
    """前台响应快照：`{case_label: (status, raw_bytes)}`，供逐字节比对。"""
    out: dict[str, tuple[int, bytes]] = {}
    for c in cases:
        r = _call(client, c)
        out[c.label] = (r.status_code, r.content)
    return out


def diff_snapshots(
    before: dict[str, tuple[int, bytes]],
    after: dict[str, tuple[int, bytes]],
    skip: set[str] | None = None,
) -> list[str]:
    skip = skip or set()
    out = []
    for k in sorted(set(before) | set(after)):
        if k in skip:
            continue
        b, a = before.get(k), after.get(k)
        if b == a:
            continue
        if b is None or a is None:
            out.append(f"{k}: 快照缺失（before={b is not None} after={a is not None}）")
            continue
        if b[0] != a[0]:
            out.append(f"{k}: HTTP {b[0]} → {a[0]}")
        else:
            out.append(
                f"{k}: 响应体变了（{len(b[1])} → {len(a[1])} 字节）"
                f" 首处差异 @{_first_diff(b[1], a[1])}"
            )
    return out


def _first_diff(a: bytes, b: bytes) -> str:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            lo = max(0, i - 40)
            return (
                f"{i}: …{a[lo:i + 40].decode('utf-8', 'replace')} "
                f"≠ …{b[lo:i + 40].decode('utf-8', 'replace')}"
            )
    return f"{n}（一方是另一方的前缀）"
