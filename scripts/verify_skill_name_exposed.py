"""闸门：出参里凡出现 skill_key（`SK` + 10 位十六进制），同一对象内必须有非空展示名。

## 为什么需要它

`skill_key` 从 2026-08-19 起是 ASCII code（`backend/kg/skill_key.py`），
而「key 兼作展示名」这个假设散布在几十处读路径与前端模板里。改造后**连着补了三轮**：

1. 管理台与学员端 13 处前端展示位
2. 四个响应模型没声明 `skill_name` → 数据层给了也被 Pydantic 静默丢弃
3. `/v1/occupation-skills-graph` 的 `categories[].skills[]`

每一轮都是靠人打开页面看见一串哈希才发现的。判据「页面上不出现 SK 开头的串」只
覆盖打开过的页面 —— 这个脚本把判据下移到**接口层**，一次扫完所有 GET。

## 判据

递归遍历每个 GET 接口的响应 JSON。凡是某个 **object** 里有形如 `SK[0-9a-f]{10}`
的值，就要求**同一个 object** 里存在一个非空的展示名字段
（`skill_name` / `name` / `skill_node_name` / `display_name` / `label` / `title`）。

同一对象是关键：把名字放在父级或兄弟节点里，前端渲染那一项时拿不到。

**只报缺名字，不报 code 本身**。code 出现在出参里是对的 —— 它是主键，前端要用它
提交、比对、拼 URL。错的是「只有 code、没有名字」。

    PYTHONPATH=. python -X utf8 scripts/verify_skill_name_exposed.py
"""

from __future__ import annotations

import re
import sys
import warnings
from typing import Any
from urllib.parse import quote

warnings.filterwarnings("ignore")
import backend.settings as _st  # noqa: E402

_st.AUTH_BYPASS = True
_st.AUTH_DEBUG = True
import backend.api.auth as _au  # noqa: E402

_au.AUTH_BYPASS = True
_au.AUTH_DEBUG = True
from fastapi.testclient import TestClient  # noqa: E402

from backend.api.main import app  # noqa: E402
from backend.kg.pg_store.client import connect  # noqa: E402

def _probe_uid() -> str:
    """挑一个**库里真有数据**的用户。

    原来固定用 uid=0，而那个用户没有任何测评快照、没有锁定目标 —— 于是
    `/v1/student/goal/overview`、`/diagnosis/report` 这些读**落库历史数据**的接口
    扫到的全是空壳，判据形同虚设。历史快照恰恰是最容易留着旧形态的地方：
    迁移前落库的 27 份报告里 skill_key 存的是中文名（2026-08-19 那次改造）。
    """
    from backend.kg.pg_store.client import connect as _c

    with _c() as c:
        r = c.execute(
            """
            SELECT s.user_id FROM biz_diagnosis_result r
            JOIN biz_diagnosis_session s ON s.id = r.session_id
            WHERE s.user_id IS NOT NULL AND r.report_json ? 'items'
            ORDER BY r.created_at DESC LIMIT 1
            """
        ).fetchone()
        return str((r or {}).get("user_id") or "0")


PROBE_UID = _probe_uid()
cli = TestClient(
    app,
    headers={"X-Test-Uid": PROBE_UID, "X-Test-Uname": "nameprobe"},
    raise_server_exceptions=False,
)

CODE_RE = re.compile(r"^SK[0-9a-f]{10}$")
# 整串等于 code 用 CODE_RE；**句子里夹着 code**（「补齐技能：SK208ab276b3」）要用这个 ——
# CODE_RE 带 ^$ 锚点，拿它去 search 一句话永远匹配不上，判据会静默失效
CODE_ANY = re.compile(r"SK[0-9a-f]{10}")
# 任一个非空即算「有展示名」。顺序无所谓 —— 只要求存在，不规定用哪个
NAME_FIELDS = (
    "skill_name",
    "name",
    "skill_node_name",
    "display_name",
    "label",
    "title",
)
# 这些键装的是 code 列表 / code→值 的映射，天生没有同层名字，单独放行并说明理由
ALLOW_KEYS = {
    # 学习计划要的是 key 列表，用来生成计划、不上屏
    "gap_skills",
    # 画像里 {skill_key: level} 的映射，键就是 code
    "skills_by_key",
    "profile",
    # 先修边只表达「谁在谁前面」，两端 id 都在 skills[] 里能查到名字
    "prereqs",
    # `attrs` 是**原始属性包**（无约束 JSON 列的直出）。里面有 skill_key 是自然的，
    # 前端渲染的是它的父对象，不会单独渲染 attrs 的某一项。
    # 放行它而不是往 attrs 里塞名字：那等于往一个"随数据来源而异"的自由字段里
    # 加约定，下一个来源又不带，判据就靠不住了。
    "attrs",
}


