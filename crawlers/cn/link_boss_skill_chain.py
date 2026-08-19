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
from backend.kg.pg_store.skill_taxonomy import FALLBACK_CODE, SKILL_CATEGORIES, to_code

REGION = "CN"
SRC_SKILL = "LLM_CN"
SRC_LINK = "LINK_CN_AI"
SOURCE_URL = "llm://voced-kg/boss-skill-chain"
LICENSE = "LLM 推断，需人工抽检"
LEVELS = (1, 2, 3, 4, 5)

# 给 LLM 看的候选项用**中文名**（模型对 TECH/OPERATE 这种 code 的语义把握不如中文），
# 落库前一律 `to_code()` 转成分类 code —— `kg_node.category` 存的是 code。
# 候选项直接取自字典表真源，不再手写一份：手写的那份曾与库里的国标口径完全不重合，
# 导致 LLM 产出的分类在页面上一个都对不上。
CATEGORIES = [c["name"] for c in SKILL_CATEGORIES if not c["code"] == FALLBACK_CODE]

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

PROMPT_ADVANCE = """下面是同一领域内的招聘岗位及其职级判定。请**逐个岗位**列出它的向上发展方向。

岗位列表（格式：岗位名 | 岗位族 | 职级1-5）：
{jobs}

做法：对列表里的**每一个**岗位，都想一遍「干这行的人往上走，能走到列表中的哪些岗位」，
尽量覆盖下面三类方向，有几条写几条（某类没有就跳过，不要硬凑）：
  A 本方向纵深 —— 同领域做深做精（Java → 架构师）
  B 转管理    —— 带团队、管项目（Java → 技术经理 → 技术总监）
  C 跨方向转型 —— 相邻领域的向上流动（测试工程师 → 测试开发）

规则：
1. from 与 to 都必须是上面列表里**原样出现**的岗位名，不要自造
2. from 的职级必须**严格低于** to 的职级；职级相同的一律不要输出
3. 平行的技术方向**不是**晋升（Java → PHP 不算，Java → Python 不算）
4. 同一个岗位通常有 2-4 条向上路径，不要只给一条
5. reason 只写最终结论，**不要写推理过程**，一句话即可

只输出 JSON，不要任何解释文字：
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
        # LLM 回的是中文名，转成 code 再落库；认不出的落兜底而不是硬塞「通用素养」
        cat = to_code(str(s.get("category") or "").strip())
        out.append({"name": name, "level": lv, "weight": w,
                    "category": cat})
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
            # 先清掉这些岗位已有的 requires 边，再整体重建。
            #
            # 必须这么做的原因：edge id 是 (src, rel, dst)，而 dst 是**具体等级节点**，
            # 同一技能的 L2 与 L3 是两个不同节点 → id 不同 → upsert 不会覆盖。
            # 重跑 apply（改了归并规则、档位判定变化）时旧边会原样留下，于是同一岗位
            # 同一技能出现多档（.NET Core开发 同时挂 L2 和 L3）。
            # 后果：列表页按 skill_key 聚合算 7 项，详情页按边算 10 项，两处对不上；
            # 而且「高档天生覆盖低档」，多档本身就是无意义的。
            occ_ids = [r["id"] for r in data["items"]]
            if occ_ids:
                qs = ",".join("?" * len(occ_ids))
                rep["stale_requires_removed"] = conn.execute(
                    f"DELETE FROM edges WHERE rel_type='requires' AND src_id IN ({qs})",
                    occ_ids,
                ).rowcount
            rep["edges_upserted"] = upsert_edges(conn, edges)
            for attrs_json, nid in patches:
                conn.execute("UPDATE nodes SET attrs=? WHERE id=?", (attrs_json, nid))
            conn.commit()
            rep["occupations_patched"] = len(patches)
    finally:
        conn.close()
    return rep


def _load_external_occupations(*, exclude: set[str]) -> dict[str, dict]:
    """本批次之外、库里已有的 BOSS 岗位，按岗位名索引。

    只取 `attrs.level` 有值的 —— 没有职级就过不了「严格递进」那道校验，
    拿进来也是白拿，反而让重名匹配变松。
    """
    out: dict[str, dict] = {}
    conn = connect()
    try:
        for r in conn.execute(
            "SELECT id, name, attrs FROM nodes "
            "WHERE type='occupation' AND source_system='BOSS'"
        ).fetchall():
            name = r["name"]
            if not name or name in exclude or name in out:
                continue
            try:
                a = json.loads(r["attrs"] or "{}")
            except Exception:
                continue
            try:
                lv = int(a.get("level"))
            except (TypeError, ValueError):
                continue
            out[name] = {"id": r["id"], "name": name, "job_level": lv,
                         "job_family": a.get("job_family"), "l2": a.get("boss_l2")}
    finally:
        conn.close()
    return out


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

    # 跨门类的上游岗位：LLM 会提出「Java → 项目总监」这类路径，而「项目总监」
    # 可能落在别的 BOSS 门类下，不在本次 raw 里。只按本批次匹配的话，跨门类晋升
    # 永远建不起来 —— 而「转管理」恰恰是最常见的一类向上路径。
    # 所以批次外的名字回库里查一次，查得到就认。
    ext = _load_external_occupations(exclude=set(by_name))

    paths, failed = [], []
    for i in range(0, len(items), batch):
        chunk = items[i : i + batch]
        listing = "\n".join(
            f"- {r['name']} | {r['job_family'] or r['l2']} | L{r['job_level']}" for r in chunk
        )
        msg = [("system", "你是职业发展路径专家，只输出 JSON。"),
               ("user", PROMPT_ADVANCE.format(jobs=listing))]
        try:
            # max_tokens 要够大：提示词改成「逐岗位枚举 2-4 条」后产出量翻了几倍，
            # 一批 40 个岗位就能出 100+ 条含 reason 的路径。给小了会被截断，
            # JSON 解析失败**静默返回 0 条**（这也是为什么 batch 默认降到 40）。
            raw = invoke_fast(msg, max_tokens=10000)
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
    ext_used = 0
    for p in paths:
        fn, tn = str(p.get("from") or "").strip(), str(p.get("to") or "").strip()
        a = by_name.get(fn)
        # 起点必须是本门类的岗位（否则跑每个门类都会重复产出同一批边）；
        # 终点允许是库里任何 BOSS 岗位，这样才接得上跨门类的管理序列
        b = by_name.get(tn) or ext.get(tn)
        if b is not None and tn not in by_name:
            ext_used += 1
        if not a or not b or a["id"] == b["id"]:
            dropped.append({"path": p, "why": "岗位名不在本批次，且库里也没有"})
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
           "cross_l1_targets": ext_used, "external_pool": len(ext),
           "dropped": len(dropped), "dropped_sample": dropped[:6],
           "llm_failed": failed,
           "sample": [e["evidence"] for e in edges[:8]]}
    if not dry_run and edges:
        conn = connect()
        try:
            # 重建前先删本门类岗位已有的 advances_to —— 与 stage_apply 对 requires
            # 的做法一致。不删的话重跑是纯增量：这一轮没再提出的旧路径会永远留着，
            # 时间一长同一个岗位挂着几轮判定的叠加（库里已经出现过挂 15 条的），
            # 没人分得清哪条是哪次跑出来的。删的只是**起点在本门类**的边，
            # 别的门类指过来的不动。
            src_ids = sorted({e["src_id"] for e in edges} | {r["id"] for r in items})
            qs = ",".join("?" * len(src_ids))
            rep["stale_advances_removed"] = conn.execute(
                f"DELETE FROM edges WHERE rel_type='advances_to' AND src_id IN ({qs})",
                src_ids,
            ).rowcount
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
    ap.add_argument("--batch", type=int, default=40,
                    help="merge/advance 的分批大小。advance 逐岗位枚举，批次大了会超 max_tokens")
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
