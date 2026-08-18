"""为 LLM 生成的技能补齐**各档位的行为锚点描述**。

问题
----
`link_boss_skill_chain --stage apply` 建技能节点时，5 档描述只是模板套 L 编号：

    Java核心编程 · L1   Java核心编程（产品等级 L1）—— 由市场化岗位技能构成推断新增
    Java核心编程 · L5   Java核心编程（产品等级 L5）—— 由市场化岗位技能构成推断新增

各档完全一样，学员看不出 L3 和 L5 差在哪，测评也没法出分档题目。
（顺带说明：MOHRSS 国标技能的描述也是元数据拼接
 `轨道交通调度员①（4-02-01-06）· 施工检修管理 · 三级/高级工 · 权重20%`，
 同样不是行为锚点，但那批有国标出处，本脚本不动它，只补 source_system=LLM_CN 的。）

做法
----
每档描述 = **通用行为锚点**（来自 `skill_level_meta`，唯一真源）+ **该技能的具体内容**。
通用锚点保证 5 档的语义梯度一致、可比较；具体内容让学员知道这一档到底要会什么。

    L1 了解  学过或听过相关内容，但没有独立实操过
    L3 熟练  能独立完成，并处理常见异常情况
    L5 专家  能制定标准或攻关行业级难题，是团队内该领域的权威

⚠ 档位名称/基准分/通用锚点**只能**从 `kg/pg_store/skill_level_meta.py` 读，
   不在本脚本硬编码（CLAUDE.md 明确约定）。

一次调用处理多个技能（默认 5 个 = 25 条描述），控制调用数。
LLM 结果落盘 `data/staging/skill_level_desc.json`，可续跑、可只重跑落库。

用法::

    python -m crawlers.cn.backfill_llm_skill_level_desc --stage gen  --limit 20
    python -m crawlers.cn.backfill_llm_skill_level_desc --stage gen
    python -m crawlers.cn.backfill_llm_skill_level_desc --stage apply --dry-run
    python -m crawlers.cn.backfill_llm_skill_level_desc --stage apply
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

from backend.kg.graph_store import connect
from backend.kg.paths import REPORTS, STAGING
from backend.kg.provenance import utc_now_iso

OUT = STAGING / "skill_level_desc.json"
SRC_FILTER = "LLM_CN"

PROMPT = """为下列技能分别写出 5 个掌握档位的**行为锚点**描述。

档位的通用含义（必须遵守这个梯度，不要自己改档位语义）：
{anchors}

要求：
1. 每档 20-45 字，说清**这一档能独立做到什么**，不要写"熟悉/了解 XX"这种空话
2. 5 档必须有清晰递进：L1 只会看/跟着做 → L3 能独立交付 → L5 能定标准/攻难题
3. 结合该技能的**具体技术内容**（工具、场景、产出物），不要写成通用套话
4. 不要在描述里重复技能名本身

技能列表：
{skills}