def pick_ids() -> dict[str, str]:
    """挑几个真实 id 去填路径/必填参数，否则大量接口只能返回 404 空壳。"""
    out: dict[str, str] = {}
    with connect() as c:
        for key, sql in (
            ("occupation", "SELECT id FROM kg_node WHERE type='occupation' AND NOT is_draft AND COALESCE(status,'published')='published' ORDER BY id LIMIT 1"),
            ("major", "SELECT id FROM kg_node WHERE type='major' AND NOT is_draft AND COALESCE(status,'published')='published' ORDER BY id LIMIT 1"),
            ("industry", "SELECT id FROM kg_node WHERE type='industry' AND NOT is_draft AND COALESCE(status,'published')='published' ORDER BY id LIMIT 1"),
            ("major_name", "SELECT name AS id FROM kg_node WHERE type='major' AND NOT is_draft AND COALESCE(status,'published')='published' ORDER BY id LIMIT 1"),
        ):
            r = c.execute(sql).fetchone()
            if r:
                out[key] = r["id"]
        r = c.execute(
            "SELECT attrs::json->>'skill_key' k FROM kg_node WHERE type='skill_level' "
            "AND NOT is_draft AND COALESCE(status,'published')='published' "
            "AND attrs::json->>'skill_key' IS NOT NULL ORDER BY id LIMIT 1"
        ).fetchone()
        if r:
            out["skill_key"] = r["k"]
    return out


IDS = pick_ids()


def fill(name: str, schema: dict[str, Any]) -> str:
    """给路径/必填 query 填一个能命中数据的值。"""
    low = name.lower()
    # **整型判断必须在 id 判断之前**：`session_id` / `proposal_id` 是自增整数，
    # 按「名字以 _id 结尾」先命中的话会填一个节点 id 字符串进去，接口回 422，
    # 于是这些接口被算成「跳过」——判据静默缩水。
    if schema.get("type") == "integer":
        return "1"
    if low == "name":
        # /v1/graph/by-major 要的是专业**名字**，不是 id
        return IDS.get("major_name", "计算机应用技术")
    if "skill_key" in low or low == "key":
        return IDS.get("skill_key", "SK0000000000")
    if "occupation" in low or "position" in low:
        return IDS.get("occupation", "x")
    if "major" in low or "profession" in low:
        return IDS.get("major", "x")
    if "industry" in low:
        return IDS.get("industry", "x")
    if low.endswith("_id") or low == "id":
        return IDS.get("occupation", "x")
    if schema.get("type") == "integer":
        return "1"
    return "a"


def scan(obj: Any, path: str, hits: list[tuple[str, str, list[str]]]) -> None:
    """找出「含 code 但同层没名字」的对象。"""
    if isinstance(obj, dict):
        codes = [k for k, v in obj.items() if isinstance(v, str) and CODE_RE.match(v)]
        if codes:
            has_name = any(
                str(obj.get(f) or "").strip() and not CODE_RE.match(str(obj.get(f)))
                for f in NAME_FIELDS
            )
            if not has_name:
                hits.append((path, ",".join(codes), sorted(obj)[:8]))
        for k, v in obj.items():
            if k in ALLOW_KEYS:
                continue
            scan(v, f"{path}.{k}", hits)
    elif isinstance(obj, list):
        # 同构列表只查前两项就够，报错点一样
        for i, v in enumerate(obj[:2]):
            scan(v, f"{path}[{i}]", hits)


spec = app.openapi()
checked = 0
# 跳过的要**列出来**，不能只报个数字：「扫了 55 个」看着像扫全了，
# 而真正的覆盖边界是「哪些没扫到」——那才是下次漏掉的地方
skipped_list: list[str] = []
bad: list[tuple[str, str, str, list[str]]] = []

