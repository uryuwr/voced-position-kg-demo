"""补齐技能残缺的岗位，并让权重归一。

问题
----
77 个国标岗位的 requires 权重和 < 1，根因是 **PDF 解析没抓全**：国标原文一个等级
有若干技能项，采集只捞到零星几条。极端案例：

    眼镜定配工      只有 1 项「培训与管理」    Σw=0.05
    印制电路制作工   只有 1 项「培训——————」   Σw=0.06   ← 技能名是表格线
    全媒体运营师    只有 1 项「内容创作」      Σw=0.40

这类岗位的技能构成是错的，匹配度、学习路径都算不准。直接按 Σw 归一化会**掩盖问题**
（1 项技能归一后变成 100%，看起来完全正常），所以先补齐再归一。

补齐方式
--------
用 LLM 生成该岗位完整的技能构成（保留已采到的项，权重整数百分比 Σ=100），
不限于国标原文——国标 PDF 本身就没解析全，只盯它补不回来。
产出标 `confidence='llm_derived'`、`source_url='llm://voced-kg/skill-fill'`，
与官方采集的数据区分开，便于日后人工复核或被真实采集覆盖。

档位分配沿用 `relevel_occupation_requires.py` 的规则（学生端基准 L2 + 按权重排名
浮动），保持两批数据口径一致。

技能节点为岗位专属
------------------
库内 8889 个国标技能节点的 attrs 都绑了 `occupation_code`，只有 249/2520 被多个
岗位共用——所以技能节点是**岗位专属**的，不能挪用别的岗位的同名节点，否则
attrs 里的岗位代码与实际引用方不一致。缺档位就为本岗位新建。

用法::
    python -X utf8 scripts/fill_incomplete_occupation_skills.py --dry-run
    python -X utf8 scripts/fill_incomplete_occupation_skills.py --gen      # 只跑 LLM，存缓存
    python -X utf8 scripts/fill_incomplete_occupation_skills.py --apply    # 写库
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SQLITE = ROOT / "data" / "graph" / "kg.sqlite"
CACHE = ROOT / "reports" / "skill_fill_llm.json"

from backend.kg.pg_store.client import connect as pg_connect  # noqa: E402
from backend.kg.provenance import make_edge_id, make_node_id  # noqa: E402

REGION = "CN"
SOURCE_SYSTEM = "MOHRSS_CN"
LICENSE = "MOHRSS-public"
FILL_URL = "llm://voced-kg/skill-fill"
FILL_CONF = "llm_derived"
BASE_LEVEL = 2
TOP_FRAC = BOT_FRAC = 0.25
QUANT = 4

_CJK = r"一-鿿"

PROMPT = """你是职业技能标准的编制专家。下面这个岗位的技能构成在采集时残缺了，请补全。

岗位：{occ}
已采到的技能项（务必保留，不要改名）：{have}

要求：
1. 输出该岗位**完整**的技能构成，总共 4–7 项，含已采到的那些。
2. 技能项要用国家职业技能标准的措辞风格：4–10 个汉字的动宾或偏正短语，
   如「设备维护与保养」「质量检验」「作业准备」。不要写成一句话，不要带标点。
