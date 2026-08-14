"""把 docs/API接口文档.md 同步到**固定的**飞书云文档，并建立文内锚点。

为什么不用 `drive +import`
--------------------------
那条命令每次都新建一篇文档，链接会变。这里走 `docs +update`，文档 token 不变。

为什么要分块
------------
整篇一次提交，飞书侧会 `server time out`。按标题切成 <= CHUNK_CHARS 的块逐次
提交，每块几秒。切点只落在标题行上，不会把一张表拦腰截断。

为什么清空要单独做一次
----------------------
踩过的坑：直接拿 `overwrite` 写第一个 7KB 大块，接口回 ok，但那一块的内容会
缺失——清空与写入不是原子的，大 payload 下前半段被吞。拆成「一行占位清空 +
全量追加」后不再丢内容。所以最后还有一道写后校验：接口回 ok ≠ 内容进去了。

锚点为什么要「先写附录、再把端点插到开头」
------------------------------------------
飞书的文内跳转必须是 `文档URL#block_id`，而 block_id 要等内容写进去才存在。
若按正常顺序写完再回头改链接，改动会重建那些块、id 全变，链接当场失效。

所以倒过来：
  ① 先只写「数据模型」附录 → 每个模型标题拿到稳定的 block_id
  ② 取回 id，把端点正文里的 `GoalOut` 换成指向该 id 的链接
  ③ 用 `block_insert_after --block-id 0` 把端点部分插到文档最前面

附录块自始至终没被重写，链接因此一直有效；端点单向引用附录，不构成循环。
回顶部的链接指向标题块，它的 id 就是文档 token，写之前就已知。

用法：
    python -X utf8 scripts/openapi_to_md.py            # 先生成 Markdown
    python -X utf8 scripts/sync_api_doc_to_lark.py     # 再同步

文档 token 存在 docs/.lark_api_doc（不入库，见 .gitignore）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MD = ROOT / "docs" / "API接口文档.md"
TOKEN_FILE = ROOT / "docs" / ".lark_api_doc"
TMP = ROOT / ".tmp_lark"

DOC_URL = "https://bcnf1dzb9oqw.feishu.cn/docx/"

# 单次提交上限。8000 字符经实测稳定；调大会零星超时，调小则请求数线性增长。
CHUNK_CHARS = 8000

# lark-cli 在 Windows 上是 .cmd，且 git-bash 的 PATH 未必包含 npm 全局目录
LARK = os.environ.get("LARK_CLI") or "lark-cli"


def run(args: list[str], timeout: int = 300) -> tuple[int, str]:
    p = subprocess.run(
        [LARK, *args], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, shell=(os.name == "nt"),
    )
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def ok_of(code: int, out: str) -> bool:
    return code == 0 and '"ok": true' in out.replace('"ok":true', '"ok": true')


def split_md(md: str, limit: int = CHUNK_CHARS) -> list[str]:
    """按标题切块。切点只在标题行，保证表格与代码块完整。"""
    lines = md.split("\n")
    chunks: list[list[str]] = [[]]
    size = 0
    for ln in lines:
        if ln.startswith("## ") and size > limit * 0.6:
            chunks.append([])
            size = 0
        elif ln.startswith("### ") and size > limit:
            chunks.append([])
            size = 0
        chunks[-1].append(ln)
        size += len(ln) + 1
    return ["\n".join(c).strip() for c in chunks if "".join(c).strip()]


def write_chunks(chunks: list[str], token: str, mode: str, label: str) -> bool:
    """mode: append=追加到末尾；insert=插到文档开头（调用方需已倒序）。"""
    for i, ch in enumerate(chunks, 1):
        f = TMP / f"{label}_{i:03d}.md"
        f.write_text(ch, encoding="utf-8", newline="\n")
        rel = "./" + f.relative_to(ROOT).as_posix()   # CLI 只收仓库内相对路径
        args = ["docs", "+update", "--doc", token, "--doc-format", "markdown",
                "--content", f"@{rel}", "--json"]
        args += (
            ["--command", "append"] if mode == "append"
            else ["--command", "block_insert_after", "--block-id", "0"]
        )
        # 文档引擎会零星回 "internal error, retry later" / 超时，都是瞬时的
        ok, out = False, ""
        for attempt in range(1, 4):
            code, out = run(args)
            ok = ok_of(code, out)
            if ok:
                break
            if attempt < 3:
                print(f"      第 {attempt} 次失败，{attempt * 5}s 后重试…", flush=True)
                time.sleep(attempt * 5)
        head = ch.split("\n", 1)[0][:44]
        print(f"  [{label} {i:>2}/{len(chunks)}] {len(ch):>6,}字  "
              f"{'OK' if ok else '失败'}  {head}", flush=True)
        if not ok:
            print("    " + out.strip()[-400:])
            return False
    return True


def headings(md: str) -> list[str]:
    """二级标题文本，用作写后校验的抽查点。"""
    return [ln[3:].strip() for ln in md.split("\n") if ln.startswith("## ") and ln[3:].strip()]


def main() -> int:
    if not MD.exists():
        print(f"缺少 {MD}，先跑：python -X utf8 scripts/openapi_to_md.py")
        return 1
    token = (
        sys.argv[1] if len(sys.argv) > 1
        else (TOKEN_FILE.read_text(encoding="utf-8").strip() if TOKEN_FILE.exists() else "")
    )
    if not token:
        print(
            "没有目标文档 token。先建一篇：\n"
            '  lark-cli docs +create --title "职业教育知识图谱 API 接口文档"\n'
            f"再把 document_id 写进 {TOKEN_FILE}，或作为参数传给本脚本"
        )
        return 1

    md = MD.read_text(encoding="utf-8")
    TMP.mkdir(exist_ok=True)

    marker = "\n## 数据模型\n"
    if marker not in md:
        print("文档里没有「## 数据模型」小节，锚点无从建立")
        return 1
    head, tail = md.split(marker, 1)
    appendix = marker.lstrip("\n") + tail

    # 每个模型标题下加一行回顶部（标题块 id 即文档 token，写之前就已知）
    appendix = re.sub(
        r"^(### \w+)$",
        lambda m: f"{m.group(1)}\n\n[↑ 回到顶部]({DOC_URL}{token}#{token})",
        appendix,
        flags=re.M,
    )
    print(f"{len(md):,} 字符　端点 {len(head):,} / 附录 {len(appendix):,}　目标 {token}")

    # 清空（小 payload，可靠）
    clear = TMP / "clear.md"
    clear.write_text("(同步中…)", encoding="utf-8", newline="\n")
    code, out = run([
        "docs", "+update", "--doc", token, "--command", "overwrite",
        "--doc-format", "markdown", "--content", "@./" + clear.relative_to(ROOT).as_posix(),
        "--json",
    ])
    if not ok_of(code, out):
        print("清空失败：" + out.strip()[-300:])
        return 1
    print("  [清空] OK")

    # ① 附录
    if not write_chunks(split_md(appendix), token, "append", "附录"):
        return 1

    # ② 取模型标题的 block_id
    code, out = run(["docs", "+fetch", "--doc", token, "--detail", "with-ids", "--json"], 300)
    if not ok_of(code, out):
        print("取 block_id 失败，无法建锚点：" + out.strip()[-300:])
        return 1
    ids = {
        m.group(2): m.group(1)
        for m in re.finditer(r'<h3 id=\\"([^\\"]+)\\">([A-Za-z_]\w*)</h3>', out)
    }
    print(f"  拿到 {len(ids)} 个模型的 block_id")
    if not ids:
        print("  一个都没匹配到，锚点会全部落空，先别写端点")
        return 1

    # ③ 端点正文里的 `模型名` → 指向附录的链接
    def to_link(m: re.Match[str]) -> str:
        bid = ids.get(m.group(1))
        return f"[`{m.group(1)}`]({DOC_URL}{token}#{bid})" if bid else m.group(0)

    head = re.sub(r"`([A-Za-z_]\w*)`", to_link, head)
    print(f"  端点正文建立 {head.count(DOC_URL)} 处锚点")

    # ④ 端点插到最前面。每块都插在「文档开头」之后，所以要倒序插，
    #    最后插进去的那块才会排在最前。
    if not write_chunks(list(reversed(split_md(head))), token, "insert", "端点"):
        return 1

    for f in TMP.glob("*.md"):
        f.unlink()

    # 写后校验：接口回 ok ≠ 内容进去了，上一版就是每块都 OK 却少了整个第一块
    print("\n校验…")
    code, out = run(["docs", "+fetch", "--doc", token, "--json"], 300)
    if code != 0:
        print("  取回文档失败，请人工打开确认")
    else:
        miss = [h for h in headings(md) if h not in out]
        if miss:
            print(f"  少了 {len(miss)} 个标题：{'、'.join(miss[:6])}")
            print("  重跑一次；若仍缺，把 CHUNK_CHARS 调小")
            return 1
        print(f"  {len(headings(md))} 个标题全部就位，{out.count(DOC_URL + token + '#')} 处锚点")

    print(f"\n完成：{DOC_URL}{token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
