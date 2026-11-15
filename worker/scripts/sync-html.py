#!/usr/bin/env python3
"""화면(HTML)을 고치려면: web/server.py 의 INDEX_HTML 을 고친 뒤 이걸 실행해 src/html.js 를 다시 만들어요."""
import json, sys
from pathlib import Path
sys.path.insert(0, "/workspace/token-usage/web")
import server
out = Path(__file__).resolve().parent.parent / "src" / "html.js"
out.write_text("// server.py 의 INDEX_HTML 을 그대로 옮긴 파일 (scripts/sync-html.py 로 다시 만들 수 있어요)\nexport default " + json.dumps(server.INDEX_HTML, ensure_ascii=False) + ";\n", encoding="utf-8")
print("wrote", out)
