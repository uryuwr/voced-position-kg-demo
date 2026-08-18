"""BOSS 市场化岗位 → 技能构成 requires + 跨族晋升 advances_to（LLM，三阶段）。

为什么必须用 LLM
----------------
现有 8787 条 `requires` 是 **official**（国家职业标准），只覆盖 492 个大典职业。
市场化岗位（Java / 产品经理 / 算法工程师）**没有国标**，技能构成只能推断。
产出全部 `confidence=ai_inferred` 且必须带 evidence（本体 confidence 约定）。

三阶段：LLM 调用结果一律落盘
----------------------------
::

    collect → data/staging/boss_skill_raw_<l1>.json     每岗位的技能/职级原始产出
    merge   → data/staging/boss_skill_canon.json        技能名归并映射（规范名 ← 别名[]）
    apply   → SQLite（skill_level 节点 + requires/advances_to 边）

拆开的原因：`collect` 一次 117~821 次 LLM 调用，跑一次十几分钟。归并规则或建边
逻辑要调整时，只重跑后面两步，不重烧 token。每步都可独立重跑、可 --dry-run。

为什么需要 merge（方案 A）
-------------------------
LLM 逐岗位独立生成，同一技能会有多种叫法。实测 117 个岗位产出 643 个"新"技能，
平均每岗位 5.5 个，复用率几乎为零：

    .NET开发 / .NET框架应用      SQL开发 / SQL数据库开发      Git版本控制 / 版本管理工具

`canon_skill()` 只能消掉空格差异，语义重复要靠 merge 阶段让 LLM 归并。
不归并的话 821 个岗位会造出 4000+ 技能、2 万节点，三四成是重复概念。

为什么晋升链要跨族（方案 B）
---------------------------
BOSS 岗位分类**不含职级信息**：「后端开发」族里 Java / PHP / Go 是平行技术方向，
不是晋升关系，LLM 只能都判成 job_level 2。真实晋升是
`Java → 技术经理 → 技术总监`，要跨族判断，见 `--stage advance`。

用法::

    python -m crawlers.cn.link_boss_skill_chain --stage collect --l1 技术
    python -m crawlers.cn.link_boss_skill_chain --stage merge   --l1 技术
    python -m crawlers.cn.link_boss_skill_chain --stage apply   --l1 技术 --dry-run
    python -m crawlers.cn.link_boss_skill_chain --stage apply   --l1 技术
    python -m crawlers.cn.link_boss_skill_chain --stage advance --l1 技术
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, upsert_edges, upsert_nodes
from backend.kg.paths import REPORTS, STAGING
from backend.kg.provenance import make_edge_id, make_node_id, utc_now_iso

REGION = "CN"
SRC_SKILL = "LLM_CN"
SRC_LINK = "LINK_CN_AI"
SOURCE_URL = "llm://voced-kg/boss-skill-chain"
LICENSE = "LLM 推断，需人工抽检"
LEVELS = (1, 2, 3, 4, 5)

CATEGORIES = [
    "技术工程", "数据能力", "运营策略", "内容创作", "商业分析",
    "设计创意", "沟通协作", "管理领导", "通用素养",
]

_CJK = r"一-龥"

PROMPT_SKILL = """你是职业技能图谱专家。为下面这个招聘市场岗位输出技能构成与岗位层级。

岗位：{name}
所属：{l1} > {l2}

要求：
1. 输出 5-8 个该岗位**最核心**的技能，具体到可培训、可考核的粒度。
   - 好例子：Spring Boot 应用开发、SQL 查询优化、A/B 实验设计
   - 坏例子：编程能力、沟通能力（太泛）、认真负责（不是技能）
2. 每个技能给出：
   - name：技能名（8 字以内为佳，不带等级后缀）
   - level：该岗位要求的掌握档位，1=了解 2=基本掌握 3=熟练 4=精通 5=专家
   - weight：在该岗位能力结构中的权重，所有技能权重之和必须等于 1.0
   - category：从这些里选一个 {categories}
3. job_level：职级序列位置，1=入门/助理 2=专员 3=资深/主管 4=经理 5=总监以上
4. job_family：岗位族名称（如「后端开发」「产品管理」）

只输出 JSON：
{{"job_level": 2, "job_family": "后端开发", "skills": [
  {{"name": "技能名", "level": 3, "weight": 0.25, "category": "技术工程"}}
]}}"""

PROMPT_MERGE = """下面是从招聘岗位推断出的技能名列表，同一个技能常有多种叫法。
请把**指同一件事**的合并为一个规范名。

