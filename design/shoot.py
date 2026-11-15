#!/usr/bin/env python3
"""시안 스크린샷 생성: /tmp/pwv 같은 venv에 playwright 설치 후 실행 (executable_path=/usr/bin/google-chrome)."""
import os
from playwright.sync_api import sync_playwright
D = os.path.dirname(os.path.abspath(__file__)); OUT = os.path.join(D, "screens"); os.makedirs(OUT, exist_ok=True)
SHOTS = [  # name, width, height, query, full_page
 ("desktop-light",1440,900,"state=ok&theme=light",True),
 ("desktop-dark",1440,900,"state=ok&theme=dark",True),
 ("mobile-light",390,844,"state=ok&theme=light",True),
 ("mobile-dark",390,844,"state=ok&theme=dark",True),
 ("desktop-empty-light",1440,900,"state=empty&theme=light",True),
 ("desktop-empty-dark",1440,900,"state=empty&theme=dark",True),
 ("mobile-empty-light",390,844,"state=empty&theme=light",True),
 ("mobile-empty-dark",390,844,"state=empty&theme=dark",True),
 ("desktop-error-light",1440,900,"state=error&theme=light",False),
 ("desktop-stale-dark",1440,900,"state=stale&theme=dark",False),
 ("desktop-loading-light",1440,900,"state=loading&theme=light",False),
 ("desktop-sparse-light",1440,900,"state=sparse&theme=light",True),
 ("desktop-raw-open-light",1440,900,"state=ok&theme=light&raw=open",True),
 ("tablet-light",820,1100,"state=ok&theme=light",True),
 ("mobile-error-light",390,844,"state=error&theme=light",False),
]
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    for name,w,h,qs,full in SHOTS:
        pg = b.new_page(viewport={"width":w,"height":h}, device_scale_factor=1)
        pg.goto(f"file://{D}/index.html?{qs}", wait_until="networkidle")
        pg.evaluate("document.fonts.ready"); pg.wait_for_timeout(400)
        pg.screenshot(path=f"{OUT}/{name}.png", full_page=full)
        print(name, pg.evaluate("document.documentElement.scrollHeight"))
        pg.close()
    b.close()
