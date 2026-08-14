"""一次性：找 backend/ 里重复的代码块（≥N 行完全相同，忽略缩进与空行）。

「有没有冗余」靠肉眼读 21000 行是读不出来的，用滑窗哈希机械地找。
只报非平凡块（含实际语句，不是纯注释/import/括号）。
"""
from __future__ import annotations

import collections
import hashlib
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
BE = ROOT / "backend"
WIN = 8          # 窗口行数


def norm(line: str) -> str:
    return line.strip()


def trivial(block: list[str]) -> bool:
    """纯注释 / import / 结构符号的块不算冗余。"""
    meat = [
        b for b in block
        if b and not b.startswith(("#", '"""', "'''", "from ", "import "))
        and b not in (")", "]", "}", "):", "],", "},", "else:", "try:", "pass")
    ]
    return len(meat) < WIN // 2


def main() -> None:
    seen: dict[str, list[tuple[str, int]]] = collections.defaultdict(list)
    for f in sorted(BE.rglob("*.py")):
        if "__pycache__" in str(f) or "neo4j_store" in str(f):
            continue
        lines = [norm(x) for x in f.read_text(encoding="utf-8").split("\n")]
        keep = [(i, x) for i, x in enumerate(lines, 1) if x]
        for i in range(len(keep) - WIN + 1):
            win = [x for _, x in keep[i:i + WIN]]
            if trivial(win):
                continue
            h = hashlib.md5("\n".join(win).encode()).hexdigest()
            seen[h].append((str(f.relative_to(ROOT)), keep[i][0]))

    dups = {h: locs for h, locs in seen.items() if len(locs) > 1}
    # 合并相邻窗口，只报每段的起点
    reported: set[tuple[str, int]] = set()
    groups: list[list[tuple[str, int]]] = []
    for h, locs in sorted(dups.items(), key=lambda kv: -len(kv[1])):
        if any((f, ln - 1) in reported for f, ln in locs):
            continue
        for loc in locs:
            reported.add(loc)
        groups.append(locs)

    print(f"=== {WIN} 行以上完全重复的代码块：{len(groups)} 组 ===")
    for locs in groups[:20]:
        print(f"  重复 {len(locs)} 处：")
        for f, ln in locs:
            print(f"      {f}:{ln}")


if __name__ == "__main__":
    main()