合并示例：
- ".NET开发" / ".NET框架应用" → ".NET开发"
- "SQL开发" / "SQL数据库开发" → "SQL开发"
- "Git版本控制" / "版本管理工具" → "Git版本控制"

规则：
1. 规范名从原列表里挑最准确、最通用的那个，不要自造新词
2. 只合并真正同义的；技术栈不同就不要合（Java开发 ≠ Kotlin开发；MySQL ≠ Redis）
3. 粒度差异大的不合（"编程基础" 不要吞掉 "Spring Boot开发"）
4. 没有同义词的技能也要出现在结果里（aliases 为空数组）

技能列表：
{names}

只输出 JSON：
{{"groups": [{{"canon": "规范名", "aliases": ["别名1", "别名2"]}}]}}"""

PROMPT_ADVANCE = """下面是同一领域内的招聘岗位及其职级判定。请找出**真实存在的晋升路径**。

岗位列表（格式：岗位名 | 岗位族 | 职级1-5）：
{jobs}

规则：
1. 只输出真实的职业晋升路径，from 的职级必须**低于** to 的职级
2. 允许跨岗位族（如 Java 工程师 → 技术经理 → 技术总监），这是主要场景
3. 平行的技术方向**不是**晋升（Java → PHP 不算，Java → Python 不算）
4. 一个岗位可以有多条向上路径（Java → 技术经理 / Java → 架构师）
5. 宁缺勿滥：不确定的不要输出