for p, ops in sorted((spec.get("paths") or {}).items()):
    op = (ops or {}).get("get")
    if not op:
        continue
    url, qs = p, []
    for prm in op.get("parameters") or []:
        nm, sch = prm["name"], (prm.get("schema") or {})
        if prm.get("in") == "path":
            url = url.replace("{" + nm + "}", quote(fill(nm, sch), safe=""))
        elif prm.get("in") == "query" and prm.get("required"):
            qs.append(f"{nm}={quote(fill(nm, sch), safe='')}")
    if "{" in url:
        skipped_list.append(f"{p}（路径参数填不出真实值）")
        continue
    full = url + ("?" + "&".join(qs) if qs else "")
    r = cli.get(full)
    if r.status_code != 200:
        # 有一批接口的 id 参数是**可选**的，但「至少给一个」——裸调直接 400。
        # 再用可选的 id 类参数试一次，否则这些恰好都返回技能的接口全被跳过，
        # 而它们正是最可能藏漏网的地方（/v1/capability、/v1/industry-graph…）。
        extra = []
        for prm in op.get("parameters") or []:
            nm = prm["name"]
            if prm.get("in") != "query" or prm.get("required"):
                continue
            low = nm.lower()
            if low.endswith("_id") or low in ("industry", "major", "occupation", "name"):
                extra.append(f"{nm}={quote(fill(nm, prm.get('schema') or {}), safe='')}")
        if extra:
            full2 = url + "?" + "&".join(qs + extra)
            r2 = cli.get(full2)
            if r2.status_code == 200:
                full, r = full2, r2
    if r.status_code != 200:
        skipped_list.append(f"{p}（HTTP {r.status_code}）")
        continue
    try:
        body = r.json()
    except Exception:
        skipped_list.append(f"{p}（响应不是 JSON）")
        continue
    checked += 1
    hits: list[tuple[str, str, list[str]]] = []
    scan(body, "", hits)
    for hpath, hcodes, hkeys in hits[:3]:
        bad.append((full, hpath or "(root)", hcodes, hkeys))

def check_learning_plan_payload() -> list[str]:
    """**出站** payload 也要查 —— 这个方向本来完全在闸门视野外。

    上面扫的是自家 GET 出参；而学习计划是我们**只发不收**地推给外部服务的，
    错值在本服务任何页面上都看不见，落库的 `path_snapshot` 又是照发的原样。
    2026-08-20 就这么漏出去过：任务名是 `补齐技能：SK208ab276b3（目标 专家）`、
    `skills[].skill_name` 也塞的是 code —— 是学员在对方页面上看见才发现的。

    只构造 payload、**不推送**（推送是对外动作，闸门不该有副作用）。
    """
    from backend.kg.pg_store import biz_store as _biz
    from backend.kg.pg_store.client import connect as _c
    from backend.learningplan import build_payload

    from backend import settings as _s

    with _c() as c:
        r = c.execute(
            """
            SELECT s.id, s.user_id, s.target_occupation_id AS occ
            FROM biz_diagnosis_session s
            JOIN biz_diagnosis_result r ON r.session_id = s.id
            WHERE s.target_occupation_id IS NOT NULL
            ORDER BY r.created_at DESC LIMIT 1
            """
        ).fetchone()
    if not r:
        return ["（跳过）库里没有带结果的诊断会话"]
    try:
        payload = build_payload(
            session_id=r["id"],
            region=_s.KG_REGION,
            occupation_id=r["occ"],
            occupation_name=r["occ"],
            skills=_biz.position_skills(r["occ"], limit=12, aggregate=True),
            report=_biz.get_diagnosis_report(r["user_id"], session_id=r["id"]) or {},
            courses_by_key={},
            revision_of=None,
        )
    except ValueError as e:
        return [f"（跳过）构造不出 payload：{e}"]
    d = payload.model_dump() if hasattr(payload, "model_dump") else payload
    out: list[str] = []
    for ph in d.get("phases") or []:
        for t in ph.get("tasks") or []:
            if CODE_ANY.search(str(t.get("name") or "")):
                out.append(f"任务名里有 code：{t.get('name')}")
            for sk in t.get("skills") or []:
                if CODE_RE.match(str(sk.get("skill_name") or "")):
                    out.append(f"skills[].skill_name 是 code：{sk}")
        if CODE_ANY.search(str(ph.get("phase_name") or "")):
            out.append(f"阶段名里有 code：{ph.get('phase_name')}")
    return out


