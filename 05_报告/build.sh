#!/usr/bin/env bash
# 报告构建脚本：md -> tex -> pdf
# 用法：bash build.sh
set -euo pipefail
cd "$(dirname "$0")"

PY="C:/Users/PC/.workbuddy/binaries/python/versions/3.13.12/python.exe"

echo "[1/4] pandoc 生成 tex"
pandoc 项目报告.md -o _build.tex -s --toc --toc-depth=1 \
  --pdf-engine=xelatex -V fontsize=11pt -H header.tex \
  --metadata date="2026 年 10 月"

echo "[2/4] 用 TikZ 替换数据流图"
"$PY" replace_flow.py

echo "[3/4] xelatex 编译三遍"
for i in 1 2 3; do
  xelatex -interaction=nonstopmode _build.tex > "_p$i.log" 2>&1 || {
    echo "第 $i 遍编译失败，日志尾部："; tail -20 "_p$i.log"; exit 1; }
done

echo "[4/4] 输出"
cp _build.pdf 项目报告.pdf
grep -c "Missing character" _p3.log 2>/dev/null || true
pdfinfo 项目报告.pdf 2>/dev/null | grep Pages || true
echo "完成：项目报告.pdf"
