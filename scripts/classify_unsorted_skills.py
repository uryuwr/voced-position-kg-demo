"""给「待归类」技能补分类（LLM）。

为什么规则补不动这批
--------------------
`seed_skill_taxonomy.py` 用关键词正则从技能名反推分类，覆盖了 71%。剩下的 469 个
是规则天然够不着的：

    区块链测试 / 云计算平台搭建 / 酿酒 / 停车收费 / 剃须与修面 / 封发国内邮件

它们不含「设备」「质量」「培训」这类锚词，只能靠语义判断。

刻意不硬塞
----------
这批里混着三种东西，只有第一种该被归类：

    1. 能判断的      酿酒 → 操作与生产、停车收费 → 服务与业务
    2. 真的太泛      「连接」「调查」「配制」「改造更新」—— 脱离上下文没法归
    3. 岗位名混进来   「水泥质检员」「城市轨道交通行车值班员」「管涵顶进工」
                     —— 这是**采集把职业名当成技能名**的数据质量问题，不是分类问题

后两种一律留在 `UNSORTED`。兜底类的意义就是把「还没解决的」和「已经分好的」分开，
硬塞进某一类等于把问题藏起来。第 3 种的数量值得单独看，脚本会报出来。

用法::

    python -X utf8 scripts/classify_unsorted_skills.py --dry-run
    python -X utf8 scripts/classify_unsorted_skills.py --limit 100   # 先小批试
    python -X utf8 scripts/classify_unsorted_skills.py
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import session
from backend.kg.pg_store.skill_aggregate import SKILL_KEY_SQL
from backend.kg.pg_store.skill_taxonomy import (
    FALLBACK_CODE,
    SKILL_CATEGORIES,
    to_code,
)

SQLITE = ROOT / "data" / "graph" / "kg.sqlite"
CACHE = ROOT / "data" / "staging" / "skill_category_llm.json"

_OPTIONS = "\n".join(
    f"  {c['code']}  {c['name']} —— {c['description']}"
    for c in SKILL_CATEGORIES
    if c["code"] != FALLBACK_CODE
)

PROMPT = f"""把下面的职业技能逐个归入分类。这些技能来自国家职业技能标准，覆盖制造、
建筑、服务、交通、IT 等各行业。

可选分类（只能用左边的 code）：
{_OPTIONS}
  {FALLBACK_CODE}  待归类 —— 见下面第 2 条

规则：
1. 每个技能都要出现在结果里，一个不漏
2. 下面两种情况**必须**返回 {FALLBACK_CODE}，不要硬凑一个类：
   - 词太泛、脱离上下文判断不了（如「连接」「调查」「配制」「改造更新」）
   - 它其实是**职业名称**而不是技能（如「水泥质检员」「城市轨道交通行车值班员」）
3. 判断依据是「做这件事主要用到哪一类能力」，不是字面关键词
   - 「酿酒」→ OPERATE（生产作业），不是 QUALITY
   - 「停车收费」→ SERVICE（业务办理），不是 MANAGE
   - 「车身零部件拆装」→ MAINTAIN（检修），不是 OPERATE

技能列表：
{{skills}}

