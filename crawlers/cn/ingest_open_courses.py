"""
入库「可点开学习」的课程资源（带 source_url）。

主源：国家职业教育智慧教育平台首页公开接口
  POST https://vocational.smartedu.cn/gjzyjy/index/getData

规则（docs/课程资源定义.md）：
  - attrs.role = learnable_resource
  - source_url 必须可访问（外链）
  - 与课标课目（curriculum_catalog）区分

Usage:
  python -m crawlers.cn.ingest_open_courses --dry-run
  python -m crawlers.cn.ingest_open_courses
  python -m backend.kg.neo4j_store.migrate
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect, stats, upsert_nodes
from backend.kg.paths import REPORTS, ensure_dirs
from backend.kg.provenance import make_node_id, utc_now_iso

SOURCE_SYSTEM = "SMART_EDU_VOC"
REGION = "CN"
LICENSE = "国家职业教育智慧教育平台公开资源；使用时请注明出处（vocational.smartedu.cn）"
API = "https://vocational.smartedu.cn/gjzyjy/index/getData"
HOME = "https://vocational.smartedu.cn/"

# 首页分区 → 资源类型
BUCKET_TYPE = {
    "index_xnfzk": "virtual_sim",  # 虚拟仿真
    "index_jsnl": "video_course",  # 教师能力/课程
    "index_spgkk": "video_course",  # 视频公开课
    "index_jck": "textbook_page",  # 教材详情页（可打开）
    "index_zxjpk": "mooc",  # 在线精品课
    "index_zyzyk": "resource_library",  # 专业资源库
}


def fetch_index_data() -> dict:
    req = urllib.request.Request(
        API,
        data=b"",
        method="POST",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://vocational.smartedu.cn",
            "Referer": "https://vocational.smartedu.cn/",
        },
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8"))


def resolve_url(item: dict, bucket: str) -> str | None:
    url = (item.get("fwdz") or item.get("tzlj") or "").strip()
    if url.startswith("http"):
        return url
    # 在线精品课 / 专业资源库：智慧职教常见课页
    ly = item.get("ly") or ""
    cid = item.get("id") or ""
    if bucket == "index_zxjpk" and cid:
        if "mooc" in ly.lower() or "爱课程" in ly or "大学mooc" in ly.lower():
            # 无稳定 course 号时用搜索落地页（仍可学入口）
            name = item.get("kcmc") or ""
            return f"https://www.icourse163.org/search.htm?search={urllib.parse.quote(name)}"
        return (
            f"https://www.icve.com.cn/portal_new/courseinfo/courseinfo.html?courseid={cid}"
        )
    if bucket == "index_zyzyk" and cid:
        # 资源库项目页（平台内检索）
        return f"https://www.icve.com.cn/portal?id={cid}"
    return None


def collect_resources(payload: dict) -> list[dict]:
    content = payload.get("content") or {}
    out: list[dict] = []
    for bucket, rtype in BUCKET_TYPE.items():
        for item in content.get(bucket) or []:
            name = (
                item.get("kcmc")
                or item.get("ziymc")
                or item.get("jcmc")
                or item.get("ztmc")
                or ""
            ).strip()
            if not name:
                continue
            # 跳过纯活动致辞类（可选保留）
            url = resolve_url(item, bucket)
            if not url or not url.startswith("http"):
                continue
            rid = str(item.get("id") or name)
            out.append(
                {
                    "id": rid,
                    "name": name,
                    "url": url,
                    "resource_type": rtype,
                    "platform": item.get("ly") or "国家职业教育智慧教育平台",
                    "school": item.get("xxmc") or item.get("zcdwmc") or "",
                    "teacher": item.get("jsxm") or "",
                    "major_category": item.get("zy") or "",
                    "industry": item.get("hy") or "",
                    "level_label": item.get("jb") or "",
                    "bucket": bucket,
                    "cover": item.get("fm") or item.get("fmdz") or "",
                    "schedule": item.get("kksj") or "",
                }
            )
    # 去重 by url
    seen = set()
    uniq = []
    for x in out:
        if x["url"] in seen:
            continue
        seen.add(x["url"])
        uniq.append(x)
    return uniq


def make_node(item: dict, fetched_at: str) -> dict:
    sid = f"smartedu:{item['bucket']}:{item['id']}"
    attrs = {
        "role": "learnable_resource",
        "resource_type": item["resource_type"],
        "platform": item["platform"],
        "school": item["school"],
        "teacher": item["teacher"],
        "major_category": item["major_category"],
        "industry": item["industry"],
        "level_label": item["level_label"],
        "bucket": item["bucket"],
        "cover": item["cover"],
        "schedule": item["schedule"],
        "playable": True,
    }
    desc_parts = [item["platform"]]
    if item["school"]:
        desc_parts.append(item["school"])
    if item["major_category"]:
        desc_parts.append(item["major_category"])
    return {
        "id": make_node_id(REGION, "course", SOURCE_SYSTEM, sid),
        "region": REGION,
        "type": "course",
        "name": item["name"],
        "name_en": None,
        "name_zh": item["name"],
        "description": " · ".join(desc_parts),
        "attrs": attrs,
        "source_system": SOURCE_SYSTEM,
        "source_id": sid,
        "source_url": item["url"],
        "license": LICENSE,
        "fetched_at": fetched_at,
        "confidence": "official",
    }


def tag_curriculum_catalog(conn) -> int:
    """把课标解析出的、无可学 URL 的 course 标为 curriculum_catalog。"""
    rows = conn.execute(
        """
        SELECT id, attrs, source_url, source_system FROM nodes
        WHERE region='CN' AND type='course'
        """
    ).fetchall()
    n = 0
    for row in rows:
        attrs = {}
        if row["attrs"]:
            try:
                attrs = json.loads(row["attrs"])
            except Exception:
                attrs = {}
        if attrs.get("role") == "learnable_resource":
            continue
        # 课标来源且 URL 是门户总页
        url = row["source_url"] or ""
        is_catalog = (
            row["source_system"] == "MOE_CN"
            or "专业教学标准" in (attrs.get("catalog") or "")
            or "bznr_zyjyzyjxbz" in url
            or attrs.get("course_kind") in ("foundation", "core", "elective")
        )
        if not is_catalog:
            continue
        if attrs.get("role") == "curriculum_catalog" and attrs.get("playable") is False:
            continue
        attrs["role"] = "curriculum_catalog"
        attrs["playable"] = False
        conn.execute(
            "UPDATE nodes SET attrs=? WHERE id=?",
            (json.dumps(attrs, ensure_ascii=False), row["id"]),
        )
        n += 1
    return n


def ingest(dry_run: bool = False) -> dict:
    ensure_dirs()
    fetched_at = utc_now_iso()
    result: dict = {
        "source_system": SOURCE_SYSTEM,
        "fetched_at": fetched_at,
        "dry_run": dry_run,
        "api": API,
    }
    try:
        payload = fetch_index_data()
    except Exception as e:
        result["error"] = f"fetch failed: {e}"
        return result

    items = collect_resources(payload)
    nodes = [make_node(x, fetched_at) for x in items]
    result["resources_parsed"] = len(nodes)
    result["sample"] = [
        {
            "name": n["name"],
            "url": n["source_url"],
            "type": n["attrs"]["resource_type"],
            "platform": n["attrs"]["platform"],
        }
        for n in nodes[:12]
    ]
    by_type: dict[str, int] = {}
    for n in nodes:
        t = n["attrs"]["resource_type"]
        by_type[t] = by_type.get(t, 0) + 1
    result["by_resource_type"] = by_type

    if dry_run:
        return result

    conn = connect()
    try:
        n_tag = tag_curriculum_catalog(conn)
        n_up = upsert_nodes(conn, nodes)
        conn.commit()
        s = stats(conn)
        # counts
        learnable = conn.execute(
            """
            SELECT COUNT(*) FROM nodes WHERE region='CN' AND type='course'
              AND attrs LIKE '%learnable_resource%'
            """
        ).fetchone()[0]
        catalog = conn.execute(
            """
            SELECT COUNT(*) FROM nodes WHERE region='CN' AND type='course'
              AND attrs LIKE '%curriculum_catalog%'
            """
        ).fetchone()[0]
    finally:
        conn.close()

    result["curriculum_tagged"] = n_tag
    result["nodes_upserted"] = n_up
    result["course_learnable"] = learnable
    result["course_catalog"] = catalog
    result["db_stats"] = s
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "ingest_cn_open_courses.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest learnable open courses (CN)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    out = ingest(dry_run=args.dry_run)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
