"""按「L2 为基准 + 权重相对高低」重定岗位的技能要求档。

为什么要重定
------------
国标一个职业标准覆盖 5 个职业等级，每级有各自的技能要求与权重。采集时对每个
(技能, 等级) 都建了 requires 边，去重脚本的兜底规则「取档位高的」把所有岗位的
要求压成顶格 L5 —— 学员几乎不可能达标。详见
docs/根因分析-国标采集丢失的两个维度.md。

规则
----
1. **基准 L2**：学生端面向在校生与应届生，L2（掌握）是合适的锚点。
   该岗位若没有 L2 档数据，取最接近的档作基准。
2. 以基准档那一套边为准（国标每档权重不同，必须锚定一档再比较）。
3. 同一岗位内按权重**排名**浮动：前 25% 提到 L3（核心技能），
   后 25% 降到 L1（边缘技能），其余留基准档。
   用排名而非「相对平均值的倍数」：国标权重分布本就均衡（4–6 个技能、
   0.1–0.35），倍数阈值下大量岗位所有技能都判同一档，区分不出核心与边缘。
4. **同权重必须同档**：排名法会把权重完全相同的技能切到不同档（实测
   中式烹调师「原料初加工」与「原料分档与切配」同为 0.2 却判了 L2 / L1）。
   因此先按权重分组、按组排名，组内一律同档。浮点毛刺（0.13999999999999999）
   先量化到 4 位小数再分组，否则二进制表示差异会让相同权重分到不同组。
5. 目标档若该技能没有对应的 skill_level 节点，回落到最近的可用档 ——
   边必须指向真实存在的节点。

权重不变：基准档那套权重 Σ≈1，改档位不动权重，所以归一性天然保持。

用法::
    python -X utf8 scripts/relevel_occupation_requires.py --dry-run
    python -X utf8 scripts/relevel_occupation_requires.py --apply
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SQLITE = ROOT / "data" / "graph" / "kg.sqlite"

from backend.kg.pg_store.client import connect as pg_connect  # noqa: E402
from backend.kg.provenance import make_edge_id  # noqa: E402

APPLY = "--apply" in sys.argv
BASE_LEVEL = 2
TOP_FRAC = BOT_FRAC = 0.25
QUANT = 4                      # 权重量化位数，消除浮点毛刺


def _a(raw):
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def plan_targets(edges: list[dict], base: int) -> list[tuple[dict, int]]:
    """返回 [(边, 目标档)]。同权重同档 —— 按权重分组后对组排名。"""
    groups: dict[float, list[dict]] = defaultdict(list)
    for e in edges:
        groups[round(e["w"] or 0, QUANT)].append(e)
    ordered = sorted(groups, reverse=True)              # 权重从高到低的组
    n = len(edges)
    top_n = max(1, round(n * TOP_FRAC))
    bot_n = max(1, round(n * BOT_FRAC))
    out: list[tuple[dict, int]] = []
    # 组不可拆（同权重必须同档），所以配额按「加进来不超额」判断，而不是
    # 「没满就加」——后者会让一个大的并列组把配额撑爆：室内装饰设计师有三个
    # 技能并列 0.2，配额 top_n=2，若按「没满就加」会把 4 项都判成核心。
    hi_groups, lo_groups = set(), set()
    acc = 0
    for g in ordered:
        size = len(groups[g])
        if acc + size > top_n:
            break
        hi_groups.add(g)
        acc += size
    acc = 0
    for g in reversed(ordered):
        if g in hi_groups:
            break
        size = len(groups[g])
        if acc + size > bot_n:
            break
        lo_groups.add(g)
        acc += size
    for g in ordered:
        tgt = 3 if g in hi_groups else (1 if g in lo_groups else base)
        for e in groups[g]:
            out.append((e, tgt))

    return out


def main() -> int:
    sq = sqlite3.connect(SQLITE)
    sq.row_factory = sqlite3.Row
    rows = sq.execute("""
        SELECT o.id occ_id, o.name occ_name, e.id edge_id, e.dst_id, e.weight,
               n.attrs sa
        FROM nodes o
        JOIN edges e ON e.src_id=o.id AND e.rel_type='requires'
        JOIN nodes n ON n.id=e.dst_id AND n.type='skill_level'
        WHERE o.type='occupation' AND o.source_system='MOHRSS_CN'
    """).fetchall()

    per = defaultdict(lambda: defaultdict(list))
    node_of = {}                       # (occ, skill_name, level) -> dst_id
    names = {}
    for r in rows:
        a = _a(r["sa"])
        try:
            lv = int(a.get("level"))
        except (TypeError, ValueError):
            continue
        if not 1 <= lv <= 5:
            continue
        sk = a.get("skill_name") or ""
        per[r["occ_id"]][lv].append(
            {"eid": r["edge_id"], "dst": r["dst_id"], "w": r["weight"], "sk": sk}
        )
        node_of[(r["occ_id"], sk, lv)] = r["dst_id"]
        names[r["occ_id"]] = r["occ_name"]

    keep, drop, dist, shifted = [], [], defaultdict(int), defaultdict(int)
    for occ, lvs in per.items():
        base = BASE_LEVEL if BASE_LEVEL in lvs else min(lvs, key=lambda x: abs(x - BASE_LEVEL))
        for e, tgt in plan_targets(lvs[base], base):
            avail = {lv for lv in lvs if (occ, e["sk"], lv) in node_of}
            if tgt not in avail and avail:
                tgt = min(avail, key=lambda x: (abs(x - tgt), x))
            keep.append({"occ": occ, "sk": e["sk"], "w": e["w"],
                         "dst": node_of[(occ, e["sk"], tgt)], "level": tgt})
            dist[tgt] += 1
            shifted[tgt - base] += 1
        # 该岗位所有旧 requires 边都要让位（保留项会以新 dst 重建）
        for lv in lvs:
            drop += [x["eid"] for x in lvs[lv]]

    print(f"国标岗位 {len(per)} 个")
    print(f"重定后要求档分布: " + "  ".join(f"L{k}={dist[k]}" for k in sorted(dist)))
    print(f"相对基准位移:     " + "  ".join(f"{k:+d}档={shifted[k]}" for k in sorted(shifted)))
    print(f"保留边 {len(keep)} 条，旧边待清 {len(drop)} 条")

    if not APPLY:
        print("\n（dry-run，加 --apply 写入）")
        return 0

    bak = SQLITE.parent / (SQLITE.name + ".bak-relevel")
    import shutil
    shutil.copy2(SQLITE, bak)
    print(f"\n  已备份源库 → {bak.name}")

    with pg_connect() as pg:
        # 先清空这些岗位的 requires：PG 归档留痕，sqlite 物理删（源库无 status 列）
        occs = list(per)
        pg.execute("""UPDATE kg_edge SET status='archived'
                      WHERE src_id=ANY(%s) AND rel_type='requires'""", (occs,))
        q = ",".join("?" * len(occs))
        sq.execute(f"DELETE FROM edges WHERE rel_type='requires' AND src_id IN ({q})", occs)

        cols = ("id src_id dst_id rel_type region weight evidence attrs source_system "
                "source_id source_url license fetched_at confidence").split()
        n = 0
        for k in keep:
            eid = make_edge_id(k["occ"], "requires", k["dst"])
            ev = (f"《{names[k['occ']]}国家职业技能标准》技能要求：{k['sk']}"
                  f"（学生端基准档 L{BASE_LEVEL}，按岗位内权重重定为 L{k['level']}）")
            at = json.dumps({"link_basis": "relevel_by_weight_rank",
                             "base_level": BASE_LEVEL}, ensure_ascii=False)
            pg.execute("""
                INSERT INTO kg_edge (id,src_id,dst_id,rel_type,region,weight,evidence,attrs,
                    source_system,source_id,source_url,license,fetched_at,confidence,status)
                SELECT %s,%s,%s,'requires','CN',%s,%s,%s,'MOHRSS_CN',%s,
                       o.source_url,'MOHRSS-public',o.fetched_at,'official','published'
                FROM kg_node o WHERE o.id=%s
                ON CONFLICT (id) DO UPDATE SET weight=EXCLUDED.weight,
                    status='published', evidence=EXCLUDED.evidence, attrs=EXCLUDED.attrs
            """, (eid, k["occ"], k["dst"], k["w"], ev, at, f"{k['occ']}#{k['sk']}", k["occ"]))
            src = sq.execute("SELECT source_url, fetched_at FROM nodes WHERE id=?",
                             (k["occ"],)).fetchone()
            sq.execute(
                f"INSERT OR REPLACE INTO edges ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                (eid, k["occ"], k["dst"], "requires", "CN", k["w"], ev, at, "MOHRSS_CN",
                 f"{k['occ']}#{k['sk']}", src["source_url"] if src else None,
                 "MOHRSS-public", src["fetched_at"] if src else None, "official"))
            n += 1
        pg.commit(); sq.commit()
        print(f"  写入 {n} 条 requires（两库）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
