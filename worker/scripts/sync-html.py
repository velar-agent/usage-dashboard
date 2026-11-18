#!/usr/bin/env python3
"""화면(HTML)을 고치려면: web/server.py 의 INDEX_HTML 을 고친 뒤 이걸 실행해 src/html.js 를 다시 만들어요.
promote-dev-to-prod.py 와 같은 방식으로 이 저장소의 web/server.py 를 읽어요(precheck 가 비교하는 것과 같은 파일)."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("promote", Path(__file__).resolve().parent / "promote-dev-to-prod.py")
promote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promote)
out = promote.WORKER / "src" / "html.js"
out.write_text(promote.expected_html_js(promote.load_index_html()), encoding="utf-8")
print("wrote", out)