只输出 JSON，不要解释：
{{{{"items": [{{{{"skill": "技能名", "code": "分类code"}}}}]}}}}"""


def salvage(text: str) -> list[dict]:
    """从被截断的输出里捞回完整对象 —— 大批量时尾部常缺 `]}`，
    整体 json.loads 失败会把前面几十条一起丢掉。"""
    return [
        {"skill": m.group(1), "code": m.group(2)}
        for m in re.finditer(
            r'\{\s*"skill"\s*:\s*"([^"]+)"\s*,\s*"code"\s*:\s*"([^"]+)"\s*\}', text or ""
        )
    ]


def load_pending() -> list[str]:
    with session() as c, c.cursor() as cur:
        cur.execute(
            f"""SELECT DISTINCT ({SKILL_KEY_SQL}) AS k
                FROM kg_node n
                WHERE n.type = 'skill_level'
                  AND COALESCE(n.status,'published') = 'published'
                  AND COALESCE(NULLIF(n.category,''), '{FALLBACK_CODE}') = '{FALLBACK_CODE}'
                ORDER BY 1"""
        )
        return [r["k"] for r in cur.fetchall() if r["k"]]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 个，先小批试")
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--no-resume", action="store_true", help="忽略缓存重跑")
    args = ap.parse_args()

    from backend.agent.llm import invoke_fast, llm_ready

    if not llm_ready():
        print("LLM 网关未配置，无法归类"); sys.exit(1)

    pending = load_pending()
    if args.limit:
        pending = pending[: args.limit]
    print("待归类逻辑技能：%d" % len(pending))

    # 落盘续跑：一次跑几百个技能十几分钟，中断了不该从头再烧一遍 token
    cache: dict[str, str] = {}
    if CACHE.exists() and not args.no_resume:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
        print("命中缓存：%d" % len([k for k in pending if k in cache]))

    todo = [k for k in pending if k not in cache]
    for i in range(0, len(todo), args.batch):
        chunk = todo[i : i + args.batch]
        msg = [
            ("system", "你是职业技能分类专家，只输出 JSON。"),
            ("user", PROMPT.format(skills="\n".join(f"- {s}" for s in chunk))),
        ]
        try:
            raw = invoke_fast(msg, max_tokens=4000)
        except Exception as e:  # noqa: BLE001
            print("  批 %d 调用失败：%s" % (i // args.batch, str(e)[:80]))
            continue
        items = []
        try:
            t = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M)
            a, b = t.find("{"), t.rfind("}")
            items = (json.loads(t[a : b + 1]) or {}).get("items") or []
        except Exception:  # noqa: BLE001
            items = salvage(raw)
            if items:
                print("  批 %d 输出截断，抢救出 %d 条" % (i // args.batch, len(items)))
        got = 0
        for it in items:
            sk = str(it.get("skill") or "").strip()
            if sk in set(chunk):
                # 过一遍 to_code：LLM 可能回中文名或编不存在的 code，
                # 认不出的落兜底而不是写进一个库里没有的分类
                cache[sk] = to_code(it.get("code"))
                got += 1
        print("  批 %-3d %3d/%3d 已归类" % (i // args.batch, got, len(chunk)))
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    decided = {k: v for k, v in cache.items() if k in set(pending) and v != FALLBACK_CODE}
    kept = [k for k in pending if cache.get(k, FALLBACK_CODE) == FALLBACK_CODE]
    dist = collections.Counter(decided.values())
    print()
    print("归类结果：%d 个定了类，%d 个留在待归类" % (len(decided), len(kept)))
    for code, n in dist.most_common():
        print("   %-9s %4d" % (code, n))
    if kept:
        print("留在待归类的样例：%s" % "、".join(kept[:12]))

    if args.dry_run or not decided:
        print("\n(dry-run，未写库)" if args.dry_run else "\n无可写入项")
        return

    with session() as c, c.cursor() as cur:
        cur.execute("CREATE TEMP TABLE _sk_cat (k text PRIMARY KEY, cat text) ON COMMIT DROP")
        cur.executemany(
            "INSERT INTO _sk_cat(k, cat) VALUES (%s, %s) ON CONFLICT (k) DO NOTHING",
            list(decided.items()),
        )
        # 只改仍是兜底的那些：跑这个脚本期间可能有人在管理台手工分过类，
        # 不能拿 LLM 的判断盖掉人工的
        cur.execute(
            f"""UPDATE kg_node n SET category = t.cat FROM _sk_cat t
                WHERE n.type = 'skill_level' AND ({SKILL_KEY_SQL}) = t.k
                  AND COALESCE(NULLIF(n.category,''), '{FALLBACK_CODE}') = '{FALLBACK_CODE}'"""
        )
        print("PG 已更新节点：%d" % cur.rowcount)

    if SQLITE.exists():
        s = sqlite3.connect(SQLITE)
        s.row_factory = sqlite3.Row
        n = 0
        for r in s.execute("SELECT id, name, attrs FROM nodes WHERE type='skill_level'").fetchall():
            try:
                a = json.loads(r["attrs"] or "{}")
            except Exception:
                continue
            key = (a.get("skill_key") or a.get("skill_name") or "").strip() or \
                  (r["name"] or "").split("·")[0].strip()
            code = decided.get(key)
            if not code or a.get("category") not in (None, "", FALLBACK_CODE):
                continue
            a["category"] = code
            s.execute("UPDATE nodes SET attrs=? WHERE id=?",
                      (json.dumps(a, ensure_ascii=False), r["id"]))
            n += 1
        s.commit()
        s.close()
        print("SQLite 已更新：%d" % n)


if __name__ == "__main__":
    main()
