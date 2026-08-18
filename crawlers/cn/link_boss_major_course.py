"""BOSS 市场化岗位链路的后两段：专业 prepares_for 岗位、技能 taught_by 课程。

两段的难点完全不同
------------------
**专业段**：候选池有 2281 个 CN 专业，不能整个塞进 LLM 上下文。做法是
「规则预筛 top-N 候选 → LLM 精选 2-5 个」，LLM 只能从候选里挑，
不会凭空造出库里没有的专业名（造了也匹配不上，会被丢弃并计入报告）。

**课程段**：课程库 16790 门几乎全是学历教育课名，现代技术栈覆盖极差 ——
实测 `Spring` / `Docker` / `Kubernetes` 命中 **0 门**。所以分两级：

1. 先按技能名去课程库做包含匹配（命中即 `confidence=derived`，真实课程）
2. 匹配不到的，生成**可点击的检索落地页**（大学 MOOC / 国家智慧教育），
   复用项目既有模式 `seed_learnable_search.py`：`attrs.role=learnable_resource`,
   `match_method=search_landing`。**这是合法检索入口，不是伪造课程详情页**。

用法::

    python -m crawlers.cn.link_boss_major_course --stage major  --l1 技术 --dry-run
    python -m crawlers.cn.link_boss_major_course --stage major  --l1 技术
    python -m crawlers.cn.link_boss_major_course --stage course --l1 技术 --dry-run
    python -m crawlers.cn.link_boss_major_course --stage course --l1 技术
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, upsert_edges, upsert_nodes
from backend.kg.paths import REPORTS, STAGING
from backend.kg.provenance import make_edge_id, make_node_id, utc_now_iso

REGION = "CN"
SRC_LINK = "LINK_CN_AI"
SRC_COURSE = "SEARCH_LANDING_CN"
SOURCE_URL = "llm://voced-kg/boss-major-course"
LICENSE_LLM = "LLM 推断，需人工抽检"
LICENSE_RULE = "规则匹配：技能名 ↔ 课程名"
MOOC_SEARCH = "https://www.icourse163.org/search.htm?search="

PROMPT_MAJOR = """你是职业教育专业设置专家。判断下面哪些专业**真正对口**培养这个岗位。

岗位：{name}（{l1} > {l2}）
岗位核心技能：{skills}

候选专业（只能从这里选，不要自己造）：
{candidates}

规则：
1. 选 2-5 个最对口的，宁少勿滥
2. 必须是该专业毕业生**能直接胜任**该岗位的，不要沾边就选
3. relevance：1.0=核心对口专业，0.7=相关可转入，0.4=沾边
4. name 只写专业名本身，**不要带括号里的学历层次**
   —— 候选写「软件技术（高等职业教育专科）」，你要返回的是「软件技术」

