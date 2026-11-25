"""开发验证：画布原位改字 / 图片浮层。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: F401
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8000/static/poster.html", wait_until="networkidle")
    page.wait_for_selector(".poster-stage svg", timeout=60000)
    page.evaluate("Alpine.$data(document.body).selectTemplate(Alpine.$data(document.body).templates[1])")
    page.wait_for_timeout(9000)

    box = page.evaluate(
        """() => {
          const el = document.querySelector('.poster-stage svg #text-012');
          if (!el) return null;
          const r = el.getBoundingClientRect();
          return {x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, h: r.height};
        }"""
    )
    print("title box", box)
    if box:
        page.mouse.click(box["x"], box["y"])
        page.wait_for_timeout(600)
        print("editing", page.evaluate("Alpine.$data(document.body).editing"))
        page.screenshot(path="data/wysiwyg-edit.png")
        page.fill(".fixed input[x-ref='inlineInput']", "2026厦门九年级")
        page.keyboard.press("Enter")
        page.wait_for_timeout(5000)
        text = page.evaluate(
            "document.querySelector('.poster-stage svg #text-012')?.textContent"
        )
        print("new title", text)
        page.screenshot(path="data/wysiwyg-after.png")

    img = page.evaluate(
        """() => {
          const el = document.querySelector('.poster-stage svg image[data-image-key="portrait_1"]');
          if (!el) return null;
          const r = el.getBoundingClientRect();
          return {x: r.x + r.width / 2, y: r.y + r.height / 2};
        }"""
    )
    print("image box", img)
    if img:
        page.mouse.click(img["x"], img["y"])
        page.wait_for_timeout(800)
        print("popover", page.evaluate("Alpine.$data(document.body).imagePopover"))
        page.screenshot(path="data/wysiwyg-image.png")

    print("errors", errors[:5])
    browser.close()
