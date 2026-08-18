"""把 hot_occupations_top100.json 渲染成方法论 + 清单的 Markdown。

    python -X utf8 scripts/render_hot_occupations_md.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = ROOT / "data" / "staging" / "hot_occupations_top100.json"
OUT = ROOT / "docs" / "热门岗位TOP100与方法论.md"

HEAD = """# 国内热门岗位 TOP100 · 方法论与清单

> 生成时间：{generated_at}
> 复现：`python -m crawlers.cn.harvest_hot_occupations --top 100` → `python -X utf8 scripts/render_hot_occupations_md.py`

## 一、先说结论

**在合规前提下，自动化拿不到真实招聘量。** 所以本文给出的是两张性质不同的表，而不是一张伪装成"招聘热度"的榜：

| | 内容 | 条数 | 能否排序 |
|---|---|---|---|
| **A 榜** | 有官方依据的岗位（人社部紧缺排名 / 国家认定新职业） | {total_ranked} | ✅ 可排序，依据可溯源 |
| **B 榜** | BOSS 公开分类树里的市场化岗位全集 | {market_pool_size} | ❌ 无热度，待校准 |

A 榜的 score **不是招聘量**，而是「官方紧缺度 + 国家认定新兴度」的合成信号。

## 二、怎么找热门岗位（方法论）

### 2.1 合规边界先划清

动手前先读三家的 `robots.txt`，这一步直接砍掉了原计划的主路径：

| 站点 | 关键规则 | 结论 |
|---|---|---|
| zhipin.com | `Disallow: *?position=*` `*?city=*` `/*?query=*` | ❌ 职位搜索接口被明确禁止 |
| zhaopin.com | `Disallow: /*?*` `*/api/*` `/*.json*` | ❌ 所有 API 与 JSON 全禁 |
| mohrss.gov.cn | 无 robots.txt（HTTP 404） | ✅ 未设限，且为主动公开发布内容 |

实测请求 `zhipin.com/wapi/zpgeek/search/joblist.json?...position=100101` 返回
`code:37 您的环境存在异常`（JS 挑战）。**不逆向、不绕验证码**，这条路放弃。

但同一站点的 `wapi/zpCommon/data/position.json`（职位分类树）**不在 Disallow 列表内**，
可以正常拉取 —— 合规判断要落到**具体路径**，不能因为"是招聘网站"就一刀切放弃。

### 2.2 三个数据源及其定位

| 源 | 提供什么 | 不提供什么 |
|---|---|---|
| **S1** 人社部《最缺工 100 个职业排行》 | 官方紧缺**排名** + 职业代码（对齐《职业分类大典》） | 已于 **2022Q4 停更**（专栏共 14 期，2019Q3–2022Q4），是历史快照 |
| **S2** 人社部新职业 / 新工种 | 国家认定的**新兴方向**，2019 年起 6 批 93 个新职业 | 无热度量级；2025 批完整名单在图片附件里，暂只取到部分 |
| **S3** BOSS 公开职位分类树 | **市场化岗位名全集**（21 一级 / 115 二级 / 851 三级）+ 领域标签 | **无热度**——`rank` 字段实测恒为 0，其余字段全是常量 |

三者互补：S1 有排名没新兴岗位，S2 有新兴方向没量级，S3 有完整岗位名没热度。

### 2.3 热度分怎么算

```
score = 0.6 × 官方紧缺分 + 0.4 × 新兴度分

官方紧缺分 = (101 - 榜单排名) / 100     不在榜为 0
新兴度分   = 1.0 新职业 / 0.6 新工种     否则 0
```

### 2.4 一个踩过的坑：不要为了凑指标编造代理变量

初版还有第三项「市场结构分 = 该岗位所属 BOSS 一级分类的岗位数 / 最大分类岗位数」，
想用它体现"市场化程度"。结果荒谬：

- **包装工排第 1**（生产制造有 100 个细分岗位 → 人人高分）
- **产品经理排 924 名**（产品类只有 15 个细分 → 人人低分）

该指标衡量的是**某领域细分岗位的数量**，与单个岗位的热度毫无关系。已删除。

教训：**没有真实信号时，宁可承认缺失，也不要用相关性存疑的代理变量凑出一个看起来精确的排名。**
一个标着"待校准"的空字段，比一个错误的分数有用。

### 2.5 为什么 A 榜看起来"不像热门岗位"

A 榜前 10 是营销员、汽车生产线操作工、快递员、餐厅服务员……而不是 Java、产品经理、算法工程师。

这不是 bug：人社部榜单的口径是**「招聘大于求职」的缺工程度**，反映的是**供给缺口**，
不是市场需求热度。制造业、服务业招不到人 ≠ 这些岗位最热门。

要得到直觉意义上的"热门岗位"，必须有**真实招聘岗位数 / 投递量**，见第五节。

