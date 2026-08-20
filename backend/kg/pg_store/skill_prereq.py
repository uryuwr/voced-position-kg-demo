"""逻辑技能先修：kg_skill_prereq + 无环校验。"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from backend.kg.pg_store.client import connect
from backend.kg.pg_store.config import DEFAULT_REGION


def _names_for(*keys: str) -> dict[str, str]:
    """先修两端的展示名，一次查完；查不到的键不出现在结果里（调用方回落成 code）。

    连草稿行一起查：管理台配先修时技能常常还没发布，只读线上行会拿不到名字。
    这些回执只给运营看，不存在草稿泄漏到学员端的问题。
    """
    from backend.kg.pg_store.skill_aggregate import resolve_skill_names

    return resolve_skill_names([k for k in keys if k], online_only_rows=False)


def list_prereqs(skill_key: str, *, region: str | None = None) -> list[dict[str, Any]]:
    reg = region or DEFAULT_REGION
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT skill_key, prereq_skill_key, region, evidence, confidence, created_at
            FROM kg_skill_prereq
            WHERE region = %s AND skill_key = %s
            ORDER BY prereq_skill_key
            """,
            (reg, skill_key),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if hasattr(d.get("created_at"), "isoformat"):
            d["created_at"] = d["created_at"].isoformat()
        out.append(d)
    # 两端都是 code，出参里必须配同层展示名（否则管理台的先修列表是两串哈希）。
    # 一次批量查，不做 N+1；查不到就退回 code —— 指向已删技能的历史先修边仍要看得见。
    # 走 pg_store 自己那份解析：原来 import 的是 `userprofile.skill_display`，
    # 依赖方向反了（kg/pg_store 不该依赖 userprofile），且那样就有了第二份同义 SQL。
    if out:
        from backend.kg.pg_store.skill_aggregate import resolve_skill_names

        m = resolve_skill_names(
            [d.get("skill_key") for d in out] + [d.get("prereq_skill_key") for d in out]
        )
        for d in out:
            k, pk = d.get("skill_key") or "", d.get("prereq_skill_key") or ""
            d["skill_name"] = m.get(k) or k
            d["prereq_skill_name"] = m.get(pk) or pk
    return out


def prereq_map(
    conn, skill_keys: list[str], *, region: str | None = None
) -> dict[str, list[dict[str, str]]]:
    """批量取 `{技能 → 先修技能 [{skill_key, skill_name}]}`，供「技能列表」类接口一次查完。

    **只按左端 `skill_key` 筛，不要求先修技能也落在传入集合内**——原型要展示的是
    「这个技能真正的前置」，哪怕那个前置不在本岗位的技能集里（学员照样得先会它）。
    这与技能图谱（`industry_graph`）的语义**不同**：那里画的是集合内的依赖连线，
    两端都必须在集内才有边可画。两处都对，别互相照搬。

    **返回对象而不是裸 code 列表**（2026-08-20 改）：原来给的是
    `list[str]`，2026-08-19 之后那串 str 全是 `SKxxxxxxxxxx`。因为先修可以指向
    本集合之外的技能，调用方**没法**拿同一份 `skills[]` 反查名字 —— 于是
    `frontend/admin-console.html` 自己维护了一张 `skillLabel()` 映射表来兜，
    即前端在补后端的坑，而外部管理台前端没有这张表，先修那一列就是两串哈希。
    名字在这里配齐，调用方直接渲染。

    名字**只从线上行取**（`online_only_rows` 默认 True）：这个 map 前台和管理台
    共用（`skill_composition.get_composition` 两个 scope 都走它），取草稿行的名字
    等于把未发布的改名泄漏到学员端。`kg_skill_prereq` 本身没有草稿态（先修立即
    生效），指向只有草稿行的技能时回落成 code，那种边本来就该被 `dangling` 标出来。

    收 `conn` 而不自己 `connect()`：调用方普遍已持有连接，先修查询总是与技能列表
    查询同处一个请求，多开一条连接纯属浪费（本项目已因此栽过性能问题）。
    """
    keys = sorted({k for k in (skill_keys or []) if k})
    if not keys:
        return {}
    raw: dict[str, list[str]] = {}
    for r in conn.execute(
        """
        SELECT skill_key, prereq_skill_key FROM kg_skill_prereq
        WHERE region = %s AND skill_key = ANY(%s)
        ORDER BY skill_key, prereq_skill_key
        """,
        (region or DEFAULT_REGION, keys),
    ).fetchall():
        raw.setdefault(r["skill_key"], []).append(r["prereq_skill_key"])
    if not raw:
        return {}

    from backend.kg.pg_store.skill_aggregate import resolve_skill_names

    # 先修可以指向传入集合之外的技能，所以名字要按右端单独收集，不能复用 keys
    names = resolve_skill_names({p for lst in raw.values() for p in lst}, conn)
    return {
        k: [{"skill_key": p, "skill_name": names.get(p) or p} for p in lst]
        for k, lst in raw.items()
    }


def _would_cycle(
    skill_key: str, prereq_skill_key: str, *, region: str
) -> bool:
    """若 skill → prereq，再从 prereq 能否走到 skill。"""
    if skill_key == prereq_skill_key:
        return True
    with connect() as conn:
        rows = conn.execute(
            "SELECT skill_key, prereq_skill_key FROM kg_skill_prereq WHERE region=%s",
            (region,),
        ).fetchall()
    graph: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        graph[r["skill_key"]].append(r["prereq_skill_key"])
    graph[skill_key].append(prereq_skill_key)
    # BFS from prereq along edges skill→prereq meaning "depends on"
    # Cycle if we can reach skill_key starting from prereq following deps
    q = deque([prereq_skill_key])
    seen = {prereq_skill_key}
    while q:
        cur = q.popleft()
        for nxt in graph.get(cur, []):
            if nxt == skill_key:
                return True
            if nxt not in seen:
                seen.add(nxt)
                q.append(nxt)
    return False


