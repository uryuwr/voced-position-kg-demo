"""从《中华人民共和国职业分类大典（2022 版）》正文 PDF 抽取职业描述，回填岗位节点。

数据来源
--------
本地已下载的官方公示稿：`data/raw/CN/mohrss/大典2022_公示稿_正文.pdf`
（source_system=MOHRSS_CN，合规登记见 schemas/sources.yaml，status=review：
 允许官方公开文件解析）。**本脚本不联网**，纯本地解析。

解析对象
--------
正文中每个细类职业的条目形如：

    2-02-01-05　钻探工程技术人员
    从事地质矿产钻探和岩土钻探技术研究、设计和指导施工的工程技术人员。
    主要工作任务:
    1. 研究、应用钻探新工艺、新技术、新方法;
    2. 进行地质矿产钻探和岩土钻探工程设计并指导施工;
    ...

抽出「职业定义」（编码行之后、"主要工作任务"之前的段落）与「主要工作任务」列表，
回填到 kg_node.description / attrs.duties —— 替换原先只有
「大典细类 4-08-03-05 · 技术辅助服务人员 · 测绘服务人员」这类分类路径占位文本。

用法：
    python crawlers/cn/extract_occupation_descriptions.py --dry-run
    python crawlers/cn/extract_occupation_descriptions.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.kg.pg_store.client import connect  # noqa: E402

PDF = ROOT / "data" / "raw" / "CN" / "mohrss" / "大典2022_公示稿_正文.pdf"

# 细类职业标题行：`2-02-01-05　钻探工程技术人员`（全角空格或空白分隔）
_TITLE = re.compile(r"^([1-8]-\d{2}-\d{2}-\d{2})[\s　]+(\S.*)$")
# 中类/小类标题（如 `2-02-02（GBM 20202）`）——遇到即中断当前职业正文
_GROUP = re.compile(r"^[1-8]-\d{2}(-\d{2})?[（(]")
_TASK_HEAD = re.compile(r"^主要工作任务\s*[:：]")
# 正文里被 PDF 换行打断的编号项：`1.` / `1．` / `2 .`
_TASK_ITEM = re.compile(r"^\s*(\d{1,2})\s*[.．、]\s*")


def _clean(s: str) -> str:
    """PDF 抽文常见问题：软换行、行首空格、全角空白、重复空格。"""
    s = s.replace("　", " ").replace("\xa0", " ")
    s = re.sub(r"\s*\n\s*", "", s)      # 中文正文换行不该留空格
    s = re.sub(r"\s{2,}", " ", s)
    # 大典在职业名后用 L / S / L/S 标注「绿色职业 / 数字职业」，会粘到定义开头
    s = re.sub(r"^(?:L/S|S/L|L|S)(?=[从在使操进])", "", s.strip())
    # 页脚页码会被当成正文尾巴（如「…从业人员。365」）
    s = re.sub(r"(?<=[。;；])\s*\d{1,4}$", "", s)
    return s.strip(" ;；,，。.")


def parse_pdf(path: Path) -> dict[str, dict]:
    import fitz

    out: dict[str, dict] = {}
    cur_code: str | None = None
    cur_name: str = ""
    buf: list[str] = []

    def flush() -> None:
        if not cur_code or not buf:
            return
        text = "\n".join(buf)
        # 切「职业定义」与「主要工作任务」
        parts = re.split(r"主要工作任务\s*[:：]", text, maxsplit=1)
        definition = _clean(parts[0])
        duties: list[str] = []
        if len(parts) > 1:
            body = parts[1]
            # 按编号切分；PDF 里编号常被换行打断，先合并再切
            merged = re.sub(r"\s*\n\s*", "", body)
            chunks = re.split(r"(?:(?<=[;；。])|^)\s*\d{1,2}\s*[.．、]", merged)
            duties = [_clean(x) for x in chunks if _clean(x)]
        if definition or duties:
            out[cur_code] = {
                "code": cur_code,
                "name": cur_name,
                "definition": definition,
                "duties": duties[:12],
            }

    with fitz.open(path) as doc:
        for page in doc:
            for raw in page.get_text().splitlines():
                line = raw.rstrip()
                if not line.strip():
                    continue
                m = _TITLE.match(line.strip())
                if m:
                    flush()
                    cur_code, cur_name = m.group(1), m.group(2).strip()
                    buf = []
                    continue
                if cur_code and _GROUP.match(line.strip()):
                    flush()
                    cur_code, buf = None, []
                    continue
                if cur_code:
                    buf.append(line)
    flush()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="仅回填前 N 条（调试用）")
    args = ap.parse_args()

    if not PDF.exists():
        print(f"缺少大典正文 PDF：{PDF}")
        return 2
    print(f"解析 {PDF.name} …")
    parsed = parse_pdf(PDF)
    print(f"  抽出职业条目 {len(parsed)} 个")

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
                by_code.setdefault(code, []).append({"id": r["id"], "name": r["name"], "attrs": a})

        hit = [c for c in by_code if c in parsed]
        print(f"  库内岗位 {len(rows)} 个，编码可匹配 {len(hit)} 个")
        samples = hit[:3]
        for c in samples:
            p = parsed[c]
            print(f"    · {c} {p['name']}：{p['definition'][:46]}… （{len(p['duties'])} 条任务）")

        if args.dry_run:
            print("\n[dry-run] 未写库")
            return 0

        updated = 0
        for code in hit:
            p = parsed[code]
            desc = p["definition"]
            if p["duties"]:
                desc += "\n主要工作任务：\n" + "\n".join(
                    f"{i}. {d}" for i, d in enumerate(p["duties"], 1)
                )
            for node in by_code[code]:
                a = dict(node["attrs"])
                a["definition"] = p["definition"]
                a["duties"] = p["duties"]
                a["desc_source"] = "MOHRSS_CN 大典2022公示稿正文"
                conn.execute(
                    "UPDATE kg_node SET description=%s, attrs=%s WHERE id=%s",
                    (desc, json.dumps(a, ensure_ascii=False), node["id"]),
                )
                updated += 1
                if args.limit and updated >= args.limit:
                    break
            if args.limit and updated >= args.limit:
                break
        print(f"\n已回填 {updated} 个岗位的职业描述")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
