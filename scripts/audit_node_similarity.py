"""四维节点相似度审计：找出**真正需要归一**的重复节点。

要区分三种形态，只有第 3 种才是问题：

1. **层级关系**：industry「互联网/AI」(level=1) → 「互联网」(level=2)，有 parent_of 边。
   正常结构，不是重复。
2. **同名不同层次**：major「计算机应用」在中职/高职专科/职业本科各有一条。
   学历层次不同，是业务需要，不能合并（但前端要显示层次以免混淆）。
3. **同层同义**：同一层级内语义重复，如技能「Linux操作」/「Linux系统操作」。
   **这类才要归一**。

用法::

    python -X utf8 scripts/audit_node_similarity.py                # 全维度
    python -X utf8 scripts/audit_node_similarity.py --type skill   # 单维度
"""
from __future__ import annotations

import argparse
import collections
import difflib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import session

OUT = ROOT / "reports" / "node_similarity_audit.json"
_CJK = r"一-龥"


def norm(s: str) -> str:
    """比较用归一：去空格/标点/括号内容，转小写。"""
    s = re.sub(r"[（(][^）)]*[）)]", "", s or "")
    s = re.sub(r"[\s·・/、,，.。\-_（）()【】\[\]]+", "", s)
    return s.lower()


def core_tokens(s: str) -> set[str]:
    return set(re.findall(rf"[A-Za-z][A-Za-z+#./0-9]{{1,}}|[{_CJK}]{{2,}}", s or ""))


def fetch(node_type: str) -> list[dict]:
    sql = """
      SELECT id, name, status, source_system, attrs
      FROM kg_node
      WHERE type = %s AND region = 'CN' AND COALESCE(status,'published') <> 'archived'
    """
    out = []
    with session() as c, c.cursor() as cur:
        cur.execute(sql, (node_type,))
        for r in cur.fetchall():
            a = r["attrs"]
            if isinstance(a, str):
                try:
                    a = json.loads(a or "{}")
                except Exception:
                    a = {}
            out.append({"id": r["id"], "name": r["name"], "status": r["status"],
                        "src": r["source_system"], "attrs": a or {}})
    return out


def parent_pairs() -> set[tuple[str, str]]:
    """已有 parent_of 的对，排除掉（层级不是重复）。"""
    out = set()
    with session() as c, c.cursor() as cur:
        cur.execute("SELECT src_id, dst_id FROM kg_edge WHERE rel_type='parent_of'")
        for r in cur.fetchall():
            out.add((r["src_id"], r["dst_id"]))
            out.add((r["dst_id"], r["src_id"]))
    return out


def audit(node_type: str, *, thresh: float = 0.82, key_field: str | None = None) -> dict:
    nodes = fetch(node_type)
    hier = parent_pairs() if node_type == "industry" else set()

    # 技能按 skill_key 聚合成逻辑技能（一技能五档，不然全是自己和自己像）
    if key_field == "skill_key":
        logical: dict[str, dict] = {}
        for n in nodes:
            k = n["attrs"].get("skill_key") or str(n["name"]).split(" · ")[0]
            logical.setdefault(k, {"id": n["id"], "name": k, "src": n["src"],
                                   "attrs": n["attrs"], "status": n["status"], "levels": 0})
            logical[k]["levels"] += 1
        nodes = list(logical.values())

    # 用核心词建倒排，只比可能相似的对，避免 O(n^2) 全比
    inv: dict[str, list[int]] = collections.defaultdict(list)
    for i, n in enumerate(nodes):
        for t in core_tokens(n["name"]):
            inv[t].append(i)

    cand: set[tuple[int, int]] = set()
    for idxs in inv.values():
        if len(idxs) > 200:  # 过于常见的词（如"技术"）跳过，否则候选爆炸
            continue
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                cand.add((idxs[a], idxs[b]))

    same_norm: list[dict] = []
    similar: list[dict] = []
    for i, j in cand:
        A, B = nodes[i], nodes[j]
        na, nb = norm(A["name"]), norm(B["name"])
        if not na or not nb:
            continue
        if (A["id"], B["id"]) in hier:
            continue  # 父子关系，不算重复
        if na == nb:
            same_norm.append({
                "a": A["name"], "b": B["name"], "a_id": A["id"], "b_id": B["id"],
                "a_src": A["src"], "b_src": B["src"],
                "a_level": A["attrs"].get("level") or A["attrs"].get("level_zh"),
                "b_level": B["attrs"].get("level") or B["attrs"].get("level_zh"),
                "verdict": "归一后同名",
            })
            continue
        ratio = difflib.SequenceMatcher(None, na, nb).ratio()
        if ratio >= thresh:
            similar.append({
                "a": A["name"], "b": B["name"], "a_id": A["id"], "b_id": B["id"],
                "a_src": A["src"], "b_src": B["src"], "ratio": round(ratio, 3),
                "a_level": A["attrs"].get("level") or A["attrs"].get("level_zh"),
                "b_level": B["attrs"].get("level") or B["attrs"].get("level_zh"),
            })
    similar.sort(key=lambda x: -x["ratio"])

    # 同名不同层次（major 的正常形态）单独统计
    by_norm = collections.defaultdict(list)
    for n in nodes:
        by_norm[norm(n["name"])].append(n)
    same_name_diff_level = {
        k: [{"name": x["name"], "level": x["attrs"].get("level_zh") or x["attrs"].get("level"),
             "id": x["id"]} for x in v]
        for k, v in by_norm.items() if len(v) > 1
    }

    return {
        "type": node_type,
        "nodes": len(nodes),
        "compared_pairs": len(cand),
        "same_after_norm": len(same_norm),
        "similar_pairs": len(similar),
        "same_name_groups": len(same_name_diff_level),
        "same_norm_sample": same_norm[:15],
        "similar_sample": similar[:25],
        "same_name_sample": dict(list(same_name_diff_level.items())[:8]),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", choices=("industry", "major", "occupation", "skill", "all"), default="all")
    ap.add_argument("--thresh", type=float, default=0.82)
    args = ap.parse_args()

    todo = (["industry", "major", "occupation", "skill"] if args.type == "all" else [args.type])
    result = {}
    for t in todo:
        if t == "skill":
            result[t] = audit("skill_level", thresh=args.thresh, key_field="skill_key")
        else:
            result[t] = audit(t, thresh=args.thresh)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    for t, r in result.items():
        print("=== %s ===" % t)
        print("  节点 %d | 比较对 %d | 归一后同名 %d | 高相似 %d | 同名多层次组 %d"
              % (r["nodes"], r["compared_pairs"], r["same_after_norm"],
                 r["similar_pairs"], r["same_name_groups"]))
        for s in r["same_norm_sample"][:4]:
            print("    [同名] %s  ←→  %s   (%s / %s)" % (s["a"], s["b"], s["a_src"], s["b_src"]))
        for s in r["similar_sample"][:6]:
            print("    [相似 %.2f] %s  ←→  %s" % (s["ratio"], s["a"], s["b"]))
        print()
    print("完整报告:", OUT)


if __name__ == "__main__":
    main()
