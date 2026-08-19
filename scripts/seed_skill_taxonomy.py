"""技能分类（category）回填 · 测试数据种子。

分类依据
--------
库内 skill_level 全部来自「国家职业技能标准」（attrs.standard_type=national_skill_standard）。
该标准的正文结构为「职业功能 → 工作内容 → 技能要求 → 相关知识」
（见《国家职业标准编制技术规程（2023 年版）》），我们爬取时只保留了
「工作内容/技能要求」这一层，父级的「职业功能」列丢失了。

本脚本按国标「职业功能」的通用维度重建分类，用关键词规则从技能名反推。
规则按特异性从高到低顺序匹配，先命中先归类；命中不了的保持 NULL（不硬塞）。

置信度：规则派生，非官方原始字段 → confidence 记为 derived，可人工覆盖。

用法：
    python scripts/seed_skill_taxonomy.py --dry-run   # 只看分布不写库
    python scripts/seed_skill_taxonomy.py             # 写入 kg_node.category
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import connect  # noqa: E402
from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL  # noqa: E402
from backend.kg.pg_store.skill_taxonomy import name_of, to_code  # noqa: E402

# 明显的表格解析噪声，不是技能，不分类
NOISE = re.compile(r"^(总计|合计|小计|序号|备注|说明|其他|其它|—+|-+)$")

# (分类名, 关键词正则)；顺序即优先级，先命中先归类
RULES: list[tuple[str, str]] = [
    # 安全类词汇最特异，且国标中「安全环保」通常独立成职业功能
    # 注意：装药/填塞/起爆/警戒/爆后 是爆破作业的操作步骤，不是安全管理，归「操作与加工」
    ("安全与环保", r"安全|环保|职业健康|应急|事故|救援|抢险|消防|火灾|隐患|风险辨识|防护"),
    # 「培训/指导」是国标里高频独立职业功能，且优先于「技术管理」
    ("培训与指导", r"培训|指导|带徒|教育|授课|讲解"),
    # 「XX准备」是国标极常见的首个职业功能
    ("作业准备", r"准备|工前|岗前|交接班"),
    ("设备维护与检修", r"设备|仪器|维护|保养|维保|检修|维修|故障|异常|排除|排查|调整|检定|校准|润滑|装调"),
    ("质量与检验", r"质量|检验|检测|检查|测定|测量|试验|化验|样品|计量|不合格品|评价|评估|巡视|监测"),
    ("数据与信息", r"数据|信息|记录|报表|报告|统计|档案|录入"),
    ("服务与业务", r"服务|业务|接待|受理|办理|营销|客户|照护|照料|咨询|调度|导购|收银|快件|收寄|收派|派送|投递"),
    ("技术管理与创新", r"技术|工艺|创新|革新|优化|方案|设计|研发|改进|攻关|开发|编程"),
    ("运营与管理", r"管理|运营|运行|监控|现场|人员|成本|计划|组织|协调|监督"),
    # 兜底：动手做的部分
    ("操作与加工", r"操作|加工|制作|制备|装配|安装|拆除|施工|生产|作业|处理|输送|采样|配料|搅拌|泵送|发酵|栽植|修剪|养护|驾驶|控制|包装|测图|调绘|装药|填塞|起爆|警戒|爆后|防治"),
]

COMPILED = [(name, re.compile(pat)) for name, pat in RULES]


def classify(skill_key: str) -> str | None:
    """返回分类 **code**（不是中文名）。

    RULES 里第一列写中文是为了规则本身可读，落库一律转成 code —— `kg_node.category`
    自 2026-08-18 起存 code，这里若写回中文名会把迁移覆盖掉，而且不报错。
    """
    s = (skill_key or "").strip()
    if not s or NOISE.match(s):
        return None
    for name, pat in COMPILED:
        if pat.search(s):
            return to_code(name)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计分布，不写库")
    args = ap.parse_args()

    with connect() as conn:
        keys = [
            r["k"]
            for r in conn.execute(
                f"SELECT DISTINCT {SKILL_KEY_SQL} AS k FROM kg_node n WHERE n.type='skill_level'"
            ).fetchall()
        ]
        mapping = {k: c for k in keys if (c := classify(k))}

        dist: dict[str, int] = {}
        for c in mapping.values():
            dist[c] = dist.get(c, 0) + 1
        print(f"逻辑技能总数 {len(keys)} · 可分类 {len(mapping)} "
              f"({len(mapping) * 100 // max(len(keys), 1)}%) · 未分类 {len(keys) - len(mapping)}")
        for c, n in sorted(dist.items(), key=lambda x: -x[1]):
            print(f"  {n:5d}  {c:<10} {name_of(c)}")

        if args.dry_run:
            print("\n[dry-run] 未写库")
            return 0

        # 一次性批量回写：逐 key UPDATE 会对 8868 行重复计算 SKILL_KEY_SQL，
        # 1500+ 次全表扫描慢到不可用，故先落临时表再 JOIN 更新。
        conn.execute("CREATE TEMP TABLE _skill_cat (k text PRIMARY KEY, cat text) ON COMMIT DROP")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO _skill_cat(k, cat) VALUES (%s, %s) ON CONFLICT (k) DO NOTHING",
                list(mapping.items()),
            )
        # 只**补缺**，不覆盖已有分类：这套规则是按国标技能名写的，
        # 里面没有 TECH/DESIGN/GENERAL 这些互联网侧的类
        # （「技术管理与创新」会落到 MANAGE），无差别跑一遍会把 LLM 技能的
        # TECH 改写成 MANAGE，而且悄无声息。
        r = conn.execute(
            f"UPDATE kg_node n SET category = t.cat FROM _skill_cat t "
            # NOT n.is_draft：种子脚本只动线上行，别把别人没发布的草稿也改了
            f"WHERE n.type='skill_level' AND NOT n.is_draft AND {SKILL_KEY_SQL} = t.k "
            f"AND COALESCE(NULLIF(n.category,''), 'UNSORTED') = 'UNSORTED' "
            f"AND n.category IS DISTINCT FROM t.cat"
        )
        print(f"\n已更新 kg_node.category：{r.rowcount} 个 skill_level 节点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
