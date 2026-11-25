"""开发诊断：检查工作台内联 SVG 是否命中本地字体。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: F401
from playwright.sync_api import sync_playwright

JS = """
() => {
  const svg = document.querySelector('.poster-stage svg');
  const texts = [...svg.querySelectorAll('text')];
  const sample = texts.map((t) => ({
    id: t.id,
    ff: t.getAttribute('font-family'),
    fs: t.getAttribute('font-size'),
    txt: (t.textContent || '').slice(0, 8),
  })).filter((x) => x.ff && x.ff.includes('PuHuiTi')).slice(0, 6);
  return {
    checkB: document.fonts.check('66px AlibabaPuHuiTiB'),
    checkM: document.fonts.check('66px AlibabaPuHuiTiM'),
    checkH: document.fonts.check('66px AlibabaPuHuiTiH'),
    fonts: [...document.fonts].filter((f) => f.family.includes('PuHuiTi')).map((f) => f.family + ':' + f.status),
    sample,
    current: Alpine.$data(document.body).current && Alpine.$data(document.body).current.name,
  };
}
"""

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    page.goto("http://127.0.0.1:8000/static/poster.html", wait_until="networkidle")
    page.wait_for_selector(".poster-stage svg", timeout=60000)
    print("templates", page.evaluate("Alpine.$data(document.body).templates.map((t) => t.name)"))
    page.evaluate("Alpine.$data(document.body).selectTemplate(Alpine.$data(document.body).templates[1])")
    page.wait_for_timeout(9000)
    print(page.evaluate(JS))
    page.screenshot(path="data/diag-new-template.png")
    browser.close()
