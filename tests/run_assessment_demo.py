"""命令行跑通一次完整测评工作流，用于人工核对出题质量与报告口径。

用法：python -X utf8 tests/run_assessment_demo.py [岗位名]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langgraph.types import Command  # noqa: E402

from backend.agent.assessment.graph import checkpointer_kind, get_graph  # noqa: E402
from backend.kg.pg_store.client import connect  # noqa: E402

RESUME = (
    "从事汽车维修5年，负责整车故障诊断与发动机检修，熟练使用示波器和解码器定位"
    "间歇性电路故障，主导过变速箱大修，季度返修率从8%降到3%，带过2名学徒。"
)
GOOD_ANSWER = (
    "负责整车故障诊断。一次客户反馈间歇性熄火但无故障码，我先复现工况，"
    "用示波器同步抓取曲轴位置传感器与点火信号波形，比对正常车型波形，"
    "定位到线束接插件虚接。之后建立了该车型的排查清单与复检流程，"
    "季度返修率从8%降到3%，并培训了2名新人。"
)
WEAK_ANSWER = "做过一些，跟着师傅学的，具体记不太清了。"


def main() -> int:
    want = sys.argv[1] if len(sys.argv) > 1 else None
    with connect() as c:
        if want:
            occ = c.execute(
                "SELECT id, name FROM kg_node WHERE type='occupation' AND name=%s", (want,)
            ).fetchone()
        else:
            occ = c.execute(
                """SELECT o.id, o.name, COUNT(*) n FROM kg_edge e
                   JOIN kg_node o ON o.id = e.src_id AND o.type='occupation'
                   WHERE e.rel_type='requires' AND COALESCE(e.status,'published')='published'
                   GROUP BY 1,2 HAVING COUNT(*) >= 8 ORDER BY n DESC LIMIT 1"""
            ).fetchone()
    if not occ:
        print("找不到岗位"); return 1
    print(f"岗位：{occ['name']}  | checkpointer：{checkpointer_kind()}", flush=True)

    g = get_graph()
    cfg = {"configurable": {"thread_id": f"demo-{int(time.time())}"}}
    t = time.time()
    st = g.invoke(
        {
            "session_id": 0,
            "user_id": "demo",
            "user_name": "demo",
            "occupation_id": occ["id"],
            "resume_text": RESUME,
        },
        cfg,
    )
    pm = st.get("paper_meta") or {}
    print(
        f"\n[解析+首批出题] {time.time()-t:.1f}s"
        f" | 画像 engine={(st.get('profile_meta') or {}).get('engine')}"
        f" 条目={len(st.get('profile_levels') or {})}"
        f" | 首批 {pm.get('batch1')}",
        flush=True,
    )
    print("  画像：", dict(list((st.get("profile_levels") or {}).items())[:6]), flush=True)

    qn = 0
    while "__interrupt__" in st:
        itr = st["__interrupt__"][0].value
        q, p = itr["question"], itr["progress"]
        qn += 1
        print(
            f"\n── 第 {p['index']+1}/{p['total']} 题 [{q['type']}/{q.get('variant')}] "
            f"{q['skill_key']} (权重{q['weight']} 要求L{q['required_level']})",
            flush=True,
        )
        print("   Q:", q["prompt"], flush=True)
        if q["type"] == "choice":
            for o in q["options"]:
                print(f"     ({o['value']}) [L{o['level']}] {o['text']}", flush=True)
            # 前两题挑最高档选项，其余挑最低档 —— 制造有优势也有短板的画像
            lv_sorted = sorted(q["options"], key=lambda o: o["level"])
            ans = (lv_sorted[-1] if qn <= 2 else lv_sorted[0])["value"]
            print(f"   A: 选 ({ans})", flush=True)
        else:
            if q.get("rubric"):
                print("   评分要点:", " / ".join(q["rubric"]), flush=True)
            ans = GOOD_ANSWER if qn % 2 else WEAK_ANSWER
            print(f"   A: {ans[:44]}…", flush=True)
        t = time.time()
        st = g.invoke(Command(resume=ans), cfg)
        print(f"   （判分 {time.time()-t:.1f}s）", flush=True)

    print(
        f"\n[收敛] 共 {len(st.get('paper') or [])} 题 / {st.get('batches')} 批"
        f" | 停止原因：{st.get('stop_reason')}",
        flush=True,
    )
    rep = st["report"]
    print("\n" + "=" * 60, flush=True)
    print(f"综合能力匹配度：{rep['match_score']}%", flush=True)
    print(rep["summary"], flush=True)
    print("\n雷达轴：", rep["radar"]["categories"], flush=True)
    for s in rep["radar"]["series"]:
        print(f"   {s['name']}: {s['scores']}", flush=True)
    print(
        "\n优势能力领域：",
        [(x["skill_key"], f"L{x['measured_level']}") for x in rep["strengths"][:5]] or "无",
        flush=True,
    )
    print(
        "关键能力短板：",
        [
            (x["skill_key"], f"L{x['measured_level']}" if x["measured_level"] else "待补强")
            for x in rep["gaps"][:5]
        ],
        flush=True,
    )
    print("\n问答题判分：", flush=True)
    for x in st["graded"]:
        if x["type"] == "open":
            print(
                f"   {x['skill_key']}：自评L{x.get('self_level')} → L{x['level']}"
                f"（证据{x.get('evidence_score')}分，{'已下调' if x.get('capped') else '维持'}）"
                f" 命中要点={x.get('rubric_met')} | {x.get('comment')}",
                flush=True,
            )
    print("\n学习计划入口：", rep["next_action"]["gap_skills"][:5], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