只输出 JSON：
{{"paths": [{{"from": "岗位名", "to": "岗位名", "reason": "一句话依据"}}]}}"""


def slug(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z一-龥]+", "_", s).strip("_")[:40]


def canon_skill(name: str) -> str:
    """技能名基础归一 —— 只处理空格，语义合并交给 merge 阶段。

    实测同批 LLM 产出里「C#编程开发」和「C# 编程开发」会被当成两个技能各建 5 个节点。
    规则：全角/连续空白压成一个半角空格；删除中文与非中文之间的空格
    （C# 编程开发 → C#编程开发）；保留英文词之间的空格（Web API 开发 → Web API开发）。
    """
    s = (name or "").replace("　", " ").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(rf"(?<=[{_CJK}])\s+(?=[^{_CJK}])", "", s)
    s = re.sub(rf"(?<=[^{_CJK}])\s+(?=[{_CJK}])", "", s)
    return s.strip()


def salvage_paths(text: str) -> list[dict]:
    """从**被截断**的 LLM 输出里抢救完整的路径对象。

    大批量输入时输出常超 max_tokens，JSON 尾部缺 `]}`，整体 json.loads 直接失败
    → 前面几十条完整路径全部丢掉。这里按对象级正则捞回来。
    """
    out = []
    for m in re.finditer(
        r'\{\s*"from"\s*:\s*"([^"]+)"\s*,\s*"to"\s*:\s*"([^"]+)"'
        r'(?:\s*,\s*"reason"\s*:\s*"([^"]*)")?\s*\}',
        text or "",
    ):
        out.append({"from": m.group(1), "to": m.group(2), "reason": m.group(3) or ""})
    return out


def core_token(name: str) -> str:
    """取技能名的核心词，用于把同族技能聚到同一个归并批次。

    首选第一个英文/符号串（SQL性能优化 → SQL、PCB Layout设计 → PCB），
    没有英文就取前两个汉字（仿真验证 → 仿真）。
    """
    m = re.search(r"[A-Za-z][A-Za-z.#+/0-9]{1,}", name)
    if m:
        return m.group(0).upper()
    m = re.match(rf"[{_CJK}]{{2}}", name)
    return m.group(0) if m else name[:2]


def chunk_by_core_token(names: list[str], batch: int) -> list[list[str]]:
    """按核心词聚类后装箱 —— 不这样做，跨批的同族技能永远归并不到一起。

    实测简单按字典序切批时，`SQL性能优化` 与 `SQL性能分析` 落在不同批次，
    LLM 看不到对方，重复就留了下来（641 → 565 仅压缩 12%）。
    """
    buckets: dict[str, list[str]] = {}
    for n in names:
        buckets.setdefault(core_token(n), []).append(n)
    # 大族优先装箱，避免被切散
    ordered = sorted(buckets.values(), key=len, reverse=True)
    out: list[list[str]] = []
    cur: list[str] = []
    for grp in ordered:
        if len(grp) >= batch:  # 单族就超一批，独立成批
            out.append(grp)
            continue
        if len(cur) + len(grp) > batch:
            out.append(cur)
            cur = []
        cur.extend(grp)
    if cur:
        out.append(cur)
    return out


def parse_json(text: str) -> dict | None:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i : j + 1])
    except json.JSONDecodeError:
        return None


def normalize_skills(d: dict) -> dict | None:
    skills = d.get("skills")
    if not isinstance(skills, list) or not skills:
        return None
    out = []
    for s in skills:
        if not isinstance(s, dict):
            continue
        name = canon_skill(str(s.get("name") or ""))
        if not name or len(name) > 30:
            continue
        try:
            lv = max(1, min(5, int(s.get("level") or 3)))
            w = float(s.get("weight") or 0)
        except (TypeError, ValueError):
            continue
        if w <= 0:
            continue
        cat = str(s.get("category") or "").strip()
        out.append({"name": name, "level": lv, "weight": w,
                    "category": cat if cat in CATEGORIES else "通用素养"})
    if not out:
        return None
    merged: dict[str, dict] = {}
    for s in out:
        prev = merged.get(s["name"])
        if prev:
            prev["weight"] += s["weight"]
            prev["level"] = max(prev["level"], s["level"])
        else:
            merged[s["name"]] = s
    out = list(merged.values())
    total = sum(s["weight"] for s in out)
    for s in out:
        s["weight"] = round(s["weight"] / total, 4)
    try:
        jl = max(1, min(5, int(d.get("job_level") or 2)))
    except (TypeError, ValueError):
        jl = 2
    return {"job_level": jl, "job_family": str(d.get("job_family") or "").strip(), "skills": out}


def boss_occupations(conn, l1: str, limit: int | None = None) -> list[dict]:
    out = []
    for nid, name, attrs in conn.execute(
        "SELECT id, name, attrs FROM nodes WHERE type='occupation' AND source_system='BOSS'"
    ):
        try:
            a = json.loads(attrs or "{}")
        except Exception:
            a = {}
        if l1 in ("all", "*", "") or a.get("boss_l1") == l1:
            out.append({"id": nid, "name": name, "l1": a.get("boss_l1"), "l2": a.get("boss_l2")})
    out.sort(key=lambda x: x["name"])
    return out[:limit] if limit else out


def raw_path(l1: str) -> Path:
    return STAGING / f"boss_skill_raw_{slug(l1)}.json"


def canon_path(l1: str) -> Path:
    return STAGING / f"boss_skill_canon_{slug(l1)}.json"


# ────────────────────────── stage: collect ──────────────────────────
def stage_collect(*, l1: str, limit: int | None, sleep: float, resume: bool) -> dict:
    from backend.agent.llm import invoke_fast, llm_ready

    if not llm_ready():
        return {"error": "LLM 网关未配置（llm_ready()=False）"}

    STAGING.mkdir(parents=True, exist_ok=True)
    out_path = raw_path(l1)
    done: dict[str, dict] = {}
    if resume and out_path.exists():
        done = {r["id"]: r for r in json.loads(out_path.read_text(encoding="utf-8"))["items"]}

    conn = connect()
    try:
        occs = boss_occupations(conn, l1, limit)
    finally:
        conn.close()

    todo = [o for o in occs if o["id"] not in done]
    failed = []
    for i, o in enumerate(todo, 1):
        msg = [("system", "你是职业技能图谱专家，只输出 JSON。"),
               ("user", PROMPT_SKILL.format(name=o["name"], l1=o["l1"], l2=o["l2"],
                                            categories="、".join(CATEGORIES)))]
        try:
            parsed = normalize_skills(parse_json(invoke_fast(msg, max_tokens=1200)) or {})
        except Exception as e:
            failed.append({"name": o["name"], "error": str(e)[:120]})
            continue
        if not parsed:
            failed.append({"name": o["name"], "error": "JSON 解析/校验失败"})
            continue
        done[o["id"]] = {**o, **parsed}
        if i % 20 == 0:  # 边跑边存，中断不丢
            out_path.write_text(
                json.dumps({"l1": l1, "items": list(done.values())}, ensure_ascii=False, indent=2),
                encoding="utf-8")
        if sleep:
            time.sleep(sleep)

    out_path.write_text(
        json.dumps({"l1": l1, "generated_at": utc_now_iso(), "items": list(done.values())},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    names = {s["name"] for r in done.values() for s in r["skills"]}
    return {"stage": "collect", "l1": l1, "occupations": len(occs), "collected": len(done),
            "llm_failed": failed, "distinct_skill_names": len(names), "raw_path": str(out_path)}


# ────────────────────────── stage: merge ──────────────────────────
def stage_merge(*, l1: str, batch: int, sleep: float) -> dict:
    from backend.agent.llm import invoke_fast, llm_ready

    if not llm_ready():
        return {"error": "LLM 网关未配置"}
    src = raw_path(l1)
    if not src.exists():
        return {"error": f"缺 {src}，先跑 --stage collect"}

    data = json.loads(src.read_text(encoding="utf-8"))
    names = sorted({s["name"] for r in data["items"] for s in r["skills"]})
    chunks = chunk_by_core_token(names, batch)

    # 分批送 LLM 归并：一次塞几百个名字会超上下文且质量下降
    mapping: dict[str, str] = {}
    groups_all = []
    failed = []
    for i, chunk in enumerate(chunks):
        msg = [("system", "你是技能词表专家，只输出 JSON。"),
               ("user", PROMPT_MERGE.format(names="\n".join("- " + n for n in chunk)))]
        try:
            got = parse_json(invoke_fast(msg, max_tokens=3000)) or {}
        except Exception as e:
            failed.append({"batch": i, "error": str(e)[:120]})
            continue
        for g in got.get("groups") or []:
            canon = canon_skill(str(g.get("canon") or ""))
            if not canon:
                continue
            groups_all.append(g)
            mapping[canon] = canon
            for al in g.get("aliases") or []:
                a = canon_skill(str(al))
                if a and a in chunk:
                    mapping[a] = canon
        if sleep:
            time.sleep(sleep)

    # LLM 漏掉的名字保持自身
    for n in names:
        mapping.setdefault(n, n)

    canon_set = sorted(set(mapping.values()))
    out = canon_path(l1)
    out.write_text(json.dumps({
        "l1": l1, "generated_at": utc_now_iso(),
        "input_names": len(names), "canon_skills": len(canon_set),
        "reduction": f"{len(names)} → {len(canon_set)}",
        "mapping": mapping, "groups": groups_all,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    multi = {c: sorted(a for a, cc in mapping.items() if cc == c and a != c) for c in canon_set}
    multi = {k: v for k, v in multi.items() if v}
    return {"stage": "merge", "l1": l1, "input_names": len(names),
            "canon_skills": len(canon_set), "merged_groups": len(multi),
            "llm_failed": failed, "sample_merges": dict(list(multi.items())[:12]),
            "canon_path": str(out)}


# ────────────────────────── stage: apply ──────────────────────────
def existing_skill_keys(conn) -> dict[str, dict[int, str]]:
    out: dict[str, dict[int, str]] = {}
    for nid, name, attrs in conn.execute(
        "SELECT id, name, attrs FROM nodes WHERE type='skill_level'"
    ):
        try:
            a = json.loads(attrs or "{}")
        except Exception:
            a = {}
        key = a.get("skill_key") or a.get("skill_name") or str(name or "").split(" · ")[0]
        try:
            lv = int(a.get("level"))
        except (TypeError, ValueError):
            continue
        if key:
            out.setdefault(canon_skill(str(key)), {})[lv] = nid
    return out


def stage_apply(*, l1: str, dry_run: bool) -> dict:
    src, cmap = raw_path(l1), canon_path(l1)
    if not src.exists():
        return {"error": f"缺 {src}，先跑 --stage collect"}
    if not cmap.exists():
        return {"error": f"缺 {cmap}，先跑 --stage merge"}

    data = json.loads(src.read_text(encoding="utf-8"))
    mapping = json.loads(cmap.read_text(encoding="utf-8"))["mapping"]
    fetched_at = utc_now_iso()

    conn = connect()
    try:
        have = existing_skill_keys(conn)
        new_nodes: dict[str, dict] = {}
        edges: list[dict] = []
        reused = 0

        for r in data["items"]:
            # 归并后同一岗位可能出现重复技能，合并权重取高档
            per: dict[str, dict] = {}
            for s in r["skills"]:
                key = mapping.get(s["name"], s["name"])
                p = per.get(key)
                if p:
                    p["weight"] += s["weight"]
                    p["level"] = max(p["level"], s["level"])
                else:
                    per[key] = {"level": s["level"], "weight": s["weight"], "category": s["category"]}
            tot = sum(v["weight"] for v in per.values()) or 1.0
            for key, v in per.items():
                v["weight"] = round(v["weight"] / tot, 4)

            for key, v in per.items():
                lv = v["level"]
                sid = (have.get(key) or {}).get(lv)
                if sid:
                    reused += 1
                else:
                    for L in LEVELS:
                        nid = make_node_id(REGION, "skill_level", SRC_SKILL, f"{slug(key)}|L{L}")
                        new_nodes.setdefault(nid, {
                            "id": nid, "region": REGION, "type": "skill_level",
                            "name": f"{key} · L{L}", "name_en": None, "name_zh": f"{key} · L{L}",
                            "aliases": None,
                            "description": f"{key}（产品等级 L{L}）—— 由市场化岗位技能构成推断新增",
                            "attrs": json.dumps({"skill_key": key, "level": L,
                                                 "category": v["category"]}, ensure_ascii=False),
                            "source_system": SRC_SKILL, "source_id": f"{slug(key)}|L{L}",
                            "source_url": SOURCE_URL, "license": LICENSE,
                            "fetched_at": fetched_at, "confidence": "ai_inferred",
                        })
                        have.setdefault(key, {})[L] = nid
                    sid = have[key][lv]

                edges.append({
                    "id": make_edge_id(r["id"], "requires", sid),
                    "src_id": r["id"], "dst_id": sid, "rel_type": "requires",
                    "region": REGION, "weight": v["weight"],
                    "evidence": (f"LLM 依岗位「{r['name']}」（{r['l1']}>{r['l2']}）推断核心技能"
                                 f"「{key}」要求 L{lv}，权重 {v['weight']}（同岗位 Σ=1，技能名经词表归并）"),
                    "attrs": json.dumps({"match_method": "llm_skill_composition_merged",
                                         "skill_key": key, "required_level": lv,
                                         "category": v["category"]}, ensure_ascii=False),
                    "source_system": SRC_LINK, "source_id": f"{r['id']}->{sid}",
                    "source_url": SOURCE_URL, "license": LICENSE,
                    "fetched_at": fetched_at, "confidence": "ai_inferred",
                })

        # 回写岗位 job_level / job_family，供晋升视图与前端排序
        patches = []
        for r in data["items"]:
            row = conn.execute("SELECT attrs FROM nodes WHERE id=?", (r["id"],)).fetchone()
            try:
                a = json.loads((row[0] if row else "") or "{}")
            except Exception:
                a = {}
            a.update({"level": r["job_level"], "job_family": r["job_family"]})
            patches.append((json.dumps(a, ensure_ascii=False), r["id"]))

        rep = {"stage": "apply", "l1": l1, "dry_run": dry_run,
               "occupations": len(data["items"]),
               "requires_edges": len(edges),
               "new_skill_nodes": len(new_nodes),
               "new_logical_skills": len(new_nodes) // len(LEVELS) if new_nodes else 0,
               "reused_existing_skill_levels": reused}

        if not dry_run:
            if new_nodes:
                rep["nodes_upserted"] = upsert_nodes(conn, list(new_nodes.values()))
            rep["edges_upserted"] = upsert_edges(conn, edges)
            for attrs_json, nid in patches:
                conn.execute("UPDATE nodes SET attrs=? WHERE id=?", (attrs_json, nid))
            conn.commit()
            rep["occupations_patched"] = len(patches)
    finally:
        conn.close()
    return rep


# ────────────────────────── stage: advance ──────────────────────────
def stage_advance(*, l1: str, dry_run: bool, batch: int, sleep: float) -> dict:
    from backend.agent.llm import invoke_fast, llm_ready

    if not llm_ready():
        return {"error": "LLM 网关未配置"}
    src = raw_path(l1)
    if not src.exists():
        return {"error": f"缺 {src}，先跑 --stage collect"}

    data = json.loads(src.read_text(encoding="utf-8"))
    items = data["items"]
    by_name = {r["name"]: r for r in items}
    fetched_at = utc_now_iso()

    paths, failed = [], []
    for i in range(0, len(items), batch):
        chunk = items[i : i + batch]
        listing = "\n".join(
            f"- {r['name']} | {r['job_family'] or r['l2']} | L{r['job_level']}" for r in chunk
        )
        msg = [("system", "你是职业发展路径专家，只输出 JSON。"),
               ("user", PROMPT_ADVANCE.format(jobs=listing))]
        try:
            # max_tokens 要够大：一批 40 个岗位可产出几十条含 reason 的路径，
            # 给 2000 时 117 个岗位一次全塞会被截断，JSON 解析失败静默返回 0 条
            raw = invoke_fast(msg, max_tokens=6000)
            got = parse_json(raw) or {}
            if not got.get("paths"):
                # 整体解析失败多半是尾部被截断，按对象级抢救
                rescued = salvage_paths(raw)
                if rescued:
                    got = {"paths": rescued}
                    failed.append({"batch": i // batch, "note": f"输出截断，抢救出 {len(rescued)} 条"})
                else:
                    failed.append({"batch": i // batch, "error": "无 paths 且抢救失败",
                                   "raw_tail": (raw or "")[-120:]})
        except Exception as e:
            failed.append({"batch": i // batch, "error": str(e)[:120]})
            continue
        paths.extend(got.get("paths") or [])
        if sleep:
            time.sleep(sleep)

    edges, dropped = [], []
    seen = set()
    for p in paths:
        a, b = by_name.get(str(p.get("from") or "").strip()), by_name.get(str(p.get("to") or "").strip())
        if not a or not b or a["id"] == b["id"]:
            dropped.append({"path": p, "why": "岗位名不在本批次"})
            continue
        if a["job_level"] >= b["job_level"]:
            dropped.append({"path": p, "why": f"职级未递进 L{a['job_level']}→L{b['job_level']}"})
            continue
        key = (a["id"], b["id"])
        if key in seen:
            continue
        seen.add(key)
        edges.append({
            "id": make_edge_id(a["id"], "advances_to", b["id"]),
            "src_id": a["id"], "dst_id": b["id"], "rel_type": "advances_to",
            "region": REGION, "weight": 1.0,
            "evidence": (f"{a['name']}(L{a['job_level']}/{a['job_family']}) → "
                         f"{b['name']}(L{b['job_level']}/{b['job_family']})："
                         f"{str(p.get('reason') or '跨族职级递进')[:80]}"),
            "attrs": json.dumps({"match_method": "llm_cross_family_advance",
                                 "from_family": a["job_family"], "to_family": b["job_family"],
                                 "from_level": a["job_level"], "to_level": b["job_level"]},
                                ensure_ascii=False),
            "source_system": SRC_LINK, "source_id": f"{a['id']}->{b['id']}",
            "source_url": SOURCE_URL, "license": LICENSE,
            "fetched_at": fetched_at, "confidence": "ai_inferred",
        })

    rep = {"stage": "advance", "l1": l1, "dry_run": dry_run,
           "llm_paths_proposed": len(paths), "edges_valid": len(edges),
           "dropped": len(dropped), "dropped_sample": dropped[:6],
           "llm_failed": failed,
           "sample": [e["evidence"] for e in edges[:8]]}
    if not dry_run and edges:
        conn = connect()
        try:
            rep["edges_upserted"] = upsert_edges(conn, edges)
            conn.commit()
        finally:
            conn.close()
    return rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=("collect", "merge", "apply", "advance"))
    ap.add_argument("--l1", default="技术", help="BOSS 一级门类，all=全部")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.2)
    ap.add_argument("--batch", type=int, default=80, help="merge/advance 的分批大小")
    ap.add_argument("--no-resume", action="store_true", help="collect 不续跑，从头来")
    args = ap.parse_args()

    if args.stage == "collect":
        rep = stage_collect(l1=args.l1, limit=args.limit, sleep=args.sleep, resume=not args.no_resume)
    elif args.stage == "merge":
        rep = stage_merge(l1=args.l1, batch=args.batch, sleep=args.sleep)
    elif args.stage == "apply":
        rep = stage_apply(l1=args.l1, dry_run=args.dry_run)
    else:
        rep = stage_advance(l1=args.l1, dry_run=args.dry_run, batch=args.batch, sleep=args.sleep)

    REPORTS.mkdir(parents=True, exist_ok=True)
    tag = "dryrun" if args.dry_run else "applied"
    (REPORTS / f"boss_skill_{args.stage}_{slug(args.l1)}_{tag}.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=2)[:2500])


if __name__ == "__main__":
    main()
