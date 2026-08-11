"""重建行业业务编码 attrs.code：数字平台码 → 有意义的语义 slug。

背景
----
行业原编码来自 BOSS 平台（如 `100405`），对运营与前端都无可读性。
参考产品原型（零售电商=ecom、文化传媒=media、智能制造=iot、金融保险=fin、
教育培训=edu、医疗健康=health、游戏=game、汽车出行=auto、政企服务=gov），
改为英文语义 slug。

编码规则
--------
- 一级行业：单词 slug，如 `internet` / `finance` / `manufacture`
- 二级行业：`父slug-子slug`，如 `internet-ai` / `finance-bank`
  分层前缀让同名子类（多个「其他…」）天然不冲突，也一眼看出归属
- 字符集限定 `[a-z0-9-]`，便于放进 URL 与前端筛选参数

唯一性
------
写库前在内存里全量查重，冲突自动加序号后缀；落库走 patch 通道，
再由 `uq_kg_node_region_type_code` 唯一索引兜底。
原平台码保留在 `attrs.platform_code`，不丢溯源。

用法：
    python scripts/rebuild_industry_codes.py --dry-run
    python scripts/rebuild_industry_codes.py
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

from backend.kg.pg_store.client import connect  # noqa: E402

# 一级行业 → slug
L1 = {
    "互联网/AI": "internet",
    "金融": "finance",
    "制造业": "manufacture",
    "消费品/批发/零售": "retail",
    "医疗/制药": "medical",
    "制药/医疗": "medical",
    "教育培训": "edu",
    "房地产/建筑": "realestate",
    "汽车": "auto",
    "电子/通信/半导体": "electronics",
    "能源/化工/环保": "energy",
    "交通运输/物流": "logistics",
    "广告/传媒/文化/体育": "media",
    "专业服务": "proservice",
    "服务业": "service",
    "政府/非营利组织/其他": "gov",
}

# 二级行业 → 子 slug（不含父前缀）。按语义命名，参考原型的简短风格。
L2 = {
    # 互联网/AI
    "互联网": "web", "人工智能": "ai", "云计算": "cloud", "大数据": "bigdata",
    "信息安全": "security", "物联网": "iot", "企业服务": "saas", "电子商务": "ecom",
    "新零售": "newretail", "游戏": "game", "社交网络与媒体": "social",
    "医疗健康": "health", "在线教育": "onlineedu", "生活服务(O2O)": "o2o",
    "计算机软件": "software", "计算机硬件": "hardware", "计算机服务": "itservice",
    "电子/硬件开发": "hwdev", "智能硬件/消费电子": "smartdevice",
    # 金融
    "银行": "bank", "保险": "insurance", "证券/期货": "securities", "基金": "fund",
    "信托": "trust", "互联网金融": "fintech", "投资/融资": "investment",
    "财富管理": "wealth", "租赁/拍卖/典当/担保": "leasing", "其他金融业": "other",
    # 制造业
    "专用设备": "specialequip", "通用设备": "generalequip", "仪器仪表": "instrument",
    "自动化设备": "automation", "电气机械/器材": "electric", "金属制品": "metal",
    "橡胶/塑料制品": "plastic", "非金属矿物制品": "nonmetal", "新材料": "material",
    "化学原料/化学制品": "chemical", "印刷/包装/造纸": "printing",
    "服装/纺织": "textile", "家具/家居": "furniture", "家用电器": "appliance",
    "食品/饮料/烟酒": "food", "日化": "dailychem", "珠宝/首饰": "jewelry",
    "摩托车/自行车制造": "bike", "铁路/船舶/航空/航天制造": "transportequip",
    "计算机/通信/其他电子设备": "eledevice", "其他制造业": "other",
    # 消费品/零售
    "批发/零售": "wholesale", "进出口贸易": "trade", "其他消费品": "other",
    # 医疗
    "医疗器械": "device", "医疗服务": "service", "生物/制药": "pharma",
    "医疗研发外包": "cro", "医药批发零售": "distribution", "医美服务": "aesthetic",
    "IVD": "ivd",
    # 教育
    "学校/学历教育": "school", "学前教育": "preschool", "职业培训": "vocational",
    "培训/辅导机构": "tutoring", "学术/科研": "research",
    # 房地产/建筑
    "房地产开发经营": "development", "房地产中介/租赁": "agency",
    "房屋建筑工程": "building", "土木工程": "civil", "机电工程": "mep",
    "建筑设计": "design", "建筑材料": "material", "装修装饰": "decoration",
    "建筑工程咨询服务": "consulting", "物业管理": "property",
    "土地与公共设施管理": "landmgmt",
    # 汽车
    "汽车研发/制造": "manufacture", "汽车零部件": "parts", "新能源汽车": "nev",
    "汽车智能网联": "connected", "汽车经销商": "dealer", "汽车后市场": "aftermarket",
    # 电子/通信/半导体
    "半导体/芯片": "semiconductor", "通信/网络设备": "netequip",
    "运营商/增值服务": "carrier",
    # 能源/化工/环保
    "石油/石化": "oil", "化工": "chemical", "电力/热力/燃气/水利": "power",
    "光伏": "solar", "风电": "wind", "储能": "storage", "动力电池": "battery",
    "环保": "environment", "采掘/冶炼": "mining", "矿产/地质": "geology",
    "回收/维修": "recycle", "其他新能源": "othernew",
    # 交通运输/物流
    "快递": "express", "公路物流": "road", "跨境物流": "crossborder",
    "同城货运": "citydelivery", "即时配送": "instant",
    "港口/铁路/公路/机场": "hub", "装卸搬运和仓储业": "warehouse",
    "客运服务": "passenger",
    # 广告/传媒/文化/体育
    "广告/公关/会展": "advertising", "广告营销": "marketing",
    "广播/影视": "film", "新闻/出版": "publishing",
    "文化艺术/娱乐": "culture", "体育": "sports", "运动/健身": "fitness",
    # 专业服务
    "咨询": "consulting", "法律": "legal", "财务/审计/税务": "finance",
    "人力资源服务": "hr", "翻译": "translation",
    "检测/认证/知识产权": "certification", "其他专业服务": "other",
    # 服务业
    "餐饮": "catering", "酒店/民宿": "hotel", "旅游/景区": "travel",
    "休闲/娱乐": "leisure", "美容": "beauty", "美发": "hairdressing",
    "保健/养生": "wellness", "家政服务": "housekeeping", "宠物服务": "pet",
    "婚庆/摄影": "wedding", "其他生活服务": "other",
    # 政府/其他
    "政府/公共事业": "public", "非营利组织": "nonprofit",
    "农/林/牧/渔": "agriculture", "其他行业": "other",
}


def slugify_fallback(name: str) -> str:
    """无人工映射时的兜底：ASCII 直接用，中文取拼音首字母。"""
    s = re.sub(r"[^0-9A-Za-z]+", "-", name).strip("-").lower()
    if s:
        return s
    try:
        from pypinyin import lazy_pinyin

        return "".join(x[0] for x in lazy_pinyin(name) if x)[:12] or "x"
    except Exception:
        return "x" + str(abs(hash(name)) % 10000)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, attrs FROM kg_node WHERE type='industry' "
            "AND COALESCE(status,'published') <> 'archived' ORDER BY name"
        ).fetchall()
        nodes = []
        for r in rows:
            try:
                a = json.loads(r["attrs"]) if isinstance(r["attrs"], str) else (r["attrs"] or {})
            except Exception:
                a = {}
            nodes.append({"id": r["id"], "name": r["name"], "attrs": a})

        used: set[str] = set()
        plan: list[dict] = []
        unmapped: list[str] = []

        # 先分配一级，二级要用父 slug 做前缀
        for n in nodes:
            if str(n["attrs"].get("level") or "") == "1":
                slug = L1.get(n["name"]) or slugify_fallback(n["name"])
                n["slug"] = slug
        l1_by_name = {
            n["name"]: n["slug"] for n in nodes if str(n["attrs"].get("level") or "") == "1"
        }

        for n in nodes:
            lv = str(n["attrs"].get("level") or "")
            if lv == "1":
                code = n["slug"]
            else:
                parent = n["attrs"].get("parent_name") or ""
                p = l1_by_name.get(parent) or L1.get(parent) or slugify_fallback(parent)
                child = L2.get(n["name"])
                if not child:
                    unmapped.append(n["name"])
                    child = slugify_fallback(n["name"])
                code = f"{p}-{child}"
            # 去重：同 slug 加序号（多个「其他…」已被父前缀区分，这里是最后兜底）
            base, i = code, 2
            while code in used:
                code = f"{base}-{i}"
                i += 1
            used.add(code)
            plan.append({**n, "new_code": code, "old_code": n["attrs"].get("code")})

    print(f"行业 {len(plan)} 个 · 生成唯一编码 {len(used)} 个 · 未命中人工映射 {len(unmapped)} 个")
    if unmapped:
        print("  兜底命名（建议补进 L2 表）:", unmapped[:12])
    print("\n样例：")
    for p in plan[:6] + plan[-4:]:
        print(f"  {p['name']:26s} {str(p['old_code']):10s} → {p['new_code']}")

    if args.dry_run:
        print("\n[dry-run] 未写库")
        return 0

    with connect() as conn:
        updated = 0
        for p in plan:
            a = dict(p["attrs"])
            if a.get("code") and a.get("code") != p["new_code"]:
                a["platform_code"] = a["code"]      # 保留原平台码，不丢溯源
            a["code"] = p["new_code"]
            conn.execute(
                "UPDATE kg_node SET attrs=%s WHERE id=%s",
                (json.dumps(a, ensure_ascii=False), p["id"]),
            )
            updated += 1
        print(f"\n已重建 {updated} 个行业的编码（原码存入 attrs.platform_code）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