只输出 JSON：
{{"majors": [{{"name": "专业名", "relevance": 1.0, "reason": "一句话依据"}}]}}"""

TECH_HINTS = (
    "计算机", "软件", "信息", "电子", "通信", "网络", "数据", "智能", "自动化",
    "物联网", "集成电路", "微电子", "云计算", "区块链", "机器人", "数字",
    "程序", "系统", "安全", "芯片", "嵌入式", "移动应用", "动漫", "游戏",
)


def slug(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z一-龥]+", "_", s).strip("_")[:40]


def parse_json(text: str) -> dict | None:
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i : j + 1])
    except json.JSONDecodeError:
        return None


def raw_items(l1: str) -> list[dict]:
    p = STAGING / f"boss_skill_raw_{slug(l1)}.json"
    if not p.exists():
        raise SystemExit(f"缺 {p}，先跑 link_boss_skill_chain --stage collect")
    return json.loads(p.read_text(encoding="utf-8"))["items"]


def load_majors(conn) -> list[dict]:
    out = []
    for nid, name, attrs in conn.execute(
        "SELECT id, name, attrs FROM nodes WHERE type='major' AND region='CN'"
    ):
        try:
            a = json.loads(attrs or "{}")
        except Exception:
            a = {}
        out.append({"id": nid, "name": name, "level": a.get("level_zh") or a.get("level"),
                    "category": a.get("category")})
    return out


def prefilter(occ: dict, skills: list[str], majors: list[dict], top: int) -> list[dict]:
    """规则预筛候选专业 —— 2281 个全塞进 LLM 会超上下文且质量下降。

    打分：岗位名/二级方向/技能名 与专业名的**词片重叠**，再加技术类专业底分。
    """
    probe = " ".join([occ["name"], occ.get("l2") or "", " ".join(skills[:5])])
    toks = set(re.findall(r"[A-Za-z][A-Za-z+#./]{1,}|[一-龥]{2,4}", probe))
    scored = []
    for m in majors:
        nm = m["name"]
        s = 0.0
        for t in toks:
            if len(t) >= 2 and t in nm:
                s += 2.0 if len(t) >= 3 else 1.0
        if any(h in nm for h in TECH_HINTS):
            s += 0.5
        if s > 0:
            scored.append((s, m))
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    return [m for _, m in scored[:top]]


def stage_major(*, l1: str, dry_run: bool, top: int, sleep: float, limit: int | None) -> dict:
    from backend.agent.llm import invoke_fast, llm_ready

    if not llm_ready():
        return {"error": "LLM 网关未配置"}
    items = raw_items(l1)[:limit] if limit else raw_items(l1)
    fetched_at = utc_now_iso()

    conn = connect()
    try:
        majors = load_majors(conn)
        by_name = {m["name"]: m for m in majors}
        edges, failed, no_cand, unmatched = [], [], [], []

        for r in items:
            skills = [s["name"] for s in r["skills"]]
            cands = prefilter(r, skills, majors, top)
            if not cands:
                no_cand.append(r["name"])
                continue
            listing = "\n".join(
                f"- {m['name']}（{m.get('level') or '?'}）" for m in cands
            )
            msg = [("system", "你是职业教育专业设置专家，只输出 JSON。"),
                   ("user", PROMPT_MAJOR.format(name=r["name"], l1=r["l1"], l2=r["l2"],
                                                skills="、".join(skills[:6]), candidates=listing))]
            try:
                got = parse_json(invoke_fast(msg, max_tokens=1200)) or {}
            except Exception as e:
                failed.append({"occupation": r["name"], "error": str(e)[:120]})
                continue

            for mj in got.get("majors") or []:
                nm = str(mj.get("name") or "").strip()
                m = by_name.get(nm)
                if not m:
                    # LLM 常把候选行里的学历层次后缀一起抄回来
                    # （「工业软件开发技术（高等职业教育专科）」），剥掉再试
                    bare = re.sub(r"[（(][^）)]*[）)]\s*$", "", nm).strip()
                    m = by_name.get(bare)
                    if m:
                        nm = bare
                if not m:
                    unmatched.append({"occupation": r["name"], "llm_major": nm})
                    continue
                try:
                    rel = max(0.1, min(1.0, float(mj.get("relevance") or 0.7)))
                except (TypeError, ValueError):
                    rel = 0.7
                edges.append({
                    "id": make_edge_id(m["id"], "prepares_for", r["id"]),
                    "src_id": m["id"], "dst_id": r["id"], "rel_type": "prepares_for",
                    "region": REGION, "weight": round(rel, 3),
                    "evidence": (f"专业「{nm}」培养岗位「{r['name']}」（{r['l1']}>{r['l2']}）："
                                 f"{str(mj.get('reason') or 'LLM 判定对口')[:80]}"),
                    "attrs": json.dumps({"match_method": "llm_major_to_market_position",
                                         "relevance": rel, "major_level": m.get("level"),
                                         "candidate_pool": len(cands)}, ensure_ascii=False),
                    "source_system": SRC_LINK, "source_id": f"{m['id']}->{r['id']}",
                    "source_url": SOURCE_URL, "license": LICENSE_LLM,
                    "fetched_at": fetched_at, "confidence": "ai_inferred",
                })
            if sleep:
                time.sleep(sleep)

        edges = list({e["id"]: e for e in edges}.values())
        rep = {"stage": "major", "l1": l1, "dry_run": dry_run, "occupations": len(items),
               "edges": len(edges), "covered_occupations": len({e["dst_id"] for e in edges}),
               "distinct_majors": len({e["src_id"] for e in edges}),
               "no_candidate": no_cand, "llm_name_unmatched": unmatched[:10],
               "llm_failed": failed,
               "sample": [e["evidence"] for e in edges[:8]]}
        if not dry_run and edges:
            rep["edges_upserted"] = upsert_edges(conn, edges)
            conn.commit()
    finally:
        conn.close()
    return rep


def stage_course(*, l1: str, dry_run: bool) -> dict:
    """技能 → 课程：先匹配真实课程，剩下的生成 MOOC 检索落地页。"""
    items = raw_items(l1)
    canon_p = STAGING / f"boss_skill_canon_{slug(l1)}.json"
    mapping = json.loads(canon_p.read_text(encoding="utf-8"))["mapping"] if canon_p.exists() else {}
    skill_keys = sorted({mapping.get(s["name"], s["name"]) for r in items for s in r["skills"]})
    fetched_at = utc_now_iso()

    conn = connect()
    try:
        # 技能 key -> 各档 node_id
        skill_nodes: dict[str, dict[int, str]] = {}
        for nid, name, attrs in conn.execute(
            "SELECT id, name, attrs FROM nodes WHERE type='skill_level'"
        ):
            try:
                a = json.loads(attrs or "{}")
            except Exception:
                a = {}
            k = a.get("skill_key") or str(name or "").split(" · ")[0]
            try:
                lv = int(a.get("level"))
            except (TypeError, ValueError):
                continue
            if k:
                skill_nodes.setdefault(k, {})[lv] = nid

        courses = [(cid, cname) for cid, cname in
                   conn.execute("SELECT id, name FROM nodes WHERE type='course' AND region='CN'")]

        edges, new_nodes = [], {}
        matched_real, landing = 0, 0
        for key in skill_keys:
            nodes = skill_nodes.get(key) or {}
            if not nodes:
                continue
            # 挂在中间档（L3 熟练）上：taught_by 表达"学这个能达到该档"
            anchor = nodes.get(3) or nodes.get(max(nodes))
            core = re.sub(r"(开发|设计|应用|编程|使用|优化|管理|基础|技术)$", "", key) or key
            hits = [(cid, cn) for cid, cn in courses if len(core) >= 2 and core in cn][:3]

            if hits:
                matched_real += 1
                for cid, cn in hits:
                    edges.append({
                        "id": make_edge_id(anchor, "taught_by", cid),
                        "src_id": anchor, "dst_id": cid, "rel_type": "taught_by",
                        "region": REGION, "weight": 0.8,
                        "evidence": f"技能「{key}」与课程「{cn}」名称匹配（核心词「{core}」）",
                        "attrs": json.dumps({"match_method": "skill_course_name_match",
                                             "skill_key": key, "core_token": core},
                                            ensure_ascii=False),
                        "source_system": SRC_LINK, "source_id": f"{anchor}->{cid}",
                        "source_url": SOURCE_URL, "license": LICENSE_RULE,
                        "fetched_at": fetched_at, "confidence": "derived",
                    })
            else:
                # 课程库无覆盖：给合法检索入口，不伪造课程详情页
                landing += 1
                url = MOOC_SEARCH + urllib.parse.quote(key)
                cid = make_node_id(REGION, "course", SRC_COURSE, f"mooc|{slug(key)}")
                new_nodes.setdefault(cid, {
                    "id": cid, "region": REGION, "type": "course",
                    "name": f"{key}（大学MOOC检索）", "name_en": None, "name_zh": f"{key}（大学MOOC检索）",
                    "aliases": None,
                    "description": f"「{key}」在中国大学MOOC的公开检索入口 —— 课程库暂无对应课程时的可学资源",
                    "attrs": json.dumps({"role": "learnable_resource", "playable": True,
                                         "match_method": "search_landing", "skill_key": key},
                                        ensure_ascii=False),
                    "source_system": SRC_COURSE, "source_id": f"mooc|{slug(key)}",
                    "source_url": url, "license": "公开检索入口",
                    "fetched_at": fetched_at, "confidence": "derived",
                })
                edges.append({
                    "id": make_edge_id(anchor, "taught_by", cid),
                    "src_id": anchor, "dst_id": cid, "rel_type": "taught_by",
                    "region": REGION, "weight": 0.5,
                    "evidence": f"技能「{key}」课程库无覆盖，提供大学MOOC检索入口作为可学资源",
                    "attrs": json.dumps({"match_method": "search_landing", "skill_key": key},
                                        ensure_ascii=False),
                    "source_system": SRC_LINK, "source_id": f"{anchor}->{cid}",
                    "source_url": url, "license": "公开检索入口",
                    "fetched_at": fetched_at, "confidence": "derived",
                })

        edges = list({e["id"]: e for e in edges}.values())
        rep = {"stage": "course", "l1": l1, "dry_run": dry_run,
               "skills_total": len(skill_keys),
               "matched_real_course": matched_real, "search_landing": landing,
               "taught_by_edges": len(edges), "new_course_nodes": len(new_nodes),
               "sample": [e["evidence"] for e in edges[:6]]}
        if not dry_run:
            if new_nodes:
                rep["nodes_upserted"] = upsert_nodes(conn, list(new_nodes.values()))
            rep["edges_upserted"] = upsert_edges(conn, edges)
            conn.commit()
    finally:
        conn.close()
    return rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=("major", "course"))
    ap.add_argument("--l1", default="技术")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--top", type=int, default=25, help="major 预筛候选数")
    ap.add_argument("--sleep", type=float, default=0.2)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    if args.stage == "major":
        rep = stage_major(l1=args.l1, dry_run=args.dry_run, top=args.top,
                          sleep=args.sleep, limit=args.limit)
    else:
        rep = stage_course(l1=args.l1, dry_run=args.dry_run)

    REPORTS.mkdir(parents=True, exist_ok=True)
    tag = "dryrun" if args.dry_run else "applied"
    (REPORTS / f"boss_{args.stage}_{slug(args.l1)}_{tag}.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=2)[:2000])


if __name__ == "__main__":
    main()
