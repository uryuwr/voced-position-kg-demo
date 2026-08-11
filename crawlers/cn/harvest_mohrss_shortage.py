"""采集人社部「全国招聘大于求职『最缺工』的 100 个职业排行」→ 岗位需求热度。

合规说明
--------
- 站点：www.mohrss.gov.cn（人力资源社会保障部官网），**无 robots.txt**（HTTP 404），
  即未设置抓取限制；采集对象为该部**主动公开发布**的新闻稿内容。
- 页面带 JS cookie 挑战（`__tst_status`），因此用 **真实浏览器（Playwright）** 正常加载，
  不逆向计算 cookie、不绕过验证码，符合 schemas/sources.yaml 的政策要求。
- 单页访问 + 请求间隔限速；User-Agent 标明研究用途。
- 每条数据落库时记录 source_url + fetched_at。

为什么用这个源
--------------
原型岗位卡片要「高薪热招 / 极高需求 / 核心骨干」这类需求热度标签，而三大招聘站的
职位搜索页均被 robots.txt 明确禁止（zhipin `*?salary=*`、zhaopin/liepin `/*?*`）。
本排行由中国就业培训技术指导中心汇总 102 个定点监测城市的公共就业服务机构数据，
是**官方口径的紧缺程度**，可直接支撑热度标签。

用法：
    python crawlers/cn/harvest_mohrss_shortage.py --dry-run
    python crawlers/cn/harvest_mohrss_shortage.py --url <公告页URL>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 voced-kg-research/0.1 (educational research)"
)
RAW_DIR = ROOT / "data" / "raw" / "CN" / "mohrss" / "shortage"

# 已知的官方发布页（可用 --url 覆盖为更新一期）
DEFAULT_URL = (
    "https://www.mohrss.gov.cn/SYrlzyhshbzb/dongtaixinwen/buneiyaowen/rsxw/"
    "202301/t20230118_493691.html"
)


def fetch_page_and_attachment(
    url: str, pdf_dest: Path, *, timeout_ms: int = 45000
) -> tuple[str, str | None]:
    """真实浏览器加载公告页，并在**同一会话内**下载附件 PDF。

    附件必须在同一浏览器上下文里取：JS 挑战写入的 cookie 是会话级的，
    换用 urllib 单独请求会被拦（实测 HTTP 567）。
    """
    from urllib.parse import urljoin

    from playwright.sync_api import sync_playwright

    pdf_url: str | None = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, locale="zh-CN")
        page = ctx.new_page()
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        try:
            page.wait_for_function(
                "() => document.body && document.body.innerText.length > 800",
                timeout=20000,
            )
        except Exception:
            time.sleep(3)
        html = page.content()

        cands = re.findall(r"""(?:href|src)=["']([^"']+\.pdf)["']""", html, re.I)
        if cands:
            pdf_url = urljoin(url, cands[0])
            time.sleep(1.5)  # 限速
            # 用页面自身的 fetch，天然带上会话 cookie 与 Referer
            b64 = page.evaluate(
                """async (u) => {
                    const r = await fetch(u, { credentials: 'include' });
                    if (!r.ok) return null;
                    const buf = await r.arrayBuffer();
                    let s = '';
                    const bytes = new Uint8Array(buf);
                    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
                    return btoa(s);
                }""",
                pdf_url,
            )
            if b64:
                import base64

                pdf_dest.write_bytes(base64.b64decode(b64))
        ctx.close()
        browser.close()
    return html, pdf_url


def find_attachment_pdf(html: str, page_url: str) -> str | None:
    """公告正文只有说明，100 个职业在附件 PDF 里；从页面提取该附件的绝对地址。"""
    from urllib.parse import urljoin

    cands = re.findall(r"""(?:href|src)=["']([^"']+\.pdf)["']""", html, re.I)
    if not cands:
        return None
    # 形如 ./W020230118415217021121.pdf
    return urljoin(page_url, cands[0])


def parse_pdf_ranking(pdf_path: Path) -> list[dict]:
    """解析附件 PDF 的「序号 / 职业名称 / 职业代码 / 职业定义 / …」表格。

    以**职业代码**为锚点：代码格式与《职业分类大典》一致（如 4-01-02-03），
    比按名称匹配更可靠。代码前一段非空文本即职业名称，再往前是序号。
    """
    import fitz

    text_parts: list[str] = []
    with fitz.open(pdf_path) as doc:
        for page in doc:
            text_parts.append(page.get_text())
    body = "\n".join(text_parts)
    lines = [x.strip() for x in body.splitlines() if x.strip()]

    # 表头字段名会在每页重复出现，回溯职业名时要跳过
    HEADER = {"序号", "职业名称", "职业代码", "职业定义", "需求典型城市", "上期", "排位", "上期排位"}

    out: list[dict] = []
    seen_codes: set[str] = set()
    for i, line in enumerate(lines):
        m = re.match(r"^([1-8]-\d{2}-\d{2}-\d{2})\b", line)
        if not m:
            continue
        code = m.group(1)
        if code in seen_codes:
            continue
        # 职业名 = 代码上方第一个「非表头、非纯数字」的行。
        # 不从序号推排名：PDF 里两位数序号常被拆成两行（"1" + "0"），
        # 而表格本身按排名升序，故直接用代码出现顺序作为排名。
        name = ""
        for j in range(i - 1, max(-1, i - 6), -1):
            prev = lines[j].strip()
            if not prev or prev in HEADER or re.fullmatch(r"[\d\s.、．]+", prev):
                continue
            mn = re.match(r"^\d{1,3}\s+(\S.+)$", prev)   # "5 商品营业员"
            name = (mn.group(1) if mn else prev).strip()
            break
        if not name or len(name) < 2:
            continue
        # 8-00-00-00「不便分类的其他从业人员」是大典的兜底类目，
        # 会因跨页表格错位被误采；它不是一个真实紧缺职业，剔除。
        if code == "8-00-00-00" or name.startswith("不便分类"):
            continue
        seen_codes.add(code)
        out.append({"rank": len(out) + 1, "name": name, "code": code})
    return out


def demand_label(rank: int) -> str:
    """按排名分档，供前端热度标签直接使用。"""
    if rank <= 20:
        return "极高需求"
    if rank <= 50:
        return "高需求"
    return "紧缺"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print(f"加载：{args.url}")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"\W+", "_", args.url.rsplit("/", 1)[-1])[:60]
    pdf_path = RAW_DIR / f"shortage_{stamp}.pdf"
    html, pdf_url = fetch_page_and_attachment(args.url, pdf_path)

    raw_path = RAW_DIR / f"{stamp}.html"
    raw_path.write_text(html, encoding="utf-8")
    print(f"  原始页已存档：{raw_path.relative_to(ROOT)}（{len(html)} 字节）")
    if not pdf_url or not pdf_path.exists():
        print("  ⚠ 未取到附件 PDF；100 个职业列表在附件里，请核对存档 HTML")
        return 2
    print(f"  附件 PDF：{pdf_url}")
    print(f"  已存档：{pdf_path.relative_to(ROOT)}（{pdf_path.stat().st_size} 字节）")

    occs = parse_pdf_ranking(pdf_path)
    print(f"  解析出职业条目 {len(occs)} 个")
    for o in occs[:8]:
        print(f"    {o['rank']:3d}. {o['name']:16s} {o['code']}")
    if len(occs) < 50:
        print("  ⚠ 解析结果偏少，请核对存档 PDF 的表格结构")

    from backend.kg.pg_store.client import connect  # noqa: E402

    now = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, attrs FROM kg_node WHERE type='occupation'"
        ).fetchall()
        by_code: dict[str, list[dict]] = {}
        for r in rows:
            try:
                a = json.loads(r["attrs"]) if isinstance(r["attrs"], str) else (r["attrs"] or {})
            except Exception:
                a = {}
            code = (a.get("code") or "").strip()
            if code:
                by_code.setdefault(code, []).append({"id": r["id"], "attrs": a})

        matched = [o for o in occs if o["code"] in by_code]
        print(f"\n  按大典职业代码可匹配：{len(matched)} / {len(occs)}")
        for o in matched[:8]:
            print(f"    · {o['rank']:3d} {o['name']:16s} {o['code']} → {demand_label(o['rank'])}")

        if args.dry_run:
            print("\n[dry-run] 未写库")
            return 0

        updated = 0
        for o in matched:
            for node in by_code[o["code"]]:
                a = dict(node["attrs"] or {})
                a["demand"] = demand_label(o["rank"])
                a["demand_rank"] = o["rank"]
                a["demand_source"] = "MOHRSS_CN 最缺工100个职业排行"
                a["demand_source_url"] = args.url
                a["demand_fetched_at"] = now
                conn.execute(
                    "UPDATE kg_node SET attrs=%s WHERE id=%s",
                    (json.dumps(a, ensure_ascii=False), node["id"]),
                )
                updated += 1
        print(f"\n已回填 {updated} 个岗位的需求热度（attrs.demand）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
