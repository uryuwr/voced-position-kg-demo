"""写出 CN 五维覆盖率验收报告。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.graph_store import connect
from backend.kg.paths import REPORTS, RAW, ensure_dirs


def main() -> None:
    ensure_dirs()
    conn = connect()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    region = "CN"

    def ntype(t: str) -> int:
        return conn.execute(
            "SELECT count(*) FROM nodes WHERE region=? AND type=?", (region, t)
        ).fetchone()[0]

    def nedge(rel: str) -> int:
        return conn.execute(
            "SELECT count(*) FROM edges WHERE region=? AND rel_type=?", (region, rel)
        ).fetchone()[0]

    def majors_with_edge(rel: str) -> int:
        return conn.execute(
            """
            SELECT count(DISTINCT n.id) FROM nodes n
            JOIN edges e ON e.src_id=n.id
            WHERE n.region=? AND n.type='major' AND e.rel_type=?
            """,
            (region, rel),
        ).fetchone()[0]

    def courses_by_role() -> dict:
        rows = conn.execute(
            """
            SELECT json_extract(attrs,'$.role') as role, count(*)
            FROM nodes WHERE region=? AND type='course'
            GROUP BY role
            """,
            (region,),
        ).fetchall()
        return {str(r[0] or "null"): r[1] for r in rows}

    def learnable_with_url() -> int:
        return conn.execute(
            """
            SELECT count(*) FROM nodes
            WHERE region=? AND type='course'
              AND source_url IS NOT NULL AND length(source_url)>8
              AND (json_extract(attrs,'$.role')='learnable_resource'
                   OR json_extract(attrs,'$.playable')=1
                   OR json_extract(attrs,'$.playable')='true')
            """,
            (region,),
        ).fetchone()[0]

    pdf_dir = RAW / "CN" / "course" / "moe_2025" / "pdfs"
    n_pdf = len(list(pdf_dir.glob("*.pdf"))) if pdf_dir.exists() else 0
    skill_pdfs = list((RAW / "CN" / "skill_standards").rglob("*.pdf")) if (RAW / "CN" / "skill_standards").exists() else []

    major_n = ntype("major")
    occ_n = ntype("occupation")
    skill_n = ntype("skill_level")
    course_n = ntype("course")
    cred_n = ntype("credential")
    pf = nedge("prepares_for")
    req = nedge("requires")
    rel = nedge("related_to")
    taught = nedge("taught_by") if conn.execute(
        "SELECT count(*) FROM edges WHERE rel_type='taught_by' AND region=?", (region,)
    ).fetchone() else 0

    majors_pf = majors_with_edge("prepares_for")
    majors_course = majors_with_edge("related_to")
    roles = courses_by_role()
    learnable = learnable_with_url()

    # Neo4j optional
    neo = {}
    try:
        from backend.kg.neo4j_store.query import session

        with session() as s:
            neo["entities"] = s.run(
                "MATCH (n:Entity) WHERE n.region=$r RETURN count(n) AS c", r=region
            ).single()["c"]
            neo["rels"] = s.run(
                "MATCH (a:Entity {region:$r})-[r]->(b) RETURN count(r) AS c", r=region
            ).single()["c"]
    except Exception as e:
        neo["error"] = str(e)

    # Acceptance heuristics (landing, not "official full universe")
    cred_linked = conn.execute(
        """
        SELECT count(DISTINCT n.id) FROM nodes n
        JOIN edges e ON e.src_id=n.id OR e.dst_id=n.id
        WHERE n.region=? AND n.type='credential'
        """,
        (region,),
    ).fetchone()[0]
    occ_req = conn.execute(
        """
        SELECT count(DISTINCT n.id) FROM nodes n
        JOIN edges e ON e.src_id=n.id
        WHERE n.region=? AND n.type='occupation' AND e.rel_type='requires'
        """,
        (region,),
    ).fetchone()[0]

    checks = {
        "major_gt_2000": major_n >= 2000,
        "occupation_gt_1000": occ_n >= 1000,
        "credential_gt_100": cred_n >= 100,
        "course_gt_100": course_n >= 100,
        "skill_level_gt_20": skill_n >= 20,
        "prepares_for_gt_500": pf >= 500,
        "requires_gt_20": req >= 20,
        "majors_pf_coverage_gt_30pct": majors_pf >= major_n * 0.3,
        "learnable_with_url_gt_0": learnable > 0,
        "credential_linked_gt_0": cred_linked > 0,
        "occ_requires_gt_5": occ_req >= 5,
        "moe_pdf_gt_500": n_pdf >= 500,
        "neo4j_synced": bool(neo.get("entities", 0) >= major_n * 0.9) if "entities" in neo else False,
    }
    summary_extra = {
        "credentials_with_edge": cred_linked,
        "occupations_with_requires": occ_req,
    }
    passed = sum(1 for v in checks.values() if v)
    total_c = len(checks)

    summary = {
        "date": day,
        "region": region,
        "nodes": {
            "major": major_n,
            "occupation": occ_n,
            "skill_level": skill_n,
            "course": course_n,
            "credential": cred_n,
            "course_by_role": roles,
            "learnable_with_url": learnable,
        },
        "edges": {
            "prepares_for": pf,
            "requires": req,
            "related_to": rel,
            "taught_by": taught,
            "majors_with_prepares_for": majors_pf,
            "majors_with_related_to": majors_course,
        },
        "raw": {
            "moe_teaching_standard_pdfs": n_pdf,
            "skill_standard_pdfs": len(skill_pdfs),
        },
        "neo4j": neo,
        "acceptance_checks": checks,
        "acceptance_score": f"{passed}/{total_c}",
        "acceptance_pass": passed == total_c,
        **summary_extra,
    }

    REPORTS.mkdir(parents=True, exist_ok=True)
    json_path = REPORTS / f"{day}-CN五维覆盖率验收.json"
    md_path = REPORTS / f"{day}-CN五维覆盖率验收.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"# CN 五维覆盖率验收 · {day}",
        "",
        f"**验收结果：{'通过' if summary['acceptance_pass'] else '部分通过'}（{summary['acceptance_score']}）**",
        "",
        "## 节点",
        f"| 维度 | 数量 |",
        f"| --- | ---: |",
        f"| major | {major_n} |",
        f"| occupation | {occ_n} |",
        f"| skill_level | {skill_n} |",
        f"| course | {course_n} |",
        f"|  · curriculum / learnable | {roles} |",
        f"|  · learnable 有 URL | {learnable} |",
        f"| credential | {cred_n} |",
        "",
        "## 边",
        f"| 关系 | 数量 |",
        f"| --- | ---: |",
        f"| prepares_for（专→岗） | {pf}（覆盖专业 {majors_pf}） |",
        f"| requires（岗→技） | {req} |",
        f"| related_to | {rel}（含专→课，覆盖专业 {majors_course}） |",
        f"| taught_by | {taught} |",
        "",
        "## 原始文件",
        f"- 课标 PDF：{n_pdf}",
        f"- 技能标准 PDF：{len(skill_pdfs)}",
        "",
        "## Neo4j",
        f"```json\n{json.dumps(neo, ensure_ascii=False, indent=2)}\n```",
        "",
        "## 检查项",
    ]
    for k, v in checks.items():
        lines.append(f"- [{'x' if v else ' '}] `{k}`")
    lines += [
        "",
        "## 说明",
        "- 本报告为「落地可验收」口径：官方可采尽采 + 无源处规则/AI 边（需 HITL 抽检）。",
        "- 技能标准/可学资源全量官方清单仍受源站限制，见 `docs/HITL待办.md`。",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    main()
