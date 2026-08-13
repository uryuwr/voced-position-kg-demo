"""把飞书 docx 的 client_vars 全量块转成 Markdown。

飞书正文是虚拟滚动 SPA，DOM 抓不全；登录与否都不影响
GET /space/api/docx/pages/client_vars（公开可读文档）。

用法：
    python -X utf8 scripts/feishu_docx_to_md.py [url] [输出.md]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "https://my.feishu.cn/docx/GEoxdBCpGoTvW2xwHfHcEs3bnne"
DEFAULT_OUT = ROOT / "docs" / "用户画像接口文档.md"


def _b36(s: str) -> int:
    return int(s, 36) if s else 0


def _plain_join(text_obj: dict | None) -> str:
    if not text_obj:
        return ""
    iat = text_obj.get("initialAttributedTexts") or {}
    texts = iat.get("text") or {}
    keys = sorted(texts, key=lambda k: int(k) if str(k).isdigit() else str(k))
    return "".join("" if texts[k] is None else str(texts[k]) for k in keys)


def render_text(text_obj: dict | None) -> str:
    """Etherpad attribs → Markdown（bold / italic / strike / inlineCode）。"""
    if not text_obj:
        return ""
    raw = _plain_join(text_obj)
    if not raw:
        return ""
    iat = text_obj.get("initialAttributedTexts") or {}
    attribs = (iat.get("attribs") or {}).get("0") or (iat.get("attribs") or {}).get(0) or ""
    pool = ((text_obj.get("apool") or {}).get("numToAttrib")) or {}
    if not attribs:
        return raw.replace("\u200b", "")

    parts: list[tuple[str, list]] = []
    i = 0
    pos = 0
    cur: list = []
    n = len(attribs)
    while i < n:
        ch = attribs[i]
        if ch == "*":
            i += 1
            num = ""
            while i < n and attribs[i].isalnum():
                num += attribs[i]
                i += 1
            key = str(_b36(num))
            cur.append(pool.get(key) or pool.get(_b36(num)))
        elif ch == "|":
            i += 1
            while i < n and attribs[i].isalnum():
                i += 1
        elif ch in "+=-":
            op = ch
            i += 1
            num = ""
            while i < n and attribs[i].isalnum():
                num += attribs[i]
                i += 1
            length = _b36(num)
            if op == "+":
                parts.append((raw[pos : pos + length], list(cur)))
                pos += length
            elif op == "=":
                pos += length
            cur = []
        else:
            i += 1
    if pos < len(raw):
        parts.append((raw[pos:], []))

    out: list[str] = []
    for slice_, attrs in parts:
        s = (slice_ or "").replace("\u200b", "")
        if not s:
            continue
        names = {a[0] for a in attrs if a}
        if "inlineCode" in names:
            s = "`" + s.replace("`", "\\`") + "`"
        if "bold" in names:
            s = f"**{s}**"
        if "italic" in names:
            s = f"*{s}*"
        if "strikethrough" in names:
            s = f"~~{s}~~"
        out.append(s)
    return "".join(out)


def fetch_client_vars(url: str) -> dict:
    from playwright.sync_api import sync_playwright

    token = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0].split("#")[0]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
        data = page.evaluate(
            """async (token) => {
                const r = await fetch(
                    '/space/api/docx/pages/client_vars?id=' + token + '&source=doc',
                    { credentials: 'include' }
                );
                if (!r.ok) throw new Error('client_vars HTTP ' + r.status);
                return await r.json();
            }""",
            token,
        )
        browser.close()
    if not data or data.get("code") not in (0, None):
        raise RuntimeError(f"client_vars 失败: {data.get('code') if data else None} {data.get('msg') if data else data}")
    return data


class Converter:
    def __init__(self, payload: dict):
        data = payload["data"]
        self.bm: dict = data["block_map"]
        self.page_id: str = data["id"]

    def _b(self, bid: str) -> dict | None:
        return self.bm.get(bid)

    def cell_plain(self, cell_id: str) -> str:
        b = self._b(cell_id)
        if not b or not b.get("data"):
            return ""
        d = b["data"]
        kids = d.get("children") or []
        if not kids:
            return render_text(d.get("text")).replace("\n", "<br>")
        bits: list[str] = []
        for kid in kids:
            kb = self._b(kid)
            if not kb or not kb.get("data"):
                continue
            kd = kb["data"]
            kt = kd.get("type")
            if kt == "code":
                bits.append("`" + _plain_join(kd.get("text")).replace("\n", " ") + "`")
            else:
                bits.append(render_text(kd.get("text")).replace("\n", "<br>"))
        return "<br>".join(x for x in bits if x)

    def render_table(self, d: dict) -> str:
        cols = d.get("columns_id") or []
        rows = d.get("rows_id") or []
        cs = d.get("cell_set") or {}
        lines: list[str] = []
        for ri, rid in enumerate(rows):
            cells: list[str] = []
            for cid in cols:
                cell = cs.get(rid + cid) or {}
                bid = cell.get("block_id")
                t = self.cell_plain(bid) if bid else ""
                t = t.replace("|", "\\|").replace("\n", "<br>").strip()
                cells.append(t)
            lines.append("| " + " | ".join(cells) + " |")
            if ri == 0:
                lines.append("| " + " | ".join("---" for _ in cols) + " |")
        return "\n".join(lines)

    def render_block(self, bid: str) -> str:
        b = self._b(bid)
        if not b or not b.get("data"):
            return ""
        d = b["data"]
        if d.get("hidden"):
            return ""
        t = d.get("type")
        if t == "page":
            title = render_text(d.get("text")) or "Untitled"
            kids = [self.render_block(c) for c in (d.get("children") or [])]
            return "# " + title + "\n\n" + "\n\n".join(x for x in kids if x)
        if isinstance(t, str) and t.startswith("heading"):
            level = 1
            m = re.search(r"(\d+)$", t)
            if m:
                level = max(1, min(6, int(m.group(1))))
            return "#" * level + " " + render_text(d.get("text"))
        if t == "text":
            s = render_text(d.get("text"))
            return s if s.strip() else ""
        if t == "code":
            lang = d.get("language") or ""
            body = _plain_join(d.get("text")).rstrip()
            return f"```{lang}\n{body}\n```"
        if t == "bullet":
            s = render_text(d.get("text"))
            kids = [self.render_block(c) for c in (d.get("children") or [])]
            extra = "\n".join("  " + x.replace("\n", "\n  ") for x in kids if x)
            return "- " + s + (("\n" + extra) if extra else "")
        if t == "ordered":
            seq = str(d.get("seq") or "1")
            s = render_text(d.get("text"))
            kids = [self.render_block(c) for c in (d.get("children") or [])]
            extra = "\n".join("  " + x.replace("\n", "\n  ") for x in kids if x)
            return f"{seq}. {s}" + (("\n" + extra) if extra else "")
        if t == "table":
            return self.render_table(d)
        if t == "table_cell":
            return "\n".join(x for x in (self.render_block(c) for c in (d.get("children") or [])) if x)
        if t in ("quote_container", "quote"):
            kids = [self.render_block(c) for c in (d.get("children") or [])]
            body = "\n".join(x for x in kids if x) or render_text(d.get("text"))
            return "\n".join("> " + line for line in body.split("\n"))
        if t == "whiteboard":
            token = d.get("token") or ""
            return f"> 图：文档内含一张飞书白板示意图（token=`{token}`），Markdown 无法还原画板内容。"
        if t == "divider":
            return "---"
        if t == "todo":
            mark = "x" if (d.get("done") or d.get("checked")) else " "
            return f"- [{mark}] " + render_text(d.get("text"))
        fallback = render_text(d.get("text"))
        if fallback:
            return fallback
        if d.get("children"):
            return "\n\n".join(x for x in (self.render_block(c) for c in d["children"]) if x)
        return ""

    def to_markdown(self, source_url: str) -> str:
        md = self.render_block(self.page_id)
        md = re.sub(r"\n{3,}", "\n\n", md)
        md = re.sub(r"[ \t]+$", "", md, flags=re.M)
        header = (
            f"> 来源：[{source_url}]({source_url})  \n"
            "> 抽取说明：从飞书 `client_vars` 全量块数据转成 Markdown"
            "（含正文、表格、代码块；白板图为占位说明）\n"
        )
        if md.startswith("# "):
            nl = md.find("\n")
            md = md[: nl + 1] + "\n" + header + "\n" + md[nl + 1 :].lstrip("\n")
        else:
            md = header + "\n" + md
        return md.rstrip() + "\n"


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    raw_json = ROOT / "docs" / "_feishu_client_vars.json"

    print(f"抓取 client_vars：{url}", flush=True)
    payload = fetch_client_vars(url)
    raw_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    nblocks = len((payload.get("data") or {}).get("block_map") or {})
    print(f"块数 {nblocks}，已缓存 {raw_json}", flush=True)

    md = Converter(payload).to_markdown(url)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"已写入 {len(md)} 字 → {out}", flush=True)
    heads = re.findall(r"^#{1,3} .+$", md, flags=re.M)
    print("目录：", flush=True)
    for h in heads:
        print("  " + h, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
