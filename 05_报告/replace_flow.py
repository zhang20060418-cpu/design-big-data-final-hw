"""把 pandoc 生成的 ASCII 数据流图替换为 TikZ 版本。

原因：verbatim 环境中中文字符宽度为等宽字符的两倍，
逐字符对齐的 ASCII 图会整体错位。
"""
import re
from pathlib import Path

here = Path(__file__).parent
tex = (here / "_build.tex").read_text(encoding="utf-8")
tikz = (here / "_flow.tex").read_text(encoding="utf-8")

pattern = re.compile(r"\\begin\{verbatim\}.*?\\end\{verbatim\}", re.S)
hits = 0


def repl(m: re.Match) -> str:
    global hits
    block = m.group(0)
    if "结构解析" in block and "逐页 PNG" in block:
        hits += 1
        return tikz.rstrip()
    return block


tex = pattern.sub(repl, tex)
(here / "_build.tex").write_text(tex, encoding="utf-8")
print(f"  替换数据流图: {hits} 处")
if hits != 1:
    raise SystemExit("数据流图定位异常，请检查 md 中的代码块")
