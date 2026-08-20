"""闸门：模型产出必须能对回技能项，且不能整批静默降级成模板题。

## 为什么需要它

出题链路有一条**只降级、不报错**的失效路径。`bank._skill_lines` 给模型看的是
**技能名**（喂 `SKa1fa1d005d` 进去，它会照着一串哈希编题），而 JSON 模板要它回
一个标识；一旦「给它看的」与「拿去查的」不是同一个东西，`gen.get(...)` 全部落空，
每道题都走 `fallback_choice`：

- HTTP 200、日志无异常、无堆栈
- `meta.engine` 从 `llm` 变成 `llm_partial`，`fallback_count = 题数`
- 学员看到的题从「情境判断」变成「在 X 上你处于哪一档」的自评模板

2026-08-20 就是这么坏了一整天：`skill_key` 改成 ASCII code 后，提示词改成喂名字，
匹配却还在按 code 查。而当时抽查的两个岗位都命中了题库缓存（`biz_assessment_item`），
缓存里是改造前生成的真情境题，所以**看起来一切正常**。

## 判据

1. `_pair_generated` 的六种形态（序号 / 名字 / code / 名字被改写 / 无标识 / 序号越界）
   都要对回正确的位置 —— **不依赖 LLM，任何环境都跑**
2. 网关可用时真出一批题：`fallback_count` 必须为 0，`variant` 必须是 `sjt`
   （`llm_ready()` 为假就跳过并明说跳过了，不算通过）

    PYTHONPATH=. python -X utf8 scripts/verify_question_pairing.py
"""

from __future__ import annotations

import sys
import warnings

warnings.filterwarnings("ignore")

from backend.agent.assessment.bank import _pair_generated, generate_batch  # noqa: E402
from backend.agent.llm import llm_ready  # noqa: E402

ITEMS = [
    {"skill_key": "SK08753b7aad", "skill_name": "门店运营管理"},
    {"skill_key": "SK5b50c5033b", "skill_name": "维修业务管控"},
    {"skill_key": "SK35fe7b2eca", "skill_name": "团队带教管理"},
]

CASES: list[tuple[str, list[dict], list[str]]] = [
    (
        "按 no 回（乱序）",
        [{"no": 2, "prompt": "B"}, {"no": 1, "prompt": "A"}, {"no": 3, "prompt": "C"}],
        ["A", "B", "C"],
    ),
    (
        "按技能名回",
        [{"skill": "团队带教管理", "prompt": "C"}, {"skill": "门店运营管理", "prompt": "A"}],
        ["A", "", "C"],
    ),
    ("按 code 回", [{"skill_key": "SK5b50c5033b", "prompt": "B"}], ["", "B", ""]),
    ("名字被改写", [{"skill": "门店 运营/管理", "prompt": "A"}], ["A", "", ""]),
    ("啥标识都不回", [{"prompt": "A"}, {"prompt": "B"}], ["A", "B", ""]),
    ("no 越界", [{"no": 99, "prompt": "X"}], ["X", "", ""]),
]

fails: list[str] = []
print("① 匹配形态（不依赖 LLM）")
for label, gens, want in CASES:
    got = [str((x or {}).get("prompt") or "") for x in _pair_generated(ITEMS, gens)]
    ok = got == want
    print(f"   [{'PASS' if ok else 'FAIL'}] {label}: {got}" + ("" if ok else f" ≠ 期望 {want}"))
    if not ok:
        fails.append(label)

print("\n② 真出一批题（要网关）")
if not llm_ready():
    print("   [SKIP] 未配 LLM 网关 —— 这一条**没有验**，不要当成通过")
else:
    from backend.agent.assessment.service import load_context
    from backend.kg.pg_store.client import connect

    with connect() as c:
        # **必须 join 到线上岗位节点**：只按边挑会挑出「边在、节点不在（或只有草稿行）」
        # 的 id，`get_composition` 直接抛 CompositionError，闸门变成挂在自己的取数上
        r = c.execute(
            """
            SELECT e.src_id AS id FROM kg_edge e
            JOIN kg_node n ON n.id = e.src_id AND NOT n.is_draft
                          AND COALESCE(n.status,'published') = 'published'
                          AND n.type = 'occupation'
            WHERE e.rel_type = 'requires' AND NOT e.is_draft
              AND COALESCE(e.status,'published') = 'published'
            GROUP BY e.src_id HAVING COUNT(*) >= 3 ORDER BY e.src_id LIMIT 1
            """
        ).fetchone()
    occ_id = (r or {}).get("id")
    if not occ_id:
        print("   [SKIP] 库里找不到带 3 个以上技能要求的岗位")
    else:
        ctx = load_context(-1, occ_id)
        items = (ctx.get("required_items") or [])[:3]
        # use_cache=False 是关键：命中题库缓存就绕过了模型，这一条等于没验
        qs, meta = generate_batch(items, occupation=ctx.get("occupation"), use_cache=False)
        nfb = int(meta.get("fallback_count") or 0)
        variants = meta.get("variants") or []
        print(f"   岗位 {occ_id}  meta={meta}")
        for q in qs:
            print(f"     {q.get('variant'):<11} {q.get('skill_name')!r}  {(q.get('prompt') or '')[:36]}")
        if nfb:
            fails.append(f"真出题降级 {nfb}/{len(items)} 道（模型产出对不回技能项）")
            print(f"   [FAIL] fallback_count={nfb} —— 模型产出没对回技能项")
        elif variants != ["sjt"]:
            fails.append(f"variant 不是 sjt：{variants}")
            print(f"   [FAIL] variants={variants}")
        else:
            print("   [PASS] 全部 sjt，零降级")

print()
if fails:
    print(f"★ {len(fails)} 项失败：{fails}")
else:
    print("PASS 模型产出能对回技能项，未发生静默降级")
sys.exit(1 if fails else 0)
