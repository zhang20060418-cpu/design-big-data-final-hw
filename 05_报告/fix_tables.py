"""归一化 Markdown 表格：按内容宽度重算列宽，并清理重复的分隔行。

对每个表格块：
  1. 把块内所有行分为「分隔行」（纯 |---|）与「内容行」
  2. 第一个内容行为表头，其余为数据行
  3. 按内容宽度重新生成一条分隔行，插在表头之后
这样无论块内混入多少条分隔行、位置是否错乱，都能收敛到正确形态。
"""
import re
import unicodedata
from pathlib import Path

PATH = Path(__file__).parent / "项目报告.md"
SEP_RE = re.compile(r"^\|[\s:\-\|]+\|$")


def vis_width(s: str) -> int:
    s = re.sub(r"\*\*|`|\\\$[^\$]*\$", "", s)
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def build_sep(header: str, rows: list[str], ncol: int) -> str:
    widths = [0] * ncol
    for row in [header] + rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        for k in range(min(ncol, len(cells))):
            widths[k] = max(widths[k], vis_width(cells[k]))
    widths = [min(max(w, 4), 42) for w in widths]
    total = sum(widths) or 1
    cols = [max(3, round(56 * w / total)) for w in widths]
    return "|" + "|".join(":" + "-" * (c - 1) for c in cols) + "|"


lines = PATH.read_text(encoding="utf-8").split("\n")
out: list[str] = []
i, fixed = 0, 0

while i < len(lines):
    if lines[i].strip().startswith("|"):
        j = i
        while j < len(lines) and lines[j].strip().startswith("|"):
            j += 1
        block = lines[i:j]
        content = [r for r in block if not SEP_RE.match(r.strip())]
        if content:
            header, rows = content[0], content[1:]
            ncol = len(header.strip().strip("|").split("|"))
            out.append(header)
            out.append(build_sep(header, rows, ncol))
            out.extend(rows)
            fixed += 1
        else:
            out.extend(block)
        i = j
        continue
    out.append(lines[i])
    i += 1

PATH.write_text("\n".join(out), encoding="utf-8")
print(f"归一化表格: {fixed} 个")
