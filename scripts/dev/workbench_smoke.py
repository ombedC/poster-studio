"""开发用：驱动工作台完成解析/上传/导出并截图。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: F401  # 设置 TEMP，避免 Playwright mkdtemp EPERM
from playwright.sync_api import sync_playwright

TEXT = """2026福建九年级
福建省名优骨干教师点拨学科重难点
数学丨寻根之旅——一元二次方程专题复习
1月24日（周六）19:30
英语丨从中考真题到高分——有意义的梳理与有价值的拓展
1月24日（周六）20:30
扫码加【2026漳州中招君】为好友"""


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:8000/static/poster.html", wait_until="networkidle")
        page.wait_for_selector(".poster-stage svg", timeout=60000)

        page.fill("textarea", TEXT)
        page.get_by_role("button", name="AI 智能解析并填充").click()
        page.wait_for_timeout(4000)
        page.screenshot(path="data/workbench-parsed.png")

        page.evaluate("document.querySelector('aside').scrollTop = 9999")
        page.wait_for_timeout(500)
        page.screenshot(path="data/workbench-step3.png")

        page.get_by_role("button", name="切换模板").click()
        page.wait_for_timeout(1200)
        page.screenshot(path="data/workbench-gallery.png")
        page.keyboard.press("Escape")

        page.evaluate("document.querySelector('aside').scrollTop = 9999")
        file_inputs = page.locator("input[type=file]")
        file_inputs.nth(0).set_input_files("老师照片/7.方芬照片.jpg")
        page.wait_for_timeout(6000)
        page.screenshot(path="data/workbench-assets.png")

        print("page errors:", errors[:5])
        browser.close()


if __name__ == "__main__":
    main()
