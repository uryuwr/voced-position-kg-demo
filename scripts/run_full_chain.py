"""按门类串行跑完整数据链，把技术类验证过的 7 步流程铺到其余门类。

流程（每个门类顺序执行，任一步失败只跳过该门类的后续步骤，不中断整批）::

    1. skill_chain --stage collect   LLM 推断技能构成 + 职级（每 20 条存档，可续跑）
    2. skill_chain --stage merge     技能名归并（按核心词聚类分批）
    3. skill_chain --stage apply     建 skill_level 节点 + requires 边
    4. skill_chain --stage advance   跨族晋升链
    5. major_course --stage major    专业 prepares_for 岗位

全部门类跑完后收尾一次（见 FINISH_STEPS）：
    6. migrate                       SQLite → PG，**不灌就等于没跑**
    7. migrate_skill_category_to_code  把 attrs.category 同步到 category 列

课程资源（6-10 步）默认**不跑**，见 COURSE_STEPS 注释；要跑加 --with-courses。

为什么 course 相关阶段放最后且共享缓存
--------------------------------------
`mooc_courses.json` / `xuetangx_courses.json` 是**全局**的（不按门类分文件），
后跑的门类会自动跳过前面查过的技能 —— 不然「项目管理」这类跨门类技能要重复搜十几次。

为什么不并发
------------
LLM 网关与慕课站都要限速；并发只会触发对方限流，反而更慢，还容易把中间结果写乱。

用法::

    python -X utf8 scripts/run_full_chain.py --list          # 看门类与岗位数
    python -X utf8 scripts/run_full_chain.py --l1 产品 设计   # 指定门类
    python -X utf8 scripts/run_full_chain.py --all           # 除技术类外全部
    python -X utf8 scripts/run_full_chain.py --all --skip-done  # 跳过已有 raw 的门类
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STAGING = ROOT / "data" / "staging"
REPORTS = ROOT / "reports"
LOG = REPORTS / "full_chain_progress.json"

# (模块, 参数模板)。{l1} 会被替换成门类名
#
# 行业→专业→岗位→技能这段链路，与课程资源分开跑。
CHAIN_STEPS: list[tuple[str, list[str]]] = [
    ("crawlers.cn.link_boss_skill_chain", ["--stage", "collect", "--l1", "{l1}", "--sleep", "0.2"]),
    ("crawlers.cn.link_boss_skill_chain", ["--stage", "merge", "--l1", "{l1}", "--batch", "80"]),
    ("crawlers.cn.link_boss_skill_chain", ["--stage", "apply", "--l1", "{l1}"]),
    ("crawlers.cn.link_boss_skill_chain", ["--stage", "advance", "--l1", "{l1}", "--batch", "40"]),
    ("crawlers.cn.link_boss_major_course", ["--stage", "major", "--l1", "{l1}", "--sleep", "0.2"]),
]

# 批次收尾：**所有门类跑完后执行一次**，不是每门类都跑。
#
# 为什么必须有这一步 —— crawlers 写的是 **SQLite 采集库**，PG 是运行库，两者靠
# `migrate` 同步。2026-08-18 那批就栽在这里：9 个门类进度全是「5 步 OK」，
# 采集库里 requires 边 4600+，而 PG 里除了技术和产品全是 0，页面上什么都看不到 ——
# 每一步都成功，链路却是断的，因为没人把数据灌过去。
#
# 第二步同样不能省：`migrate` 只搬 attrs，**不写 kg_node.category 列**，
# 而读路径查的是列。少了它，新灌进来的技能一律显示「待归类」
# （上次实测 12642 个）。
FINISH_STEPS: list[tuple[str, list[str]]] = [
    ("backend.kg.pg_store.migrate", ["--region", "CN"]),
    ("scripts.migrate_skill_category_to_code", []),
]

# 课程资源：**默认不跑**，要 --with-courses 才加进来。
#
# 2026-08-18 停用原因 —— 慕课平台的课不满足「能真实学习」：
#   * 中国大学MOOC / 学堂在线的课**要登录报名**才能看内容；
#   * 它们按学期开课，往期课程结束后非选课用户点进去只有课程介绍页。
# 也就是说 `_course_kind` 判定的 real 只保证「是个课程页」，不保证「现在能学」。
# 判定口径要先立起来（随时可访问 / 免登录 / 无开课周期），再决定采哪些源。
# link_official_docs 是纯静态文档页，本身满足该口径，但先一并停，等口径定了再单独开。
COURSE_STEPS: list[tuple[str, list[str]]] = [
    ("crawlers.cn.harvest_mooc_courses", ["--stage", "fetch", "--l1", "{l1}", "--sleep", "0.7"]),
    ("crawlers.cn.harvest_mooc_courses", ["--stage", "apply", "--l1", "{l1}"]),
    ("crawlers.cn.harvest_xuetangx_courses",
     ["--stage", "fetch", "--l1", "{l1}", "--only-missing", "--sleep", "0.7"]),
    ("crawlers.cn.harvest_xuetangx_courses", ["--stage", "apply", "--l1", "{l1}"]),
    ("crawlers.cn.link_official_docs", ["--stage", "apply", "--l1", "{l1}"]),
]


def slug(s: str) -> str:
    import re

    return re.sub(r"[^0-9A-Za-z一-龥]+", "_", s).strip("_")[:40]


def list_l1() -> list[tuple[str, int]]:
    from backend.kg.pg_store.client import session

    with session() as c, c.cursor() as cur:
        cur.execute(
            """SELECT (attrs::jsonb->>'boss_l1') l1, count(*)::int n
               FROM kg_node WHERE type='occupation' AND source_system='BOSS'
                 AND COALESCE(status,'published')='published'
               GROUP BY 1 ORDER BY 2 DESC"""
        )
        return [(r["l1"], r["n"]) for r in cur.fetchall() if r["l1"]]


def run_step(mod: str, args: list[str], l1: str, *, timeout: int) -> tuple[bool, str]:
    cmd = [sys.executable, "-X", "utf8", "-m", mod] + [a.replace("{l1}", l1) for a in args]
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"超时 {timeout}s"
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "")[-300:]
        return False, f"rc={p.returncode} {tail}"
    return True, (p.stdout or "")[-200:]


def save(progress: dict) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--l1", nargs="*", help="指定门类")
    ap.add_argument("--all", action="store_true", help="除技术类外全部")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--skip-done", action="store_true", help="跳过已有 collect 产物的门类")
    ap.add_argument("--with-courses", action="store_true",
                    help="附带跑课程资源采集（默认不跑，原因见 COURSE_STEPS 注释）")
    ap.add_argument("--no-finish", action="store_true",
                    help="跳过收尾的灌库与分类同步（只想采集、稍后自己灌时用）")
    ap.add_argument("--timeout", type=int, default=5400, help="单步超时秒")
    args = ap.parse_args()

    steps = CHAIN_STEPS + (COURSE_STEPS if args.with_courses else [])

    pairs = list_l1()
    if args.list:
        print("门类与岗位数：")
        for name, n in pairs:
            done = (STAGING / f"boss_skill_raw_{slug(name)}.json").exists()
            print("  %-16s %4d %s" % (name, n, "（已采集）" if done else ""))
        print("\n合计 %d 个岗位" % sum(n for _, n in pairs))
        return

    if args.all:
        todo = [n for n, _ in pairs if n != "技术"]
    elif args.l1:
        todo = args.l1
    else:
        print("需要 --l1 或 --all；先看 --list"); return

    if args.skip_done:
        todo = [n for n in todo
                if not (STAGING / f"boss_skill_raw_{slug(n)}.json").exists()]

    counts = dict(pairs)
    print("将处理 %d 个门类，共约 %d 个岗位：%s"
          % (len(todo), sum(counts.get(n, 0) for n in todo), "、".join(todo)))
    print("每门类 %d 步%s" % (len(steps), "" if args.with_courses else "（课程资源已跳过）"))
    print()

    progress = {"started_at": datetime.now(timezone.utc).isoformat(), "l1": {}}
    save(progress)

    for idx, l1 in enumerate(todo, 1):
        print("=" * 60)
        print("[%d/%d] %s（%d 个岗位）" % (idx, len(todo), l1, counts.get(l1, 0)))
        print("=" * 60)
        rec = {"occupations": counts.get(l1, 0), "steps": [], "ok": True}
        progress["l1"][l1] = rec
        for mod, a in steps:
            label = f"{mod.split('.')[-1]} {a[1]}"
            t0 = time.time()
            ok, msg = run_step(mod, a, l1, timeout=args.timeout)
            dur = round(time.time() - t0, 1)
            rec["steps"].append({"step": label, "ok": ok, "sec": dur,
                                 "msg": msg[:200] if not ok else ""})
            print("   %-42s %s  %.1fs" % (label, "OK" if ok else "FAIL", dur))
            if not ok:
                print("      → %s" % msg[:200])
                rec["ok"] = False
                break  # 后续步骤依赖前一步产物，跳过本门类剩余步骤
            save(progress)
        save(progress)

    # 收尾：灌库 + 同步分类列。跑到这里说明至少有门类产出了数据，
    # 即便中间有门类失败也要灌 —— 成功的那些不该陪着一起躺在采集库里。
    if not args.no_finish:
        print("=" * 60)
        print("收尾：SQLite → PG 灌库 + 同步 category 列")
        print("=" * 60)
        progress["finish"] = []
        for mod, a in FINISH_STEPS:
            t0 = time.time()
            ok, msg = run_step(mod, a, "", timeout=args.timeout)
            dur = round(time.time() - t0, 1)
            progress["finish"].append({"step": mod, "ok": ok, "sec": dur,
                                       "msg": msg[:300] if not ok else ""})
            print("   %-46s %s  %.1fs" % (mod, "OK" if ok else "FAIL", dur))
            if not ok:
                print("      → %s" % msg[:300])
            save(progress)

    progress["finished_at"] = datetime.now(timezone.utc).isoformat()
    save(progress)
    okn = sum(1 for v in progress["l1"].values() if v["ok"])
    print()
    print("完成：%d/%d 门类全部步骤成功，进度文件 %s" % (okn, len(todo), LOG))


if __name__ == "__main__":
    main()