3. 每项给权重（整数百分比），全部加起来**正好 100**。核心技能权重高。
4. 只输出 JSON，不要解释：
{{"skills":[{{"name":"技能名","weight_pct":30}}]}}"""


def canon(name: str) -> str:
    """技能名规范化：与 crawlers/cn/link_boss_skill_chain.canon_skill 同口径。"""
    s = (name or "").replace("　", " ").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(rf"(?<=[{_CJK}])\s+(?=[^{_CJK}])", "", s)
    s = re.sub(rf"(?<=[^{_CJK}])\s+(?=[{_CJK}])", "", s)
    return s.strip()


def parse_json(text: str) -> dict | None:
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i : j + 1])
    except json.JSONDecodeError:
        return None


DIRTY = re.compile(r"[—_]{3,}|^__e2e")


def targets_by_weight(items: list[tuple[str, float]]) -> list[tuple[str, float, int]]:
    """按权重排名定档：前 25% → L3，后 25% → L1，其余基准档。同权重必同档。

    规则与 relevel_occupation_requires.plan_targets 一致；组不可拆，配额按
    「加进来不超额」判断，避免一个大并列组把配额撑爆。
    """
    groups: dict[float, list[tuple[str, float]]] = defaultdict(list)
    for nm, w in items:
        groups[round(w, QUANT)].append((nm, w))
    ordered = sorted(groups, reverse=True)
    n = len(items)
    top_n, bot_n = max(1, round(n * TOP_FRAC)), max(1, round(n * BOT_FRAC))
    hi, lo, acc = set(), set(), 0
    for g in ordered:
        if acc + len(groups[g]) > top_n:
            break
        hi.add(g)
        acc += len(groups[g])
    acc = 0
    for g in reversed(ordered):
        if g in hi or acc + len(groups[g]) > bot_n:
            break
        lo.add(g)
        acc += len(groups[g])
    out = []
    for g in ordered:
        lv = 3 if g in hi else (1 if g in lo else BASE_LEVEL)
        for nm, w in groups[g]:
            out.append((nm, w, lv))
    return out


def load_incomplete() -> list[dict[str, Any]]:
    with pg_connect() as c:
        rows = c.execute("""
            WITH s AS (
              SELECT e.src_id, o.name, count(*) c, sum(e.weight) w
              FROM kg_edge e
              JOIN kg_node o ON o.id=e.src_id AND o.type='occupation'
                            AND o.source_system=%s
              WHERE e.rel_type='requires' AND COALESCE(e.status,'published')='published'
              GROUP BY 1,2 HAVING count(e.weight)>0 AND sum(e.weight) < 0.995)
            SELECT s.src_id, s.name, round(s.w::numeric,3) w, s.c,
                   o.attrs::json->>'code' code, o.source_url, o.fetched_at
            FROM s JOIN kg_node o ON o.id=s.src_id ORDER BY s.c, s.w
        """, (SOURCE_SYSTEM,)).fetchall()
        out = []
        for r in rows:
            have = c.execute("""
                SELECT n.attrs::json->>'skill_name' sk
                FROM kg_edge e JOIN kg_node n ON n.id=e.dst_id AND n.type='skill_level'
                WHERE e.src_id=%s AND e.rel_type='requires'
                  AND COALESCE(e.status,'published')='published'
            """, (r["src_id"],)).fetchall()
            # 表格线、e2e 残留这类脏名字不要喂给 LLM，也不要保留
            keep = [x["sk"] for x in have if x["sk"] and not DIRTY.search(x["sk"])]
            out.append({**dict(r), "have": keep,
                        "dropped": [x["sk"] for x in have
                                    if x["sk"] and DIRTY.search(x["sk"])]})
        return out


def gen_llm(targets: list[dict]) -> dict[str, list[dict]]:
    """调 LLM 生成，结果落缓存（断点续跑：已有的不重复调）。"""
    from backend.agent.llm import get_chat_model

    cache: dict[str, Any] = {}
    if CACHE.is_file():
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    model = get_chat_model(fast=True, max_tokens=800)
    todo = [t for t in targets if t["src_id"] not in cache]
    print(f"  需调用 LLM {len(todo)} 个（缓存已有 {len(cache)}）")
    for i, t in enumerate(todo, 1):
        prompt = PROMPT.format(occ=t["name"], have="、".join(t["have"]) or "（无）")
        try:
            resp = model.invoke(prompt)
            txt = resp.content if hasattr(resp, "content") else str(resp)
            d = parse_json(txt)
            skills = d.get("skills") if isinstance(d, dict) else None
            if not isinstance(skills, list) or not skills:
                print(f"    [{i}/{len(todo)}] {t['name']} 解析失败，跳过")
                continue
            clean = []
            for s in skills:
                nm = canon(str(s.get("name") or ""))
                w = s.get("weight_pct")
                if nm and isinstance(w, (int, float)) and w > 0 and not DIRTY.search(nm):
                    clean.append({"name": nm, "weight_pct": float(w)})
            if clean:
                cache[t["src_id"]] = clean
                print(f"    [{i}/{len(todo)}] {t['name']}: {len(clean)} 项")
        except Exception as e:  # noqa: BLE001
            print(f"    [{i}/{len(todo)}] {t['name']} 失败: {str(e)[:80]}")
        if i % 10 == 0:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return cache


def main() -> int:
    ap = argparse.ArgumentParser(description="补齐残缺岗位技能并归一")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--gen", action="store_true", help="只跑 LLM 生成并缓存")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    targets = load_incomplete()
    if args.limit:
        targets = targets[: args.limit]
    print(f"残缺岗位 {len(targets)} 个")
    n_dirty = sum(len(t["dropped"]) for t in targets)
    print(f"  其中脏技能名（表格线/e2e 残留）待丢弃: {n_dirty}")

    if args.gen or args.apply:
        cache = gen_llm(targets)
    else:
        cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.is_file() else {}

    ready = [t for t in targets if cache.get(t["src_id"])]
    print(f"  已生成方案的岗位: {len(ready)}")
    add_skill = 0
    for t in ready:
        got = {s["name"] for s in cache[t["src_id"]]}
        add_skill += len(got - set(t["have"]))
    print(f"  新增技能项合计: {add_skill}")

    if not args.apply:
        for t in ready[:3]:
            plan = targets_by_weight([(s["name"], s["weight_pct"]) for s in cache[t["src_id"]]])
            print(f"\n  === {t['name']}（原 {t['c']} 项 Σw={t['w']}）===")
            for nm, w, lv in plan:
                tag = "" if nm in t["have"] else "新增"
                print(f"     {nm[:22]:24} {w:>5}% → L{lv} {tag}")
        print("\n（未写库；--apply 执行）")
        return 0

    bak = SQLITE.parent / (SQLITE.name + ".bak-fill")
    shutil.copy2(SQLITE, bak)
    print(f"\n  已备份源库 → {bak.name}")

    sq = sqlite3.connect(SQLITE)
    sq.row_factory = sqlite3.Row
    NCOLS = ("id region type name name_en name_zh aliases description attrs "
             "source_system source_id source_url license fetched_at confidence").split()
    ECOLS = ("id src_id dst_id rel_type region weight evidence attrs source_system "
             "source_id source_url license fetched_at confidence").split()
    n_node = n_edge = 0
    with pg_connect() as pg:
        for t in ready:
            occ, code = t["src_id"], t["code"] or t["src_id"]
            plan = targets_by_weight([(s["name"], s["weight_pct"]) for s in cache[occ]])
            # 旧边全部让位：PG 归档留痕，sqlite 物理删（源库无 status 列）
            pg.execute("""UPDATE kg_edge SET status='archived'
                          WHERE src_id=%s AND rel_type='requires'
                            AND NOT is_draft""", (occ,))
            sq.execute("DELETE FROM edges WHERE src_id=? AND rel_type='requires'", (occ,))
            for nm, wpct, lv in plan:
                sid = f"{code}|{nm}|L{lv}"
                nid = make_node_id(REGION, "skill_level", SOURCE_SYSTEM, sid)
                attrs = {"skill_name": nm, "skill_key": nm, "level": lv,
                         "occupation_code": code, "occupation_name": t["name"],
                         "weight_pct": wpct, "standard_type": "national_skill_standard",
                         "fill_basis": "llm_skill_fill"}
                ab = json.dumps(attrs, ensure_ascii=False)
                desc = f"{t['name']}（{code}）· {nm} · L{lv} · 权重{wpct:g}%"
                pg.execute("""
                    INSERT INTO kg_node (id,region,type,name,name_zh,description,attrs,
                        source_system,source_id,source_url,license,fetched_at,confidence,
                        status,is_draft)
                    VALUES (%s,%s,'skill_level',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        'published',false)
                    ON CONFLICT (id,is_draft) DO UPDATE SET attrs=EXCLUDED.attrs,
                        description=EXCLUDED.description
                """, (nid, REGION, f"{nm} · L{lv}", f"{nm} · L{lv}", desc, ab,
                      SOURCE_SYSTEM, sid, FILL_URL, LICENSE, t["fetched_at"], FILL_CONF))
                sq.execute(
                    f"INSERT OR REPLACE INTO nodes ({','.join(NCOLS)}) "
                    f"VALUES ({','.join('?' * len(NCOLS))})",
                    (nid, REGION, "skill_level", f"{nm} · L{lv}", None, f"{nm} · L{lv}",
                     None, desc, ab, SOURCE_SYSTEM, sid, FILL_URL, LICENSE,
                     t["fetched_at"], FILL_CONF))
                n_node += 1

                eid = make_edge_id(occ, "requires", nid)
                w = round(wpct / 100.0, 4)
                ev = (f"{t['name']} 技能构成补齐（原采集残缺 Σw={t['w']}）："
                      f"{nm}，权重{wpct:g}%，学生端基准档 L{BASE_LEVEL} 按权重定为 L{lv}")
                eab = json.dumps({"link_basis": "llm_skill_fill",
                                  "base_level": BASE_LEVEL}, ensure_ascii=False)
                pg.execute("""
                    INSERT INTO kg_edge (id,src_id,dst_id,rel_type,region,weight,evidence,
                        attrs,source_system,source_id,source_url,license,fetched_at,
                        confidence,status,is_draft)
                    VALUES (%s,%s,%s,'requires',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        'published',false)
                    ON CONFLICT (id,is_draft) DO UPDATE SET weight=EXCLUDED.weight,
                        status='published', evidence=EXCLUDED.evidence
                """, (eid, occ, nid, REGION, w, ev, eab, SOURCE_SYSTEM,
                      f"{sid}#req", FILL_URL, LICENSE, t["fetched_at"], FILL_CONF))
                sq.execute(
                    f"INSERT OR REPLACE INTO edges ({','.join(ECOLS)}) "
                    f"VALUES ({','.join('?' * len(ECOLS))})",
                    (eid, occ, nid, "requires", REGION, w, ev, eab, SOURCE_SYSTEM,
                     f"{sid}#req", FILL_URL, LICENSE, t["fetched_at"], FILL_CONF))
                n_edge += 1
        pg.commit()
        sq.commit()
    print(f"  写入节点 {n_node}，边 {n_edge}（两库）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
