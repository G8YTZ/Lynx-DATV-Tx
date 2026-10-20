#!/usr/bin/env python3
"""Syntax-checks the JavaScript in Lynx-Tx's pages before a commit.

The pages are Python strings, so a "\\n" meant for JavaScript must be
written "\\\\n" in lynx_tx.py; get it wrong and the browser sees a broken
string, the whole script stops, and the page shows nothing but grey
badges (20 Oct 2026). Needs node.  python3 tools/check_js.py
"""
import ast
import re
import subprocess
import sys
import tempfile
from pathlib import Path

src = Path(__file__).resolve().parent.parent / "lynx_tx.py"
bad = 0
for node in ast.walk(ast.parse(src.read_text())):
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and "<script>" in node.value:
        for m in re.finditer(r"<script>(.*?)</script>", node.value, re.S):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(m.group(1))
            r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
            line = node.lineno
            if r.returncode:
                bad += 1
                print(f"lynx_tx.py page string at line {line}:\n{r.stderr.strip()}\n")
            else:
                print(f"lynx_tx.py page string at line {line}: OK")
sys.exit(1 if bad else 0)
