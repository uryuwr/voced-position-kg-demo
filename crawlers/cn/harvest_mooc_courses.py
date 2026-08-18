"""按技能名采集中国大学MOOC 的**真实课程**，替换检索落地页。

为什么是这个源，不是 B站/抖音
----------------------------
2026-08-17 实测各平台 robots.txt：

    search.bilibili.com   User-agent: * → Disallow: /   （白名单制，只放行 9 家搜索引擎）
    api.bilibili.com      Disallow: /                    （API 域全禁）
    douyin.com            Disallow: /video/ /search/ /note/ *general_search*
    icourse163.org        **无 robots.txt**（HTTP 404）→ 未设限
    xuetangx.com          无 robots.txt → 未设限

所以「抓 B站高播放视频」这条路被 robots 挡死 —— 而恰恰是搜索接口（拿播放量必经）
被禁。用真浏览器渲染也不改变性质：`User-agent: *` 管的是所有自动化访问者。

中国大学MOOC 反而更契合筛选标准：
- **选课人数**（`learnerCount`）= 你要的"观看人数足够多"，如 Java程序设计 96.8 万人
- 院校开课、平台审核 → 天然满足"非广告、高质量"
- 有 `schoolName` / `imgUrl` / 课程 id，可直达课程页

合规做法
--------
- 站点无 robots.txt，未设抓取限制
- `csrfKey` 从首页 Cookie(`NTESSTUDYSI`) 取，是**站点正常会话流程**，不是逆向反爬
- 单线程 + 请求间隔限速；UA 标明研究用途
- 每条课程记录 `source_url` + `fetched_at` + `learnerCount`（质量依据可追溯）

质量门槛
--------
`--min-learners`（默认 3000）：低于此选课人数的不收，避免把冷门/空课挂上去。

用法::

    python -m crawlers.cn.harvest_mooc_courses --stage fetch --l1 技术 --limit 20
    python -m crawlers.cn.harvest_mooc_courses --stage fetch --l1 技术
    python -m crawlers.cn.harvest_mooc_courses --stage apply --l1 技术 --dry-run
    python -m crawlers.cn.harvest_mooc_courses --stage apply --l1 技术
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, upsert_edges, upsert_nodes
from backend.kg.paths import REPORTS, STAGING
from backend.kg.provenance import make_edge_id, make_node_id, utc_now_iso

REGION = "CN"
SRC_COURSE = "ICOURSE163"
SRC_LINK = "LINK_CN_RULE"
HOME = "https://www.icourse163.org/"
RPC = "https://www.icourse163.org/web/j/mocSearchBean.searchCourse.rpc?csrfKey="
# 课程页 URL 必须是 {学校短名}-{课程数字id}，如 /course/PKU-1001663016。
# ⚠ 别用返回里的 `shortName`（形如 csharp003 / 0802NWPU008）—— 那是课程内部编号，
#   拼进 URL 会 404「您无权访问此页面」。学校短名在 schoolPanel.shortName。
COURSE_URL = "https://www.icourse163.org/course/{school_short}-{course_id}"
LICENSE = "中国大学MOOC 公开课程信息（站点无 robots 限制）"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 voced-kg-research/0.1 (educational research)"
)
OUT = STAGING / "mooc_courses.json"


def slug(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z一-龥]+", "_", s).strip("_")[:40]


class MoocClient:
    """带 Cookie 会话的最小客户端 —— csrfKey 必须与 Cookie 同源，否则回「非法跨域请求」。"""

    def __init__(self) -> None:
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.csrf: str | None = None

    def open_session(self) -> str | None:
        req = urllib.request.Request(HOME, headers={"User-Agent": UA})
        with self.opener.open(req, timeout=25) as r:
            r.read(2048)
        for ck in self.jar:
            if ck.name == "NTESSTUDYSI":
                self.csrf = ck.value
                break
        return self.csrf

    def search(self, keyword: str, *, page_size: int = 8) -> list[dict]:
        if not self.csrf:
            self.open_session()
        body = urllib.parse.urlencode({
            "mocCourseQueryVo": json.dumps(
                {"keyword": keyword, "pageIndex": 1, "highlight": True,
                 "orderBy": 0, "stats": 30, "pageSize": page_size},
                ensure_ascii=False),
        }).encode()
        req = urllib.request.Request(
            RPC + (self.csrf or ""), data=body,
            headers={"User-Agent": UA, "Referer": HOME,
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        with self.opener.open(req, timeout=25) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("code") != 0:
            raise RuntimeError(f"code={d.get('code')} msg={d.get('message')}")
        out = []
        for it in (d.get("result") or {}).get("list") or []:
            c = ((it.get("mocCourseCard") or {}).get("mocCourseCardDto")) or {}
            if not c.get("name"):
                continue
            panel = c.get("schoolPanel") or {}
            out.append({
                "course_id": c.get("id"),
                "short_name": c.get("shortName"),          # 仅留档，**不要拼 URL**
                "school_short": panel.get("shortName"),    # 拼 URL 用这个
                "name": re.sub(r"</?em>", "", str(c.get("name"))).strip(),
                "school": panel.get("name") or c.get("schoolName"),
                "learners": c.get("learnerCount") or 0,
                "img": c.get("imgUrl") or panel.get("imgUrl"),
            })
        return out


def skill_keys(l1: str) -> list[str]:
    raw = STAGING / f"boss_skill_raw_{slug(l1)}.json"
    if not raw.exists():
        raise SystemExit(f"缺 {raw}，先跑 link_boss_skill_chain --stage collect")
    items = json.loads(raw.read_text(encoding="utf-8"))["items"]
    cmap = STAGING / f"boss_skill_canon_{slug(l1)}.json"
    mapping = json.loads(cmap.read_text(encoding="utf-8"))["mapping"] if cmap.exists() else {}
    return sorted({mapping.get(s["name"], s["name"]) for r in items for s in r["skills"]})


# 技能名里的通用词 —— 用它们做匹配会把任何课程都判成相关。
# 实测教训：技能「SpringBoot开发」因为共享「开发」二字，匹配到了
# 《Linux开发环境及应用》《Python游戏开发入门》《Web前端开发》。
_STOP_TOKENS = {
    "开发", "设计", "应用", "管理", "优化", "使用", "编程", "技术", "基础", "进阶",
    "调试", "运维", "测试", "分析", "处理", "系统", "平台", "工具", "项目", "实现",
    "配置", "部署", "维护", "搭建", "编写", "操作", "原理", "方法", "流程", "规范",
    "能力", "实践", "入门", "高级", "初级", "中级", "综合", "实训", "实验", "案例",
}


def is_relevant(skill_key: str, query: str, course_name: str) -> bool:
    """课程名必须与技能**实质相关** —— 只看选课人数会收进一堆无关热门课。

    两条教训都来自实测：

    1. 技能「3D图形渲染」退化查询成「3D」、「3GPP标准参与」退化成「3GPP」，
       搜索没命中时返回按人数排序的热门课，两者都匹配到《猴博士高数不挂科》
       （92 万人选课）—— 人数门槛挡不住错配。
    2. 只要共享 ≥2 字的词片就算相关，则「SpringBoot开发」会匹配
       《Linux开发环境及应用》—— 因为都有「开发」。故通用词必须排除。

    规则：命中完整查询词，或命中技能名里**有辨识度**的词
    （英文/数字串 ≥2 字符，或中文 ≥2 字且不在停用词表内）。
    """
    cn = (course_name or "").lower()
    q = (query or "").strip().lower()
    if q and len(q) >= 2 and q in cn:
        return True
    for t in re.findall(r"[A-Za-z][A-Za-z+#./0-9]{1,}|[一-龥]{2,}", skill_key or ""):
        if t in _STOP_TOKENS:
            continue
        if len(t) >= 2 and t.lower() in cn:
            return True
    return False


def query_variants(key: str) -> list[str]:
    """技能名直接搜命中率低（"SpringBoot开发"搜不到），退化成核心词再搜。"""
    base = re.sub(r"(开发|设计|应用|编程|使用|优化|管理|基础|技术|调试|运维|测试)+$", "", key).strip()
    out = [key]
    if base and base != key:
        out.append(base)
    m = re.match(r"^([A-Za-z][A-Za-z+#./0-9]*)", key)
    if m and m.group(1) not in out:
        out.append(m.group(1))
    return out[:3]


def stage_fetch(*, l1: str, limit: int | None, sleep: float, min_learners: int) -> dict:
    keys = skill_keys(l1)
    if limit:
        keys = keys[:limit]
    STAGING.mkdir(parents=True, exist_ok=True)
    done: dict[str, list[dict]] = {}
    if OUT.exists():
        done = json.loads(OUT.read_text(encoding="utf-8")).get("items", {})

    cli = MoocClient()
    cli.open_session()
    failed, empty = [], []
    todo = [k for k in keys if k not in done]
    for i, key in enumerate(todo, 1):
        hits: list[dict] = []
        for q in query_variants(key):
            try:
                got = cli.search(q)
            except Exception as e:
                failed.append({"skill": key, "query": q, "error": str(e)[:100]})
                time.sleep(max(sleep, 1.0))
                continue
            # 两道门槛都要过：① 选课人数（质量）② 名称相关性（避免收进无关热门课）
            hits = [
                c for c in got
                if (c["learners"] or 0) >= min_learners
                and is_relevant(key, q, c["name"])
            ]
            if hits:
                for c in hits:
                    c["matched_query"] = q
                break
            time.sleep(sleep)
        if hits:
            done[key] = sorted(hits, key=lambda x: -(x["learners"] or 0))[:3]
        else:
            empty.append(key)
            done[key] = []
        if i % 20 == 0:
            OUT.write_text(json.dumps({"generated_at": utc_now_iso(), "min_learners": min_learners,
                                       "items": done}, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(sleep)

    OUT.write_text(json.dumps({"generated_at": utc_now_iso(), "min_learners": min_learners,
                               "items": done}, ensure_ascii=False, indent=2), encoding="utf-8")
    hit_n = sum(1 for v in done.values() if v)
    return {"stage": "fetch", "l1": l1, "skills": len(keys), "with_course": hit_n,
            "no_course": len(keys) - hit_n, "min_learners": min_learners,
            "failed": failed[:8], "empty_sample": empty[:12], "out": str(OUT),
            "sample": {k: [(c["name"], c["learners"]) for c in v]
                       for k, v in list(done.items())[:5] if v}}


def stage_apply(*, l1: str, dry_run: bool) -> dict:
    if not OUT.exists():
        return {"error": f"缺 {OUT}，先跑 --stage fetch"}
    data = json.loads(OUT.read_text(encoding="utf-8"))
    items = data["items"]
    fetched_at = utc_now_iso()

    conn = connect()
    try:
        # 技能 key → 各档 node_id
        nodes_by_key: dict[str, dict[int, str]] = {}
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
                nodes_by_key.setdefault(k, {})[lv] = nid

        new_nodes, edges, replaced = {}, [], 0
        for key, courses in items.items():
            if not courses:
                continue
            levels = nodes_by_key.get(key) or {}
            anchor = levels.get(3) or (levels.get(max(levels)) if levels else None)
            if not anchor:
                continue
            for c in courses:
                # URL 只认 {学校短名}-{课程id}；缺学校短名就没法生成可访问链接，跳过
                school_short, course_id = c.get("school_short"), c.get("course_id")
                if not school_short or not course_id:
                    continue
                ident = f"{school_short}-{course_id}"
                cid = make_node_id(REGION, "course", SRC_COURSE, ident)
                url = COURSE_URL.format(school_short=school_short, course_id=course_id)
                new_nodes.setdefault(cid, {
                    "id": cid, "region": REGION, "type": "course",
                    "name": c["name"], "name_en": None, "name_zh": c["name"],
                    "aliases": None,
                    "description": (f"{c['name']}"
                                    + (f"（{c['school']}）" if c.get("school") else "")
                                    + f" · 中国大学MOOC 选课 {c['learners']} 人"),
                    "attrs": json.dumps({"role": "learnable_resource", "playable": True,
                                         "match_method": "mooc_search",
                                         "learner_count": c["learners"],
                                         "school": c.get("school"),
                                         "img_url": c.get("img"),
                                         "skill_key": key,
                                         "matched_query": c.get("matched_query")},
                                        ensure_ascii=False),
                    "source_system": SRC_COURSE, "source_id": ident,
                    "source_url": url, "license": LICENSE,
                    "fetched_at": fetched_at, "confidence": "official",
                })
                edges.append({
                    "id": make_edge_id(anchor, "taught_by", cid),
                    "src_id": anchor, "dst_id": cid, "rel_type": "taught_by",
                    "region": REGION, "weight": 0.9,
                    "evidence": (f"技能「{key}」→ 中国大学MOOC《{c['name']}》"
                                 + (f"（{c['school']}）" if c.get("school") else "")
                                 + f"，选课 {c['learners']} 人（≥{data['min_learners']} 门槛）"),
                    "attrs": json.dumps({"match_method": "mooc_search",
                                         "skill_key": key,
                                         "learner_count": c["learners"]}, ensure_ascii=False),
                    "source_system": SRC_LINK, "source_id": f"{anchor}->{cid}",
                    "source_url": url, "license": LICENSE,
                    "fetched_at": fetched_at, "confidence": "official",
                })
            # 有真课程了，撤掉该技能的检索落地页边
            for lv, nid in levels.items():
                cur = conn.execute(
                    """SELECT e.id FROM edges e JOIN nodes c ON c.id = e.dst_id
                       WHERE e.src_id=? AND e.rel_type='taught_by'
                         AND c.source_system='SEARCH_LANDING_CN'""", (nid,))
                for (eid,) in cur.fetchall():
                    if not dry_run:
                        conn.execute("DELETE FROM edges WHERE id=?", (eid,))
                    replaced += 1

        rep = {"stage": "apply", "l1": l1, "dry_run": dry_run,
               "skills_with_course": sum(1 for v in items.values() if v),
               "new_course_nodes": len(new_nodes), "taught_by_edges": len(edges),
               "search_landing_edges_removed": replaced}
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
    ap.add_argument("--stage", required=True, choices=("fetch", "apply"))
    ap.add_argument("--l1", default="技术")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--sleep", type=float, default=0.8, help="请求间隔（限速，别调太小）")
    ap.add_argument("--min-learners", type=int, default=3000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rep = (stage_fetch(l1=args.l1, limit=args.limit, sleep=args.sleep,
                       min_learners=args.min_learners)
           if args.stage == "fetch" else stage_apply(l1=args.l1, dry_run=args.dry_run))
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"mooc_{args.stage}_{slug(args.l1)}.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=2)[:2200])


if __name__ == "__main__":
    main()
