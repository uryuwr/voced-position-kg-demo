"""技术栈官方文档 → 可学资源（course）。

为什么用映射表而不是爬搜索
--------------------------
慕课平台对现代技术栈覆盖极差（实测 552 个技术技能里 448 个没有真实课程：
`Spring` / `Docker` / `Kubernetes` 在中国大学MOOC 命中 0 门），而 B站/抖音的搜索
被 robots 明确禁止。官方文档是剩下的合法且权威的来源：

- **不需要爬取**：URL 是公开且长期稳定的，本脚本只做「引用 + 可访问性验证」
- **权威**：官方维护，不存在广告/劣质内容问题，故 `confidence=official`
- **免费可读**：符合本体对 course 的定义（可点开学习的资源）

⚠️ **绝不能凭记忆写 URL 就入库** —— 文档站会改版（Vue 2→3、Angular 换域名、
Epic 迁移文档中心）。所以映射表只是候选，每条都必须过 `verify` 阶段：
HTTP 200 + 页面标题/正文出现预期关键词，才允许落库。

匹配方式
--------
按技能名里的技术栈**关键词**匹配（大小写不敏感）。一个技能可命中多个技术栈
（`HTML/CSS开发` → MDN；`K8s容器编排` → Kubernetes），取全部。
匹配不到的（`3GPP标准参与`、`MBIST设计`、`EMC/EMI设计` 这类通信/硬件标准）
本脚本不处理 —— 它们有行业规范但没有教程性质的官方文档，硬凑只会造脏数据。

用法::

    python -m crawlers.cn.link_official_docs --stage verify          # 只验 URL，不写库
    python -m crawlers.cn.link_official_docs --stage apply --l1 技术 --dry-run
    python -m crawlers.cn.link_official_docs --stage apply --l1 技术
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, upsert_edges, upsert_nodes
from backend.kg.paths import REPORTS, STAGING
from backend.kg.provenance import make_edge_id, make_node_id, utc_now_iso

REGION = "CN"
SRC_COURSE = "OFFICIAL_DOCS"
SRC_LINK = "LINK_CN_RULE"
LICENSE = "官方文档公开页面（仅引用链接，未抓取正文）"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 voced-kg-research/0.1 (educational research)"
)
OUT = STAGING / "official_docs_verified.json"

# 技术栈 → 官方文档。`kw` 是匹配技能名用的关键词（小写比较），
# `expect` 是验证页面时必须出现的词（防止改版后 URL 指向无关页面）。
# 优先中文文档站，学员可读性更好。
DOCS: list[dict] = [
    # —— 前端 ——
    {"kw": ["react"], "name": "React 官方文档", "org": "Meta",
     "url": "https://zh-hans.react.dev/learn", "expect": ["React"]},
    {"kw": ["vue"], "name": "Vue.js 官方文档", "org": "Vue.js",
     "url": "https://cn.vuejs.org/guide/introduction.html", "expect": ["Vue"]},
    {"kw": ["typescript", "ts开发"], "name": "TypeScript 官方手册", "org": "Microsoft",
     "url": "https://www.typescriptlang.org/docs/", "expect": ["TypeScript"]},
    {"kw": ["html", "css", "es6", "javascript", "js核心", "dom", "bom"],
     "name": "MDN Web 开发技术文档", "org": "Mozilla",
     "url": "https://developer.mozilla.org/zh-CN/docs/Web", "expect": ["Web", "MDN"]},
    {"kw": ["angular"], "name": "Angular 官方文档", "org": "Google",
     "url": "https://angular.dev/overview", "expect": ["Angular"]},
    {"kw": ["webpack"], "name": "webpack 中文文档", "org": "webpack",
     "url": "https://webpack.docschina.org/concepts/", "expect": ["webpack"]},
    {"kw": ["vite"], "name": "Vite 中文文档", "org": "Vite",
     "url": "https://cn.vite.dev/guide/", "expect": ["Vite"]},
    {"kw": ["小程序"], "name": "微信小程序开发文档", "org": "腾讯",
     "url": "https://developers.weixin.qq.com/miniprogram/dev/framework/",
     "expect": ["小程序"]},
    # —— 后端 ——
    {"kw": ["spring"], "name": "Spring Boot 官方文档", "org": "VMware",
     "url": "https://docs.spring.io/spring-boot/index.html", "expect": ["Spring Boot"]},
    {"kw": ["django"], "name": "Django 中文文档", "org": "Django 软件基金会",
     "url": "https://docs.djangoproject.com/zh-hans/stable/", "expect": ["Django"]},
    {"kw": ["flask"], "name": "Flask 官方文档", "org": "Pallets",
     "url": "https://flask.palletsprojects.com/", "expect": ["Flask"]},
    {"kw": ["node", "nodejs"], "name": "Node.js 官方文档", "org": "OpenJS 基金会",
     "url": "https://nodejs.org/docs/latest/api/", "expect": ["Node.js"]},
    {"kw": ["golang", "go语言", "go开发"], "name": "Go 官方文档", "org": "Google",
     "url": "https://go.dev/doc/", "expect": ["Go"]},
    {"kw": ["rust"], "name": "Rust 程序设计语言（中文）", "org": "Rust 基金会",
     "url": "https://kaisery.github.io/trpl-zh-cn/", "expect": ["Rust"]},
    {"kw": ["php"], "name": "PHP 中文手册", "org": "PHP Group",
     "url": "https://www.php.net/manual/zh/", "expect": ["PHP"]},
    {"kw": ["kotlin"], "name": "Kotlin 官方文档", "org": "JetBrains",
     "url": "https://kotlinlang.org/docs/home.html", "expect": ["Kotlin"]},
    {"kw": [".net", "c#", "asp.net", "ef core", "linq"],
     "name": ".NET 官方文档（中文）", "org": "Microsoft",
     "url": "https://learn.microsoft.com/zh-cn/dotnet/", "expect": [".NET"]},
    {"kw": ["python"], "name": "Python 官方文档（中文）", "org": "PSF",
     "url": "https://docs.python.org/zh-cn/3/", "expect": ["Python"]},
    # C++：cppreference 与 MySQL 官方手册均对本 UA 返回 403，不收录。
    # 硬塞会得到一个打不开的链接 —— 宁可缺资源，也不给学员死链。
    # —— 数据库 ——
    {"kw": ["postgresql", "postgres", "pgsql"], "name": "PostgreSQL 官方文档", "org": "PGDG",
     "url": "https://www.postgresql.org/docs/current/", "expect": ["PostgreSQL"]},
    {"kw": ["redis"], "name": "Redis 官方文档", "org": "Redis Ltd.",
     "url": "https://redis.io/docs/latest/", "expect": ["Redis"]},
    {"kw": ["mongodb", "mongo"], "name": "MongoDB 官方文档（中文）", "org": "MongoDB",
     "url": "https://www.mongodb.com/zh-cn/docs/manual/", "expect": ["MongoDB"]},
    {"kw": ["elasticsearch", "es检索"], "name": "Elasticsearch 官方指南", "org": "Elastic",
     "url": "https://www.elastic.co/guide/en/elasticsearch/reference/current/index.html",
     "expect": ["Elasticsearch"]},
    # —— 运维 / 云原生 ——
    {"kw": ["docker", "容器化"], "name": "Docker 官方文档", "org": "Docker Inc.",
     "url": "https://docs.docker.com/", "expect": ["Docker"]},
    {"kw": ["k8s", "kubernetes", "容器编排"], "name": "Kubernetes 官方文档（中文）", "org": "CNCF",
     "url": "https://kubernetes.io/zh-cn/docs/home/", "expect": ["Kubernetes"]},
    {"kw": ["git"], "name": "Pro Git（中文版）", "org": "Git",
     "url": "https://git-scm.com/book/zh/v2", "expect": ["Git"]},
    {"kw": ["nginx"], "name": "nginx 官方文档", "org": "nginx",
     "url": "https://nginx.org/en/docs/", "expect": ["nginx"]},
    {"kw": ["jenkins", "ci/cd", "cicd", "流水线"], "name": "Jenkins 官方文档（中文）", "org": "Jenkins",
     "url": "https://www.jenkins.io/zh/doc/", "expect": ["Jenkins"]},
    {"kw": ["prometheus", "监控告警"], "name": "Prometheus 官方文档", "org": "CNCF",
     "url": "https://prometheus.io/docs/introduction/overview/", "expect": ["Prometheus"]},
    {"kw": ["linux"], "name": "Linux man 手册页", "org": "man7",
     "url": "https://man7.org/linux/man-pages/", "expect": ["man", "Linux"]},
    # —— AI / 大数据 ——
    # PyTorch：HTTP 200 但返回的是 "Client Challenge" 人机校验页 —— 只验状态码
    # 会把校验页当文档入库，关键词校验拦下了它。不收录。
    # TensorFlow / Android：Google 系文档站 302 按地区分流，国内镜像
    # （tensorflow.google.cn / developer.android.google.cn）SSL 握手失败。不收录。
    {"kw": ["pandas"], "name": "pandas 官方文档", "org": "NumFOCUS",
     "url": "https://pandas.pydata.org/docs/", "expect": ["pandas"]},
    {"kw": ["numpy"], "name": "NumPy 官方文档", "org": "NumFOCUS",
     "url": "https://numpy.org/doc/stable/", "expect": ["NumPy"]},
    {"kw": ["spark"], "name": "Apache Spark 官方文档", "org": "ASF",
     "url": "https://spark.apache.org/docs/latest/", "expect": ["Spark"]},
    {"kw": ["flink"], "name": "Apache Flink 官方文档（中文）", "org": "ASF",
     "url": "https://nightlies.apache.org/flink/flink-docs-master/zh/", "expect": ["Flink"]},
    {"kw": ["hadoop", "hdfs"], "name": "Apache Hadoop 官方文档", "org": "ASF",
     "url": "https://hadoop.apache.org/docs/stable/", "expect": ["Hadoop"]},
    {"kw": ["kafka"], "name": "Apache Kafka 官方文档", "org": "ASF",
     "url": "https://kafka.apache.org/documentation/", "expect": ["Kafka"]},
    # —— 移动 / 客户端 ——
    {"kw": ["ios", "swift", "objective-c", "swiftui"], "name": "Apple 开发者文档", "org": "Apple",
     "url": "https://developer.apple.com/documentation/", "expect": ["Documentation", "Apple"]},
    {"kw": ["flutter", "dart"], "name": "Flutter 官方文档", "org": "Google",
     "url": "https://docs.flutter.dev/", "expect": ["Flutter"]},
    {"kw": ["harmony", "鸿蒙", "arkts", "arkui", "deveco"],
     "name": "HarmonyOS 开发者文档", "org": "华为",
     # document-outline 那个入口是 JS 渲染（标题只有「文档中心」，验不出内容），换开发者主站
     "url": "https://developer.huawei.com/consumer/cn/develop/",
     "expect": ["HarmonyOS", "鸿蒙"]},
    # —— 测试 / 工具 ——
    {"kw": ["jmeter"], "name": "Apache JMeter 用户手册", "org": "ASF",
     "url": "https://jmeter.apache.org/usermanual/index.html", "expect": ["JMeter"]},
    {"kw": ["selenium", "自动化测试"], "name": "Selenium 官方文档（中文）", "org": "Selenium",
     "url": "https://www.selenium.dev/zh-cn/documentation/", "expect": ["Selenium"]},
    {"kw": ["postman", "api接口"], "name": "Postman 学习中心", "org": "Postman",
     "url": "https://learning.postman.com/docs/introduction/overview/", "expect": ["Postman"]},
    # —— 游戏 / 图形 ——
    {"kw": ["unity", "u3d"], "name": "Unity 官方手册（中文）", "org": "Unity",
     "url": "https://docs.unity3d.com/cn/current/Manual/index.html", "expect": ["Unity"]},
    {"kw": ["unreal", "ue4", "ue5"], "name": "虚幻引擎官方文档", "org": "Epic Games",
     "url": "https://dev.epicgames.com/documentation/zh-cn/unreal-engine",
     "expect": ["虚幻", "Unreal"]},
    {"kw": ["cocos"], "name": "Cocos Creator 官方手册", "org": "Cocos",
     "url": "https://docs.cocos.com/creator/manual/zh/", "expect": ["Cocos"]},
]


def slug(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z一-龥]+", "_", s).strip("_")[:40]


def fetch(url: str, *, timeout: int = 25) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(200_000).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def stage_verify(*, sleep: float) -> dict:
    """逐条验证映射表里的 URL —— 记忆里的地址会过时，必须实测。"""
    ok, bad = [], []
    for d in DOCS:
        status, html = fetch(d["url"])
        hit = any(k.lower() in html.lower() for k in d["expect"]) if html else False
        rec = {**{k: d[k] for k in ("name", "org", "url", "kw")},
               "status": status, "keyword_hit": hit}
        (ok if (status == 200 and hit) else bad).append(rec)
        time.sleep(sleep)
    STAGING.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"generated_at": utc_now_iso(), "verified": ok, "failed": bad},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    return {"stage": "verify", "total": len(DOCS), "passed": len(ok), "failed": len(bad),
            "failed_detail": [{"name": b["name"], "url": b["url"],
                               "status": b["status"], "keyword_hit": b["keyword_hit"]}
                              for b in bad],
            "out": str(OUT)}


def _kw_hit(kw: str, skill_low: str) -> bool:
    """关键词匹配要带边界 —— 裸 `in` 会命中子串。

    实测踩坑：关键词 `go开发` 命中了 `Django开发`（Djan-**go开发**），
    于是 Django 技能挂上了《Go 官方文档》。同理 `ios` 会命中 `Studios`、
    `es6` 会命中 `Rules6`。

    英文/数字开头的关键词要求前后不是字母数字；纯中文关键词不加边界
    （中文没有词边界，且「容器编排」这类本来就是整词）。
    """
    kw = kw.lower()
    if re.match(r"^[a-z0-9]", kw):
        # 关键词里的 . + # / 在正则里要转义
        return re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", skill_low) is not None
    return kw in skill_low


def match_docs(skill_key: str, verified: list[dict]) -> list[dict]:
    low = (skill_key or "").lower()
    return [d for d in verified if any(_kw_hit(k, low) for k in d["kw"])]


def stage_apply(*, l1: str, dry_run: bool) -> dict:
    if not OUT.exists():
        return {"error": f"缺 {OUT}，先跑 --stage verify"}
    verified = json.loads(OUT.read_text(encoding="utf-8"))["verified"]
    if not verified:
        return {"error": "verify 没有任何通过项，拒绝入库"}
    fetched_at = utc_now_iso()

    conn = connect()
    try:
        # 目标：该门类岗位涉及的技能，且尚无真实课程的
        rows = conn.execute(
            """
            SELECT DISTINCT n.id AS sid, n.attrs AS attrs
            FROM nodes o
            JOIN edges e ON e.src_id = o.id AND e.rel_type = 'requires'
            JOIN nodes n ON n.id = e.dst_id AND n.type = 'skill_level'
            WHERE o.type = 'occupation' AND o.source_system = 'BOSS'
            """
        ).fetchall()
        has_real = {r[0] for r in conn.execute(
            """SELECT DISTINCT e.src_id FROM edges e JOIN nodes c ON c.id = e.dst_id
               WHERE e.rel_type='taught_by'
                 AND c.source_system IN ('ICOURSE163','XUETANGX')""")}

        new_nodes, edges = {}, []
        matched_skills = set()
        for sid, attrs in rows:
            if sid in has_real:
                continue
            try:
                a = json.loads(attrs or "{}")
            except Exception:
                a = {}
            key, lv = a.get("skill_key"), a.get("level")
            if not key:
                continue
            try:
                lv = int(lv)
            except (TypeError, ValueError):
                continue
            if lv != 3:  # 只挂 L3（熟练）这一档，与慕课课程保持一致
                continue
            for d in match_docs(key, verified):
                cid = make_node_id(REGION, "course", SRC_COURSE, slug(d["name"]))
                new_nodes.setdefault(cid, {
                    "id": cid, "region": REGION, "type": "course",
                    "name": d["name"], "name_en": None, "name_zh": d["name"],
                    "aliases": None,
                    "description": f"{d['name']}（{d['org']} 官方维护）—— 权威、免费、持续更新",
                    "attrs": json.dumps({"role": "learnable_resource", "playable": True,
                                         "match_method": "official_docs",
                                         "org": d["org"], "doc_type": "official_documentation"},
                                        ensure_ascii=False),
                    "source_system": SRC_COURSE, "source_id": slug(d["name"]),
                    "source_url": d["url"], "license": LICENSE,
                    "fetched_at": fetched_at, "confidence": "official",
                })
                edges.append({
                    "id": make_edge_id(sid, "taught_by", cid),
                    "src_id": sid, "dst_id": cid, "rel_type": "taught_by",
                    "region": REGION, "weight": 0.85,
                    "evidence": (f"技能「{key}」对应技术栈官方文档《{d['name']}》"
                                 f"（{d['org']}）；慕课平台无该技术栈课程时的权威替代"),
                    "attrs": json.dumps({"match_method": "official_docs",
                                         "skill_key": key, "org": d["org"]},
                                        ensure_ascii=False),
                    "source_system": SRC_LINK, "source_id": f"{sid}->{cid}",
                    "source_url": d["url"], "license": LICENSE,
                    "fetched_at": fetched_at, "confidence": "official",
                })
                matched_skills.add(key)

        edges = list({e["id"]: e for e in edges}.values())
        rep = {"stage": "apply", "l1": l1, "dry_run": dry_run,
               "verified_docs": len(verified),
               "skills_matched": len(matched_skills),
               "new_course_nodes": len(new_nodes), "taught_by_edges": len(edges),
               "sample_skills": sorted(matched_skills)[:20]}
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
    ap.add_argument("--stage", required=True, choices=("verify", "apply"))
    ap.add_argument("--l1", default="技术")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.5)
    args = ap.parse_args()

    rep = (stage_verify(sleep=args.sleep) if args.stage == "verify"
           else stage_apply(l1=args.l1, dry_run=args.dry_run))
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"official_docs_{args.stage}.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=2)[:2500])


if __name__ == "__main__":
    main()