只输出 JSON：
{{"items": [{{"skill": "技能名", "levels": {{"1": "...", "2": "...", "3": "...", "4": "...", "5": "..."}}}}]}}"""


def parse_json(text: str) -> dict | None:
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i : j + 1])
    except json.JSONDecodeError:
        return None


def salvage_items(text: str) -> list[dict]:
    """截断抢救：捞出已完整输出的 skill+levels 块。"""
    out = []
    for m in re.finditer(
        r'\{\s*"skill"\s*:\s*"([^"]+)"\s*,\s*"levels"\s*:\s*\{([^}]*)\}\s*\}', text or ""
    ):
        levels = dict(re.findall(r'"([1-5])"\s*:\s*"([^"]*)"', m.group(2)))
        if len(levels) == 5:
            out.append({"skill": m.group(1), "levels": levels})
    return out


def target_skills(conn) -> dict[str, dict[int, str]]:
    """待补技能：source_system=LLM_CN 且各档描述雷同的。"""
    agg: dict[str, dict[int, str]] = {}
    for nid, name, attrs in conn.execute(
        "SELECT id, name, attrs FROM nodes WHERE type='skill_level' AND source_system=?",
        (SRC_FILTER,),
    ):
        try:
            a = json.loads(attrs or "{}")
        except Exception:
            a = {}
        key = a.get("skill_key") or str(name or "").split(" · ")[0]
        try:
            lv = int(a.get("level"))
        except (TypeError, ValueError):
            continue
        if key:
            agg.setdefault(key, {})[lv] = nid
    return agg


def stage_gen(*, limit: int | None, batch: int, sleep: float) -> dict:
    from backend.agent.llm import invoke_fast, llm_ready
    from backend.kg.pg_store.skill_level_meta import SKILL_LEVEL_META

    if not llm_ready():
        return {"error": "LLM 网关未配置"}

    anchors = "\n".join(
        f"- L{m['level']} {m['name']}（基准分 {m['base_score']}）：{m['behavior']}"
        for m in SKILL_LEVEL_META
    )

    conn = connect()
    try:
        agg = target_skills(conn)
    finally:
        conn.close()

    keys = sorted(agg)
    done: dict[str, dict[str, str]] = {}
    if OUT.exists():
        done = json.loads(OUT.read_text(encoding="utf-8")).get("items", {})
    todo = [k for k in keys if k not in done]
    if limit:
        todo = todo[:limit]

    STAGING.mkdir(parents=True, exist_ok=True)
    failed = []
    for i in range(0, len(todo), batch):
        chunk = todo[i : i + batch]
        msg = [("system", "你是职业技能标准编写专家，只输出 JSON。"),
               ("user", PROMPT.format(anchors=anchors,
                                      skills="\n".join("- " + k for k in chunk)))]
        try:
            raw = invoke_fast(msg, max_tokens=4000)
            got = parse_json(raw) or {}
            items = got.get("items") or salvage_items(raw)
        except Exception as e:
            failed.append({"batch": i // batch, "error": str(e)[:120]})
            continue
        for it in items:
            sk = str(it.get("skill") or "").strip()
            lv = it.get("levels") or {}
            if sk in chunk and all(str(n) in lv and lv[str(n)] for n in range(1, 6)):
                done[sk] = {str(n): str(lv[str(n)]).strip() for n in range(1, 6)}
        OUT.write_text(json.dumps({"generated_at": utc_now_iso(), "items": done},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        if sleep:
            time.sleep(sleep)

    return {"stage": "gen", "llm_skills_total": len(keys), "already_done": len(done),
            "requested": len(todo), "llm_failed": failed, "out": str(OUT),
            "sample": {k: done[k] for k in list(done)[:2]}}


def stage_apply(*, dry_run: bool) -> dict:
    if not OUT.exists():
        return {"error": f"缺 {OUT}，先跑 --stage gen"}
    desc = json.loads(OUT.read_text(encoding="utf-8"))["items"]

    from backend.kg.pg_store.skill_level_meta import label_map

    labels = label_map()
    conn = connect()
    try:
        agg = target_skills(conn)
        updates, missing = [], []
        for key, levels in desc.items():
            nodes = agg.get(key)
            if not nodes:
                missing.append(key)
                continue
            for lv, nid in nodes.items():
                text = levels.get(str(lv))
                if not text:
                    continue
                # 描述前缀带档位名，前端不必再拼
                updates.append((f"{labels.get(str(lv), 'L%d' % lv)}：{text}", nid))

        rep = {"stage": "apply", "dry_run": dry_run, "skills_with_desc": len(desc),
               "node_updates": len(updates), "skills_not_in_db": missing[:10]}
        if not dry_run:
            conn.executemany("UPDATE nodes SET description=? WHERE id=?", updates)
            conn.commit()
            rep["updated"] = len(updates)
    finally:
        conn.close()
    return rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=("gen", "apply"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch", type=int, default=5, help="一次请求处理几个技能")
    ap.add_argument("--sleep", type=float, default=0.2)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rep = (stage_gen(limit=args.limit, batch=args.batch, sleep=args.sleep)
           if args.stage == "gen" else stage_apply(dry_run=args.dry_run))
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"skill_level_desc_{args.stage}.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=2)[:1800])


if __name__ == "__main__":
    main()