## 三、A 榜 · TOP{top_n}（按合成热度倒序）

依据列含义：`缺工#N` = 人社部 2022Q4 榜第 N 名；`新职业`/`新工种` = 国家认定；
`领域` = 能对上 BOSS 分类树时的领域标签（仅标注，不参与打分）。

| # | 岗位 | 分数 | 依据 | 领域 | 职业代码 | 在库 |
|---:|---|---:|---|---|---|:---:|
{rows_a}

## 四、B 榜 · 市场化岗位全集（{market_pool_size} 个，无热度）

BOSS 分类树里、且不在 A 榜的岗位。**这些正是当前岗位库最缺的部分**——
库里 1641 个 occupation 全部来自《职业分类大典》，市场化岗位名一个都没有。

按一级领域分布：

| 领域 | 岗位数 | 示例 |
|---|---:|---|
{rows_b}

完整清单见 `data/staging/hot_occupations_top100.json` 的 `market_pool` 字段。

## 五、局限与后续校准

1. **缺真实招聘量**（最主要）。合规路径下拿不到。可选方案：
   - 用户在**自己账号**下于浏览器查看公开岗位数量，人工/半自动记录（需确认平台服务条款）
   - 改用招聘平台**主动发布的**《人才趋势报告》PDF —— 属公开发布内容，引用合规
   - 采购官方数据授权
2. **S1 已停更 3 年半**，2022Q4 的紧缺度不代表 2026 年现状。
3. **S2 的 2025 批不全**：17 个新职业 + 42 个新工种，完整名单在人社部公示的图片附件里，
   文本提取不到，需在浏览器中打开原图补全。
4. **名称归一未做**：同一岗位在大典叫"育婴员"、市场上叫"育婴师"/"月嫂"。
   A 榜与现有库比对时，97 个里 93 个靠职业代码精确命中、3 个靠名称命中、
   1 个（育婴员）未命中即属此类。B 榜 837 个岗位入库前必须先做别名归一，否则会产生大量重复节点。

## 六、数据源溯源

| id | URL | 许可 | 备注 |
|---|---|---|---|
{rows_src}
"""


def main() -> None:
    d = json.loads(SRC.read_text(encoding="utf-8"))

    # A 榜是否已在岗位库
    try:
        from backend.kg.pg_store.client import session

        names = [it["name"] for it in d["top"]]
        in_db = set()
        with session() as c, c.cursor() as cur:
            for n in names:
                cur.execute(
                    "SELECT 1 FROM kg_node WHERE type='occupation' AND name=%s LIMIT 1", (n,)
                )
                if cur.fetchone():
                    in_db.add(n)
    except Exception as e:  # 库不可用时不阻塞出文档
        print(f"[warn] 查库失败，'在库'列留空: {e}", file=sys.stderr)
        in_db = set()

    rows_a = []
    for it in d["top"]:
        basis = []
        if it.get("shortage_rank"):
            basis.append(f"缺工#{it['shortage_rank']}")
        if it.get("emerging_type"):
            basis.append("新职业" if it["emerging_type"] == "new_occupation" else "新工种")
        rows_a.append(
            "| {r} | {n} | {s:.4f} | {b} | {d} | {c} | {k} |".format(
                r=it["hot_rank"], n=it["name"], s=it["score"],
                b=" + ".join(basis) or "—",
                d=it.get("boss_l1") or "—",
                c=it.get("occupation_code") or "—",
                k="✅" if it["name"] in in_db else "❌",
            )
        )

    by_l1: dict[str, list[str]] = {}
    for j in d["market_pool"]:
        by_l1.setdefault(j["boss_l1"], []).append(j["name"])
    rows_b = [
        "| {k} | {n} | {eg} |".format(k=k, n=len(v), eg="、".join(v[:5]))
        for k, v in sorted(by_l1.items(), key=lambda x: -len(x[1]))
    ]

    rows_src = [
        "| `{id}` | {url} | {lic} | {note} |".format(
            id=s["id"], url=s["url"], lic=s["license"], note=s["note"]
        )
        for s in d["sources"]
    ]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        HEAD.format(
            generated_at=d["generated_at"],
            total_ranked=d["total_ranked"],
            market_pool_size=d["market_pool_size"],
            top_n=len(d["top"]),
            rows_a="\n".join(rows_a),
            rows_b="\n".join(rows_b),
            rows_src="\n".join(rows_src),
        ),
        encoding="utf-8",
    )
    print(f"已生成 {OUT}")
    print(f"  A 榜 {len(d['top'])} 行 | B 榜 {d['market_pool_size']} 个（按领域汇总 {len(rows_b)} 行）")
    print(f"  A 榜已在库: {len(in_db)}/{len(d['top'])}")


if __name__ == "__main__":
    main()
