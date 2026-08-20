"""基准库数据质量审计：把「该清掉的东西」逐类列出来，只读不改。

为什么先审计再清洗
------------------
「无效数据」不是一个类别，是七八类形态各异的东西：测试 fixture、职业名被当成
技能、URL 编码没解码、表格解析噪声、孤儿边……每一类的判据和处置方式都不同。
不先量化就动手清，要么漏，要么误删真数据。

这个脚本是**只读**的，产出 `reports/baseline_quality_audit.json`，
清洗脚本按它的分类逐项处理。也可以当发布前闸门跑（`--gate` 时有问题即非零退出）。

用法::

    python -X utf8 scripts/audit_baseline_quality.py
    python -X utf8 scripts/audit_baseline_quality.py --gate     # 有问题就 exit 1
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import session

REPORT = ROOT / "reports" / "baseline_quality_audit.json"

# 三个维度别混：
#   _ROW_*   这一行是线上行（非草稿）—— 用于「行是否存在」
#   _LIVE_*  线上行 **且** 未归档 —— 用于「这条数据有没有问题」
# 混用会做出自相矛盾的判据：`edge_to_archived` 若用 _LIVE_N 去 JOIN 目标节点，
# 就是一边要求「未归档」一边找「已归档」，恒为 0；而那些边会转头被算成孤儿边。
_ROW_N = "COALESCE(n.is_draft, false) = false"
_ROW_E = "COALESCE(e.is_draft, false) = false"
_LIVE_N = _ROW_N + " AND COALESCE(n.status, 'published') <> 'archived'"
_LIVE_E = _ROW_E + " AND COALESCE(e.status, 'published') <> 'archived'"


def _has_is_draft(cur) -> bool:
    """兼容主键迁移前后：is_draft 列不存在时退回全表。"""
    cur.execute(
        "SELECT count(*)::int n FROM information_schema.columns "
        "WHERE table_name='kg_node' AND column_name='is_draft'"
    )
    return bool(cur.fetchone()["n"])


CHECKS: list[tuple[str, str, str]] = [
    # (key, 说明, SQL —— 必须 SELECT 出 id, name, extra 三列)
    (
        "test_fixture",
        "测试 fixture：E2E/单测遗留，已扩散到真实岗位的技能构成上",
        """
        SELECT n.id, n.name, n.type AS extra FROM kg_node n
        WHERE {live}
          AND (n.id LIKE 'TESTFX:%' OR n.name LIKE '\\_\\_e2e%' OR n.name LIKE '\\_\\_test%'
               OR n.name LIKE '\\_\\_cat\\_e2e%' OR n.name LIKE '\\_\\_del\\_e2e%'
               OR n.name LIKE '\\_\\_probe%'
               OR n.name LIKE '%临时测试%' OR n.name LIKE '测试技能%'
               OR (n.attrs::jsonb->>'test_fixture') = 'true')
        """,
    ),
    (
        "url_encoded_name",
        "名字里带 URL 编码：前端双重编码后建出来的幽灵记录",
        """
        SELECT n.id, n.name, n.type AS extra FROM kg_node n
        WHERE {live} AND n.name ~ '%[0-9A-Fa-f]{{2}}'
        """,
    ),
    (
        "parse_noise",
        "表格解析噪声：不是技能/岗位，是原表的合计行与表头",
        """
        SELECT n.id, n.name, n.type AS extra FROM kg_node n
        WHERE {live}
          AND regexp_replace(COALESCE(n.attrs::jsonb->>'skill_name', n.name), ' · L[1-5]$', '')
              ~ '^(总计|合计|小计|序号|备注|说明|其他|其它|-+|—+)$'
        """,
    ),
    (
        # 判据必须是「与某个 occupation 同名」，不能用「工/员/师」后缀正则 ——
        # 那样 `基础施工`、`锻造加工`、`偏心工件及曲轴加工` 会被当成职业名
        # （实测误伤 545 个里绝大多数是真技能）。同名才是硬证据。
        "occupation_as_skill",
        "职业名被当成技能：与岗位同名**且**以「人」的后缀结尾",
        """
        SELECT n.id, n.name, o.name AS extra FROM kg_node n
        JOIN kg_node o ON o.type = 'occupation' AND {live_o}
          AND o.name = regexp_replace(COALESCE(n.attrs::jsonb->>'skill_name', n.name),
                                      ' · L[1-5]$', '')
        WHERE {live} AND n.type = 'skill_level'
          -- 光「与岗位同名」不够：`功能测试`/`性能测试`/`商业数据分析` 既是岗位
          -- 也是合理的技能（会做这件事）。加上「人」的后缀才是职业名称混入
          -- （水泥质检员、城市轨道交通行车值班员、管涵顶进工）。
          AND regexp_replace(COALESCE(n.attrs::jsonb->>'skill_name', n.name), ' · L[1-5]$', '')
              ~ '(员|工|师|长|主管|经理|技师|专员|负责人)$'
        """,
    ),
    (
        # 字数不是判据：`装配`、`开炉`、`点焊` 都是 2 字的明确操作技能。
        # 收紧成「≤2 字 **且** 没有任何岗位要求它」—— 没人用的极短名才可疑。
        # 这一类**不阻塞发布**，需要人看一眼再决定。
        "too_vague_skill",
        "疑似过泛：名字 ≤2 字，且整个逻辑技能（全部档位）都没有岗位引用",
        """
        WITH sk AS (
          SELECT n.id, n.name, n.category,
                 regexp_replace(COALESCE(n.attrs::jsonb->>'skill_name', n.name),
                                ' · L[1-5]$', '') AS nm,
                 COALESCE(n.attrs::jsonb->>'skill_key', '') AS k
          FROM kg_node n WHERE {live} AND n.type = 'skill_level'
        ),
        -- 「无引用」必须按 skill_key 聚合判断：技能是 L1–L5 五个节点，
        -- 只有被要求的那一档挂 requires 边，逐节点判会把 `装配 · L4` 这种
        -- 「L2 有人要、L4 没人要」的正常档位误报成过泛。
        used AS (
          SELECT DISTINCT COALESCE(n2.attrs::jsonb->>'skill_key', '') AS k
          FROM kg_edge e JOIN kg_node n2 ON n2.id = e.dst_id
          WHERE e.rel_type = 'requires' AND COALESCE(e.status,'published') = 'published'
        )
        SELECT sk.id, sk.name, sk.category AS extra FROM sk
        WHERE length(sk.nm) <= 2
          AND sk.k NOT IN (SELECT k FROM used)
        """,
    ),
    (
        "empty_name",
        "名字为空或只有空白",
        """
        SELECT n.id, COALESCE(n.name, '') AS name, n.type AS extra FROM kg_node n
        WHERE {live} AND (n.name IS NULL OR btrim(n.name) = '')
        """,
    ),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", action="store_true", help="有问题即 exit 1，用于发布前闸门")
    ap.add_argument("--sample", type=int, default=8, help="每类打印几个样例")
    args = ap.parse_args()

    out: dict[str, object] = {}
    with session() as c, c.cursor() as cur:
        has = _has_is_draft(cur)
        live_n, live_e = (_LIVE_N, _LIVE_E) if has else ("true", "true")
        row_n, row_e = (_ROW_N, _ROW_E) if has else ("true", "true")

        for key, desc, sql in CHECKS:
            cur.execute(sql.format(live=live_n, live_o=live_n.replace("n.", "o.")))
            rows = [dict(r) for r in cur.fetchall()]
            out[key] = {"desc": desc, "count": len(rows),
                        "sample": [(r["id"], r["name"], r.get("extra")) for r in rows[:args.sample]]}
            print("%-22s %5d  %s" % (key, len(rows), desc))
            for r in rows[:args.sample]:
                print("      %-30s %s" % (str(r["name"])[:30], r.get("extra") or ""))

        # 孤儿边：外键 2026-08-19 已删，两端存在性只能靠查
        cur.execute(
            f"""
            SELECT count(*)::int n FROM kg_edge e
            WHERE {row_e}
              AND (NOT EXISTS (SELECT 1 FROM kg_node n WHERE n.id = e.src_id AND {row_n})
                OR NOT EXISTS (SELECT 1 FROM kg_node n WHERE n.id = e.dst_id AND {row_n}))
            """
        )
        n = cur.fetchone()["n"]
        out["orphan_edge"] = {"desc": "孤儿边：某一端的节点行根本不存在（与归档无关）", "count": n}
        print("%-22s %5d  孤儿边（外键已删，只能靠查）" % ("orphan_edge", n))

        # 边指向已归档节点：节点不可见而边仍 published，会让计数与详情对不上
        cur.execute(
            f"""
            SELECT count(*)::int n FROM kg_edge e
            JOIN kg_node n ON n.id = e.dst_id AND {row_n}
            WHERE {row_e} AND COALESCE(e.status,'published') = 'published'
              AND COALESCE(n.status,'published') = 'archived'
            """
        )
        n = cur.fetchone()["n"]
        out["edge_to_archived"] = {
            "desc": "边 published 但指向 archived 节点：列表按边计数、详情按可见节点，两个数字",
            "count": n,
        }
        print("%-22s %5d  边 published 指向 archived 节点" % ("edge_to_archived", n))

        # 岗位技能权重和异常
        cur.execute(
            f"""
            SELECT count(*)::int n FROM (
              SELECT e.src_id, sum(e.weight) w FROM kg_edge e
              JOIN kg_node n ON n.id = e.dst_id AND n.type='skill_level' AND {live_n}
              WHERE e.rel_type='requires' AND {live_e}
                AND COALESCE(e.status,'published')='published' AND e.weight IS NOT NULL
              GROUP BY 1
            ) t WHERE w < 0.85 OR w > 1.15
            """
        )
        n = cur.fetchone()["n"]
        out["weight_sum_off"] = {"desc": "岗位 requires 权重和超出 0.85–1.15", "count": n}
        print("%-22s %5d  岗位权重和超出容差" % ("weight_sum_off", n))

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print("报告：", REPORT)

    if args.gate:
        # 闸门只卡「必须清掉」的几类。过泛/职业名混入是数据质量问题，
        # 需要人判断，不该阻塞发布。
        blocking = ("test_fixture", "url_encoded_name", "parse_noise", "empty_name",
                    "orphan_edge", "edge_to_archived")
        bad = {k: out[k]["count"] for k in blocking if out[k]["count"]}
        if bad:
            print("闸门未通过：", bad)
            sys.exit(1)
        print("闸门通过")


if __name__ == "__main__":
    main()
