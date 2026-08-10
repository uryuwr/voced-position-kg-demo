"""
从 osta_catalog.json 批量下载标准 PDF（公开 decrypt 接口，无需登录）。

  GET https://www.osta.org.cn/api/sys/downloadFile/decrypt?fileName=...

若出现验证码/403，会写入 reports/h2_WAIT_FOR_LOGIN.txt 并暂停。

Usage:
  python -m crawlers.cn.batch_download_osta_catalog_pdfs
  python -m crawlers.cn.batch_download_osta_catalog_pdfs --limit 50
  python -m crawlers.cn.batch_download_osta_catalog_pdfs --resume
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.paths import RAW, REPORTS, ensure_dirs

CATALOG = RAW / "CN" / "skill_standards" / "osta_catalog.json"
DEST = RAW / "CN" / "skill_standards" / "osta_pdfs"
DOWNLOAD = "https://www.osta.org.cn/api/sys/downloadFile/decrypt"
UA = {
    "User-Agent": "Mozilla/5.0 EducationalKG/1.0 (research; vocational KG)",
    "Accept": "*/*",
    "Referer": "https://www.osta.org.cn/skillStandard",
}
WAIT_FLAG = REPORTS / "h2_login_ready.flag"
WAIT_MSG = REPORTS / "h2_WAIT_FOR_LOGIN.txt"


def safe_name(item: dict) -> str:
    code = (item.get("code") or "unknown").replace("/", "-")
    name = re.sub(r'[\\/:*?"<>|]', "_", item.get("name") or "standard")
    name = name[:40]
    return f"{code}_{name}.pdf"


def download_one(file_key: str, path: Path, timeout: int = 90) -> tuple[bool, str]:
    if path.exists() and path.stat().st_size > 8000:
        head = path.read_bytes()[:5]
        if head.startswith(b"%PDF"):
            return True, "skip"
    q = urllib.parse.urlencode({"fileName": file_key})
    url = f"{DOWNLOAD}?{q}"
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)
    if data[:4] != b"%PDF":
        # maybe captcha html
        text = data[:500].decode("utf-8", errors="replace")
        if any(k in text for k in ("登录", "验证", "captcha", "滑块", "403", "Forbidden")):
            return False, "need_login_or_captcha"
        return False, f"not_pdf:{text[:80]!r}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True, f"ok:{len(data)}"


def pause_for_human(reason: str) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    msg = f"""# H2 需要你协助（下载被拦）

原因: {reason}

## 请你做

1. 浏览器打开: https://www.osta.org.cn/skillStandard
2. 如出现登录/滑块验证，请完成
3. 能正常点「点击查看」并看到 PDF 后，创建标记文件:

   New-Item -ItemType File -Force -Path "{WAIT_FLAG}"

4. 在聊天回复「已登录」或等我检测到标记后继续

（当前脚本会等待标记最多 15 分钟）
"""
    WAIT_MSG.write_text(msg, encoding="utf-8")
    print(msg, flush=True)
    if WAIT_FLAG.exists():
        try:
            WAIT_FLAG.unlink()
        except Exception:
            pass
    t0 = time.time()
    while time.time() - t0 < 900:
        if WAIT_FLAG.exists():
            print("resume after human login", flush=True)
            try:
                WAIT_FLAG.unlink()
            except Exception:
                pass
            return
        time.sleep(3)
    print("human wait timeout", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.35)
    parser.add_argument("--pause-on-block", action="store_true", default=True)
    args = parser.parse_args()
    ensure_dirs()
    if not CATALOG.exists():
        print("missing catalog, run: python -m crawlers.cn.harvest_osta_skill_api")
        sys.exit(1)
    cat = json.loads(CATALOG.read_text(encoding="utf-8"))
    items = cat.get("items") or []
    if args.limit:
        items = items[: args.limit]
    DEST.mkdir(parents=True, exist_ok=True)
    ok = skip = fail = 0
    fails = []
    for i, item in enumerate(items, 1):
        key = item.get("standardInfo")
        if not key or not str(key).endswith(".pdf"):
            fail += 1
            fails.append({"name": item.get("name"), "err": "no standardInfo"})
            continue
        path = DEST / safe_name(item)
        good, msg = download_one(str(key), path)
        if good and msg == "skip":
            skip += 1
        elif good:
            ok += 1
            print(f"OK {i}/{len(items)} {path.name} {msg}", flush=True)
        else:
            fail += 1
            fails.append({"name": item.get("name"), "err": msg, "key": key})
            print(f"FAIL {i}/{len(items)} {item.get('name')} {msg}", flush=True)
            if args.pause_on_block and "need_login" in msg:
                pause_for_human(msg)
                # retry once
                good2, msg2 = download_one(str(key), path)
                if good2:
                    ok += 1
                    fail -= 1
                    fails.pop()
                    print(f"RETRY OK {path.name}", flush=True)
        if i % 20 == 0:
            print(f"progress {i}/{len(items)} ok={ok} skip={skip} fail={fail}", flush=True)
        time.sleep(args.sleep)

    report = {
        "total": len(items),
        "downloaded": ok,
        "skipped": skip,
        "failed": fail,
        "dest": str(DEST),
        "fail_sample": fails[:40],
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "batch_download_osta_catalog_pdfs.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