def add_prereq(
    skill_key: str,
    prereq_skill_key: str,
    *,
    region: str | None = None,
    evidence: str | None = None,
    confidence: str = "manual_seed",
    created_by: str | None = None,
) -> dict[str, Any]:
    reg = region or DEFAULT_REGION
    sk = (skill_key or "").strip()
    pk = (prereq_skill_key or "").strip()
    if not sk or not pk:
        raise ValueError("skill_key 与 prereq_skill_key 必填")
    if sk == pk:
        raise ValueError("不能以自身为先修")
    if _would_cycle(sk, pk, region=reg):
        raise ValueError(f"添加先修会成环: {sk} → {pk}")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO kg_skill_prereq
              (skill_key, prereq_skill_key, region, evidence, confidence, created_by)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (region, skill_key, prereq_skill_key) DO UPDATE SET
              evidence = EXCLUDED.evidence,
              confidence = EXCLUDED.confidence
            """,
            (sk, pk, reg, evidence, confidence, created_by),
        )
        conn.commit()
    # 两端都是 code，回执要配名字（同 list_prereqs）
    _nm = _names_for(sk, pk)
    return {
        "skill_key": sk,
        "skill_name": _nm.get(sk) or sk,
        "prereq_skill_key": pk,
        "prereq_skill_name": _nm.get(pk) or pk,
        "region": reg,
        "evidence": evidence,
        "confidence": confidence,
    }


def remove_prereq(
    skill_key: str, prereq_skill_key: str, *, region: str | None = None
) -> bool:
    reg = region or DEFAULT_REGION
    with connect() as conn:
        cur = conn.execute(
            """
            DELETE FROM kg_skill_prereq
            WHERE region=%s AND skill_key=%s AND prereq_skill_key=%s
            """,
            (reg, skill_key, prereq_skill_key),
        )
        conn.commit()
        return cur.rowcount > 0


def _cycle_in_replacement(
    skill_key: str, prereq_keys: list[str], *, region: str
) -> str | None:
    """把 `skill_key` 的先修整体换成 `prereq_keys` 后会不会成环。

    不能逐条调 `_would_cycle`：那个函数是**基于库里当前的图**判断「再加一条」，
    而整体替换时旧边即将消失。先删后逐条校验（原来的做法）能算对，代价是必须
    先把库改了 —— 中途报错就把运营原有的先修列表毁了（见 `set_prereqs`）。
    这里在内存里做替换，一次校验全部，库一个字都还没动。

    返回第一个导致成环的先修名（用于报错文案），无环返回 None。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT skill_key, prereq_skill_key FROM kg_skill_prereq WHERE region=%s",
            (region,),
        ).fetchall()
    graph: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if r["skill_key"] == skill_key:
            continue                      # 旧边被这次替换整体覆盖，不参与判断
        graph[r["skill_key"]].append(r["prereq_skill_key"])
    graph[skill_key] = list(prereq_keys)

    # 逐个先修单独判：报错要能指名道姓说是哪一个成环，
    # 只回一句「会成环」运营得自己一条条试。
    for pk in prereq_keys:
        q = deque([pk])
        seen = {pk}
        while q:
            cur = q.popleft()
            if cur == skill_key:
                return pk
            for nxt in graph.get(cur, []):
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
    return None


def set_prereqs(
    skill_key: str,
    prereq_keys: list[str],
    *,
    region: str | None = None,
    created_by: str | None = None,
) -> list[dict[str, Any]]:
    """整体替换某技能的先修列表 —— **先全量校验，再一个事务里删+写**。

    原来是「先 DELETE 并 commit，再逐条 add_prereq」，两个问题：
    - 第 N 条成环时抛异常，而 DELETE 已经提交、前 N-1 条已写入：接口回 400，
      运营原有的先修列表**已经被清空了**，比单纯保存失败严重得多。
    - 删和写不在一个事务里，中间有个「先修为空」的时间窗，并发读会读到空列表。
    """
    reg = region or DEFAULT_REGION
    sk = (skill_key or "").strip()
    if not sk:
        raise ValueError("skill_key 必填")

    # 去重 + 去空 + 去自身，保持传入顺序（运营在界面上的排序有意义）
    seen_pk: set[str] = set()
    keys: list[str] = []
    for raw in prereq_keys or []:
        pk = (raw or "").strip()
        if not pk or pk in seen_pk:
            continue
        if pk == sk:
            raise ValueError("不能以自身为先修")
        seen_pk.add(pk)
        keys.append(pk)

    bad = _cycle_in_replacement(sk, keys, region=reg)
    if bad:
        raise ValueError(f"先修会成环：{sk} → {bad} → … → {sk}")

    with connect() as conn:
        conn.execute(
            "DELETE FROM kg_skill_prereq WHERE region=%s AND skill_key=%s",
            (reg, sk),
        )
        for pk in keys:
            conn.execute(
                """
                INSERT INTO kg_skill_prereq
                  (skill_key, prereq_skill_key, region, evidence, confidence, created_by)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (region, skill_key, prereq_skill_key) DO UPDATE SET
                  evidence = EXCLUDED.evidence,
                  confidence = EXCLUDED.confidence
                """,
                (sk, pk, reg, None, "manual_seed", created_by),
            )
        conn.commit()

    nm = _names_for(sk, *keys)
    return [
        {
            "skill_key": sk,
            "skill_name": nm.get(sk) or sk,
            "prereq_skill_key": pk,
            "prereq_skill_name": nm.get(pk) or pk,
            "region": reg,
            "evidence": None,
            "confidence": "manual_seed",
        }
        for pk in keys
    ]