def check_memory_signal_text() -> list[str]:
    """**出站** 五维记忆文本 —— 与学习计划同一个形状，而后果更重。

    2026-08-19 skill_key 改成 code 后这里漏改了一天，灌进画像平台的是
    「`SKa1fa1d005d` 达到 3 级」。它比页面上的哈希难查也难收：
    平台把这段文本当**语义证据长期保存**供别的岗位推断，而 `Idempotency-Key`
    按 session 派生、重推覆盖不了；读回来那端（`profile.recall_skills`）
    按技能名做子串召回，写哈希进去永远召不回。

    只构造文本、**不提交**（提交是对外动作，闸门不该有副作用）。
    """
    from backend.kg.pg_store import biz_store as _biz
    from backend.kg.pg_store.client import connect as _c
    from backend.userprofile.sync import _writable, build_text

    with _c() as c:
        rows = c.execute(
            """
            SELECT s.id, s.user_id, s.target_occupation_id AS occ
            FROM biz_diagnosis_session s
            JOIN biz_diagnosis_result r ON r.session_id = s.id
            WHERE s.target_occupation_id IS NOT NULL
            ORDER BY r.created_at DESC LIMIT 5
            """
        ).fetchall()
    if not rows:
        return ["（跳过）库里没有带结果的诊断会话"]

    out: list[str] = []
    checked_any = False
    for r in rows:
        rep = _biz.get_diagnosis_report(r["user_id"], session_id=r["id"]) or {}
        if not rep.get("items"):
            continue
        checked_any = True
        text = build_text(rep, occupation_name=r["occ"] or "目标岗位")
        if CODE_ANY.search(text):
            out.append(f"session {r['id']} 的记忆文本里有 code：…{CODE_ANY.search(text).group(0)}…")
        # 整批被丢说明上游 items 里根本没有展示名 —— 文本干净但证据全丢了，同样要红
        rows_ok, dropped = _writable(rep)
        if dropped and not rows_ok:
            out.append(
                f"session {r['id']}：{dropped} 项实测技能全都取不到展示名，"
                f"这次测评的证据一条也同步不出去"
            )
    if not checked_any:
        return ["（跳过）最近的诊断报告里都没有 items"]
    return out


print(f"探测用户 uid={PROBE_UID}（挑的是库里有测评快照的用户 —— "
      f"用没数据的用户扫，读历史快照的接口全是空壳）")
print(f"扫了 {checked} 个 GET 接口，跳过 {len(skipped_list)} 个：")
for s in skipped_list:
    print(f"    - {s}")
print(f"放行的键：{sorted(ALLOW_KEYS)}（装 code 列表或 code→值 映射，天生没有同层名字）")
print()
if bad:
    print(f"★ {len(bad)} 处「有 code 但同层没有展示名」：")
    for u, hp, hc, hk in bad:
        print(f"  {u[:70]}")
        print(f"      位置 {hp}  code 字段={hc}")
        print(f"      该对象的键: {hk}")
else:
    print("PASS 所有出参里的 skill_key 都配着同层展示名")

print("\n出站给学习计划服务的 payload（只构造不推送）：")
plan_bad = check_learning_plan_payload()
if plan_bad and plan_bad[0].startswith("（跳过）"):
    print(f"    {plan_bad[0]}")
    plan_bad = []
elif plan_bad:
    print(f"★ {len(plan_bad)} 处：")
    for x in plan_bad:
        print(f"    - {x}")
else:
    print("    PASS 任务名 / 阶段名 / skills[].skill_name 里没有裸 code")

print("\n出站给用户画像服务的五维记忆文本（只构造不提交）：")
mem_bad = check_memory_signal_text()
if mem_bad and mem_bad[0].startswith("（跳过）"):
    print(f"    {mem_bad[0]}")
    mem_bad = []
elif mem_bad:
    print(f"★ {len(mem_bad)} 处：")
    for x in mem_bad:
        print(f"    - {x}")
else:
    print("    PASS 记忆文本里没有裸 code，且实测技能的展示名都取到了")

sys.exit(1 if (bad or plan_bad or mem_bad) else 0)
