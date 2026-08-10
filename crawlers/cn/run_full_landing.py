"""
CN 五维完整落地编排（可断点续跑、可独立后台执行）。

不依赖会话：中断后重新执行同一命令会从 checkpoint 跳过已成功步骤。

  python -u -m crawlers.cn.run_full_landing
  python -u -m crawlers.cn.run_full_landing --from-step ingest_courses
  python -u -m crawlers.cn.run_full_landing --force   # 忽略 checkpoint
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.paths import REPORTS, RAW, ensure_dirs

CKPT = REPORTS / "cn_full_landing_checkpoint.json"
LOG = REPORTS / "cn_full_landing.log"
PROGRESS = REPORTS / "cn_full_landing_progress.json"

STEPS: list[tuple[str, list[str]]] = [
    ("batch_download_moe", [sys.executable, "-u", "-m", "crawlers.cn.batch_download_moe_standards"]),
    ("batch_download_skill", [sys.executable, "-u", "-m", "crawlers.cn.batch_download_skill_standards"]),
    ("ingest_courses", [sys.executable, "-u", "-m", "crawlers.cn.ingest_courses"]),
    ("ingest_skill_standards", [sys.executable, "-u", "-m", "crawlers.cn.ingest_skill_standards"]),
    ("ingest_open_courses", [sys.executable, "-u", "-m", "crawlers.cn.ingest_open_courses"]),
    ("link_prepares_for", [sys.executable, "-u", "-m", "crawlers.cn.link_prepares_for"]),
    ("link_prepares_for_llm", [sys.executable, "-u", "-m", "crawlers.cn.link_prepares_for_llm"]),
    ("link_course_resources", [sys.executable, "-u", "-m", "crawlers.cn.link_course_resources"]),
    ("link_credentials", [sys.executable, "-u", "-m", "crawlers.cn.link_credentials"]),
    ("link_course_skills", [sys.executable, "-u", "-m", "crawlers.cn.link_course_skills"]),
    ("link_requires_propagate", [sys.executable, "-u", "-m", "crawlers.cn.link_requires_propagate"]),
    ("seed_learnable_search", [sys.executable, "-u", "-m", "crawlers.cn.seed_learnable_search"]),
    ("tag_cn_scope", [sys.executable, "-u", "-m", "crawlers.maintenance.tag_cn_scope"]),
    ("neo4j_migrate", [sys.executable, "-u", "-m", "backend.kg.neo4j_store.migrate"]),
    ("coverage_report", [sys.executable, "-u", "-m", "crawlers.cn.write_coverage_report"]),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    line = f"[{utc_now()}] {msg}"
    print(line, flush=True)
    ensure_dirs()
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_ckpt() -> dict:
    if CKPT.exists():
        try:
            return json.loads(CKPT.read_text(encoding="utf-8"))
        except Exception:
            return {"done": {}, "failed": {}}
    return {"done": {}, "failed": {}}


def save_ckpt(ckpt: dict) -> None:
    ensure_dirs()
    CKPT.write_text(json.dumps(ckpt, ensure_ascii=False, indent=2), encoding="utf-8")


def save_progress(data: dict) -> None:
    ensure_dirs()
    PROGRESS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def run_step(name: str, cmd: list[str], timeout: int = 0) -> tuple[bool, str]:
    log(f"START {name}: {' '.join(cmd)}")
    t0 = time.time()
    try:
        import os

        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        env["PYTHONUNBUFFERED"] = "1"
        r = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout or None,
            env=env,
        )
        dur = round(time.time() - t0, 1)
        out = (r.stdout or "")[-4000:]
        err = (r.stderr or "")[-2000:]
        if r.returncode != 0:
            log(f"FAIL {name} rc={r.returncode} {dur}s\nSTDERR:\n{err}\nSTDOUT tail:\n{out}")
            return False, f"rc={r.returncode} {err or out}"
        log(f"OK {name} {dur}s")
        # keep last stdout snippet for reports
        snippet_path = REPORTS / f"step_{name}.out.txt"
        snippet_path.write_text((r.stdout or "")[-20000:], encoding="utf-8")
        return True, f"ok {dur}s"
    except subprocess.TimeoutExpired:
        log(f"TIMEOUT {name}")
        return False, "timeout"
    except Exception as e:
        log(f"ERROR {name}: {e}\n{traceback.format_exc()}")
        return False, str(e)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="ignore checkpoint")
    parser.add_argument("--from-step", default="", help="start from this step name")
    parser.add_argument("--only", default="", help="comma-separated step names")
    args = parser.parse_args()
    ensure_dirs()
    log("=" * 60)
    log("CN full landing orchestrator start")
    pdfs = RAW / "CN" / "course" / "moe_2025" / "pdfs"
    n_pdf = len(list(pdfs.glob("*.pdf"))) if pdfs.exists() else 0
    log(f"moe pdfs on disk: {n_pdf}")

    ckpt = {"done": {}, "failed": {}} if args.force else load_ckpt()
    only = {s.strip() for s in args.only.split(",") if s.strip()} if args.only else set()
    started = not args.from_step

    results: dict = {"started_at": utc_now(), "steps": {}, "moe_pdfs": n_pdf}
    save_progress({"status": "running", **results})

    all_ok = True
    for name, cmd in STEPS:
        if only and name not in only:
            continue
        if not started:
            if name == args.from_step:
                started = True
            else:
                continue
        if name in ckpt.get("done", {}) and not args.force and not only:
            log(f"SKIP {name} (checkpoint)")
            results["steps"][name] = {"status": "skipped", "at": ckpt["done"][name]}
            save_progress({"status": "running", **results})
            continue

        # migrate can be long; courses with 700 pdfs long
        timeout = 0
        ok, msg = run_step(name, cmd, timeout=timeout)
        results["steps"][name] = {
            "status": "ok" if ok else "fail",
            "msg": msg,
            "at": utc_now(),
        }
        if ok:
            ckpt.setdefault("done", {})[name] = utc_now()
            ckpt.get("failed", {}).pop(name, None)
        else:
            all_ok = False
            ckpt.setdefault("failed", {})[name] = msg
            # critical path: continue non-fatal optional steps; stop if migrate/coverage after fail of ingest?
            # Policy: continue all steps so partial landing still progresses
            log(f"continue after fail: {name}")
        save_ckpt(ckpt)
        save_progress({"status": "running", **results})

    results["finished_at"] = utc_now()
    results["success"] = all_ok
    save_progress({"status": "done" if all_ok else "done_with_errors", **results})
    log(f"FINISHED success={all_ok}")
    # final summary path
    summary = REPORTS / "cn_full_landing_summary.json"
    summary.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
