"""
H2 · 通过 osta 公开 API 采集国家职业标准目录（无需登录）。

API:
  GET https://www.osta.org.cn/api/public/skillStandardList?pageSize=50&pageNum=1&status=1

产出:
  data/raw/CN/skill_standards/osta_catalog.json
  reports/h2_osta_api_harvest.json
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.paths import RAW, REPORTS, ensure_dirs

API = "https://www.osta.org.cn/api/public/skillStandardList"
UA = {
    "User-Agent": "Mozilla/5.0 EducationalKG/1.0 (research; vocational KG)",
    "Accept": "application/json",
    "Referer": "https://www.osta.org.cn/skillStandard",
}
DEST = RAW / "CN" / "skill_standards" / "osta_catalog.json"


def fetch_page(page_num: int, page_size: int = 50) -> dict:
    url = f"{API}?pageSize={page_size}&pageNum={page_num}&total=0&nameCode=&status=1"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8"))


def try_detail(item_id: int) -> dict:
    candidates = [
        f"https://www.osta.org.cn/api/public/skillStandardDetail?id={item_id}",
        f"https://www.osta.org.cn/api/public/skillStandardInfo?id={item_id}",
        f"https://www.osta.org.cn/api/public/getSkillStandard?id={item_id}",
        f"https://www.osta.org.cn/api/public/skillStandard/{item_id}",
        f"https://www.osta.org.cn/api/public/skillStandardPdf?id={item_id}",
        f"https://www.osta.org.cn/api/public/file/skillStandard?id={item_id}",
    ]
    out = []
    for url in candidates:
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                ct = r.headers.get("content-type", "")
                data = r.read()
                entry = {"url": url, "status": r.status, "ct": ct, "len": len(data)}
                if "json" in ct:
                    try:
                        entry["json_head"] = json.loads(data.decode("utf-8", errors="replace"))
                        # truncate
                        entry["json_head"] = json.loads(
                            json.dumps(entry["json_head"], ensure_ascii=False)[:800]
                            if False
                            else json.dumps(entry["json_head"], ensure_ascii=False)
                        )
                        s = json.dumps(entry["json_head"], ensure_ascii=False)
                        entry["json_head"] = s[:800]
                    except Exception:
                        entry["body_head"] = data[:200].decode("utf-8", errors="replace")
                elif data[:4] == b"%PDF":
                    entry["is_pdf"] = True
                else:
                    entry["body_head"] = data[:120].decode("utf-8", errors="replace")
                out.append(entry)
        except Exception as e:
            out.append({"url": url, "error": str(e)})
    return {"id": item_id, "tries": out}


def main() -> None:
    ensure_dirs()
    (RAW / "CN" / "skill_standards").mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)

    all_items: list[dict] = []
    page = 1
    total = None
    page_size = 50
    while page <= 40:
        data = fetch_page(page, page_size)
        body = data.get("body") or {}
        total = body.get("total")
        lst = body.get("list") or []
        if not lst:
            break
        all_items.extend(lst)
        print(f"page {page}: +{len(lst)} total={len(all_items)}/{total}", flush=True)
        if total and len(all_items) >= int(total):
            break
        page += 1
        time.sleep(0.2)

    DEST.write_text(
        json.dumps(
            {
                "source": API,
                "list_page": "https://www.osta.org.cn/skillStandard",
                "login_required": False,
                "count": len(all_items),
                "total_api": total,
                "items": all_items,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    detail_probe = None
    if all_items:
        iid = all_items[0].get("id")
        print("probe detail id", iid, flush=True)
        detail_probe = try_detail(int(iid))

    report = {
        "count": len(all_items),
        "total_api": total,
        "catalog_path": str(DEST),
        "sample_keys": list(all_items[0].keys()) if all_items else [],
        "sample": all_items[:3],
        "detail_probe": detail_probe,
        "login_required_for_list": False,
        "note": "列表公开 API 无需登录；PDF 详情若仍无直链需 Playwright 弹层或登录",
    }
    (REPORTS / "h2_osta_api_harvest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: report[k] for k in report if k != "detail_probe"}, ensure_ascii=False, indent=2))
    print("detail_probe", json.dumps(detail_probe, ensure_ascii=False, indent=2)[:2500])


if __name__ == "__main__":
    main()
