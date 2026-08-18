"""合成「国内热门岗位 TOP100」候选榜 —— 多源交叉、可复现、全程合规。

为什么不是直接爬招聘网站
------------------------
BOSS/智联/猎聘的**职位搜索接口被 robots.txt 明确禁止**（实测 2026-08-17）：

    zhipin.com/robots.txt :  Disallow: *?position=*  /  *?city=*  /  /*?query=*
    zhaopin.com/robots.txt:  Disallow: /*?*  /  */api/*  /  /*.json*

实测请求 `zhipin.com/wapi/zpgeek/search/joblist.json?...position=...` 返回
`code:37 您的环境存在异常`（JS 挑战）。**不逆向、不绕过**，这条路直接放弃。

因此热度改由「官方信号 + 市场结构」合成，各信号都可溯源、可复算。

三个数据源（全部合规）
----------------------
S1 人社部《全国招聘大于求职"最缺工"的 100 个职业排行》
   - 官方口径紧缺度，含明确排名与职业代码（与《职业分类大典》一致）
   - ⚠ 该榜 **2022Q4 后停更**（专栏共 14 期，2019Q3–2022Q4），是历史快照
   - mohrss.gov.cn 无 robots.txt（HTTP 404），且为主动公开发布内容

S2 人社部新职业 / 新工种（2019 年起 6 批 93 个新职业）
   - 国家认定的新兴需求方向，代表"未来热"而非"当下量"
   - 2024 批 19 个已取全；2025 批 17 职业 + 42 工种仅取到部分（名单在图片附件里）

S3 BOSS 直聘公开职位分类树 `wapi/zpCommon/data/position.json`
   - **不在 robots Disallow 内**，可正常拉取；21 一级 / 115 二级 / 851 三级
   - 只提供岗位名全集与领域结构，**不含热度**（rank 字段恒为 0，实测）

热度分（可解释、可调权重）
--------------------------
    score = 0.6 * 官方紧缺分 + 0.4 * 新兴度分

    官方紧缺分 = (101 - 榜单排名) / 100      不在榜为 0
    新兴度分   = 1.0 新职业 / 0.6 新工种      否则 0

**为什么不给 BOSS 岗位打分**：初版曾用「所属一级分类的岗位数」当市场结构分，
结果荒谬 —— 产品经理排 924 名、包装工排第 1。因为该指标衡量的是某领域**细分
岗位的数量**（产品类仅 15 个细分、生产制造 100 个），与单个岗位的热度无关。
已删除。BOSS 分类树在本脚本里只承担两件事：提供**市场化岗位名全集**、给岗位
打**领域标签**，不参与排序。

于是输出拆成两张表：

  A 榜 `top`         有官方依据（紧缺排名 / 新职业认定）的岗位，可排序
  B 榜 `market_pool` BOSS 851 个市场化岗位，**无热度、不排序**，等真实招聘量校准

⚠ A 榜也不是真实招聘量：它是「官方紧缺度 + 国家认定新兴度」的合成信号。
   "缺工"反映的是**供给缺口**而非市场需求热度，所以 A 榜偏传统行业属预期内。
   真实岗位数需登录后在平台自有账号下查看，见 task #6。

用法::

    python -m crawlers.cn.harvest_hot_occupations              # 合成并落盘
    python -m crawlers.cn.harvest_hot_occupations --top 100    # 指定榜单长度
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RAW = ROOT / "data" / "raw" / "CN"
STAGING = ROOT / "data" / "staging"

SRC_SHORTAGE_PDF = RAW / "mohrss" / "shortage" / "shortage_2022Q4.pdf"
SRC_SHORTAGE_URL = (
    "https://www.mohrss.gov.cn/SYrlzyhshbzb/dongtaixinwen/buneiyaowen/rsxw/"
    "202301/t20230118_493691.html"
)
SRC_BOSS_JSON = RAW / "boss" / "boss_position_20260817.json"
SRC_BOSS_URL = "https://www.zhipin.com/wapi/zpCommon/data/position.json"
SRC_NEWJOB_2024_URL = (
    "https://www.mohrss.gov.cn/SYrlzyhshbzb/dongtaixinwen/buneiyaowen/rsxw/"
    "202407/t20240731_523281.html"
)

# —— S2：人社部新职业（2024 批，正文已取全 19 个）——
NEWJOBS_2024 = [
    "生物工程技术人员", "口腔卫生技师", "网络安全等级保护测评师", "云网智能运维员",
    "生成式人工智能系统应用员", "工业互联网运维员", "智能网联汽车测试员",
    "有色金属现货交易员", "用户增长运营师", "会展搭建师", "文创产品策划运营师",
    "储能电站运维管理员", "电能质量管理员", "版权经纪人", "网络主播",
    "滑雪巡救员", "氢基直接还原炼铁工", "智能制造系统运维员", "智能网联汽车装调运维员",
]

# —— 2025 批：公示 17 职业 + 42 工种，完整名单在图片附件里，此处仅收录已确认的 ——
# TODO(需登录/浏览器)：从人社部公示原文补全，见 docs/热门岗位TOP100与方法论.md
NEWJOBS_2025_PARTIAL = [
    "跨境电商运营管理师", "无人机群飞行规划员", "电子电路设计师", "装修管家",
    "咖啡加工工", "钢结构装配工", "检验检测管理工程技术人员",
]
NEWTRADES_2025_PARTIAL = [
    "黄金鉴定估价师", "旅拍定制师", "智慧仓运维员", "睡眠健康管理师",
    "服务犬驯养师", "生成式人工智能系统测试员", "保鲜花制作工",
    "生成式人工智能动画制作员", "特种救援员", "烧烤料理师",
]

W_SHORTAGE, W_EMERGING = 0.6, 0.4


def load_shortage() -> list[dict]:
    """S1：解析人社部最缺工榜 PDF。"""
    if not SRC_SHORTAGE_PDF.exists():
        print(f"[warn] 缺 {SRC_SHORTAGE_PDF}，跳过 S1", file=sys.stderr)
        return []
    from crawlers.cn.harvest_mohrss_shortage import parse_pdf_ranking

    return parse_pdf_ranking(SRC_SHORTAGE_PDF)


def load_boss() -> list[dict]:
    """S3：展开 BOSS 三级职位分类树。"""
    if not SRC_BOSS_JSON.exists():
        print(f"[warn] 缺 {SRC_BOSS_JSON}，跳过 S3", file=sys.stderr)
        return []
    data = json.loads(SRC_BOSS_JSON.read_text(encoding="utf-8"))
    out = []
    for l1 in data["zpData"]:
        for l2 in l1.get("subLevelModelList") or []:
            for l3 in l2.get("subLevelModelList") or []:
                out.append(
                    {
                        "name": l3["name"],
                        "code": l3.get("code"),
                        "l1": l1["name"],
                        "l2": l2["name"],
                    }
                )
    return out


def build(top_n: int = 100) -> dict:
    shortage = load_shortage()
    boss = load_boss()
    boss_by_name = {j["name"]: j for j in boss}

    emerging = {n: ("new_occupation", 1.0) for n in NEWJOBS_2024 + NEWJOBS_2025_PARTIAL}
    emerging.update({n: ("new_trade", 0.6) for n in NEWTRADES_2025_PARTIAL})

    cand: dict[str, dict] = {}

    def touch(name: str) -> dict:
        j = boss_by_name.get(name) or {}
        return cand.setdefault(
            name,
            {
                "name": name,
                "sources": [],
                "shortage_rank": None,
                "occupation_code": None,
                # 领域标签：能对上 BOSS 分类就带上，纯标注用途，不参与打分
                "boss_l1": j.get("l1"),
                "boss_l2": j.get("l2"),
                "emerging_type": None,
                "s_shortage": 0.0,
                "s_emerging": 0.0,
            },
        )

    for r in shortage:
        it = touch(r["name"])
        it["sources"].append("mohrss_shortage_2022Q4")
        it["shortage_rank"] = r.get("rank")
        it["occupation_code"] = r.get("code")
        it["s_shortage"] = (101 - r["rank"]) / 100 if r.get("rank") else 0.0

    for name, (etype, score) in emerging.items():
        it = touch(name)
        it["sources"].append("mohrss_new_occupation")
        it["emerging_type"] = etype
        it["s_emerging"] = score

    for it in cand.values():
        it["score"] = round(
            W_SHORTAGE * it["s_shortage"] + W_EMERGING * it["s_emerging"], 4
        )
        it["sources"] = sorted(set(it["sources"]))

    ranked = sorted(cand.values(), key=lambda x: (-x["score"], x["name"]))
    for i, it in enumerate(ranked, 1):
        it["hot_rank"] = i

    # B 榜：BOSS 市场化岗位全集，剔除已进 A 榜的，无热度不排序
    in_a = {it["name"] for it in ranked}
    market_pool = [
        {"name": j["name"], "boss_l1": j["l1"], "boss_l2": j["l2"], "boss_code": j["code"],
         "heat": None, "heat_status": "pending_calibration"}
        for j in boss if j["name"] not in in_a
    ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "weights": {"shortage": W_SHORTAGE, "emerging": W_EMERGING},
        "sources": [
            {"id": "mohrss_shortage_2022Q4", "url": SRC_SHORTAGE_URL,
             "license": "政府公开信息", "note": "官方紧缺榜，2022Q4 后停更"},
            {"id": "boss_position_tree", "url": SRC_BOSS_URL,
             "license": "公开数据接口（不在 robots Disallow 内）",
             "note": "仅提供岗位名全集与领域标签，不参与热度打分"},
            {"id": "mohrss_new_occupation", "url": SRC_NEWJOB_2024_URL,
             "license": "政府公开信息", "note": "2024 批 19 个已全；2025 批部分"},
        ],
        "caveat": (
            "A 榜 score = 官方紧缺度 + 国家认定新兴度的合成信号，非真实招聘量；"
            "「缺工」反映供给缺口而非需求热度，故 A 榜偏传统行业属预期内。"
            "B 榜为市场化岗位全集，热度待登录后校准。"
        ),
        "total_ranked": len(ranked),
        "top": ranked[:top_n],
        "all_ranked": ranked,
        "market_pool_size": len(market_pool),
        "market_pool": market_pool,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=100)
    args = ap.parse_args()

    result = build(args.top)
    STAGING.mkdir(parents=True, exist_ok=True)
    out = STAGING / "hot_occupations_top100.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"A 榜（有官方依据、可排序）{result['total_ranked']} 个 → TOP{args.top}")
    print(f"B 榜（市场化岗位，待热度校准）{result['market_pool_size']} 个")
    print(f"落盘: {out}")
    print()
    for it in result["top"][:15]:
        tags = []
        if it["shortage_rank"]:
            tags.append(f"缺工#{it['shortage_rank']}")
        if it["emerging_type"]:
            tags.append("新职业" if it["emerging_type"] == "new_occupation" else "新工种")
        if it["boss_l1"]:
            tags.append(it["boss_l1"])
        print("  %3d. %-24s %.4f  %s" % (it["hot_rank"], it["name"], it["score"], " | ".join(tags)))


if __name__ == "__main__":
    main()
