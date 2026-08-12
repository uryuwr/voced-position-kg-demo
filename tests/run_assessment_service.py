"""按服务层接口（start/answer）跑一遍，验证阶段状态与 question_end 契约。"""
from __future__ import annotations
import sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.agent.assessment import service
from backend.kg.pg_store.client import connect

with connect() as c:
    occ = c.execute("""SELECT o.id,o.name,COUNT(*) n FROM kg_edge e
        JOIN kg_node o ON o.id=e.src_id AND o.type='occupation'
        WHERE e.rel_type='requires' AND COALESCE(e.status,'published')='published'
        GROUP BY 1,2 HAVING COUNT(*)>=8 ORDER BY n DESC LIMIT 1""").fetchone()
sid = f"svc-{int(time.time())}"
print("岗位:", occ["name"], "| session:", sid, flush=True)

v = service.start(sid, user_id="demo", user_name="demo", occupation_id=occ["id"],
                  resume_text="从事汽车维修5年，负责整车故障诊断与发动机检修，用示波器定位间歇性故障，返修率从8%降到3%。")
def show(v, tag):
    st = " | ".join(f"{s['name']}:{s['status']}" for s in v["stages"])
    print(f"\n[{tag}] {v['elapsed_ms']}ms  当前={v['current_stage']}  question_end={v['question_end']}", flush=True)
    print("   阶段:", st, flush=True)
    if v["stages"][0]["status"] == "done":
        o = v["stages"][0]["output"]
        print(f"   阶段1输出: engine={o.get('engine')} 技能{o.get('skill_count')}项", flush=True)
    q = v.get("question")
    if q:
        print(f"   待答: [{q['type']}] {q['skill_key']} — {q['prompt'][:44]}…", flush=True)
show(v, "start")

n = 0
while v.get("question") and n < 15:
    q = v["question"]; n += 1
    ans = max(q["options"], key=lambda o: o["level"])["value"] if q["type"] == "choice" else \
        "负责整车故障诊断，读故障码后用示波器比对波形定位线束虚接，建立排查清单，返修率从8%降到3%，培训2名新人。"
    v = service.answer(sid, ans)
    show(v, f"answer#{n}")

print("\n" + "="*54, flush=True)
r = v.get("report") or {}
print(f"匹配度 {r.get('match_score')}%  覆盖 {r.get('coverage')}%", flush=True)
print(r.get("summary"), flush=True)
print("雷达轴:", r.get("radar", {}).get("categories"), flush=True)
for s in r.get("radar", {}).get("series", []): print("  ", s["name"], s["scores"], flush=True)
print("优势:", [(x["skill_key"], f"L{x['measured_level']}/要求L{x['required_level']}") for x in r.get("strengths", [])[:4]], flush=True)
print("短板:", [(x["skill_key"], f"L{x['measured_level']}/要求L{x['required_level']}") for x in r.get("gaps", [])[:4]], flush=True)
print("未覆盖:", r.get("counts", {}).get("untested"), flush=True)
print("\n刷新恢复:", {k: service.get_state(sid)[k] for k in ("exists", "current_stage", "question_end")}, flush=True)
