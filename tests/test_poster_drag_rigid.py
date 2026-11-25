"""Dragging a field must move parent <text> and child <tspan> as one rigid body."""

import json
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from app.errors import BrowserUnavailable
from app.services.svg_browser import _launch

STATIC = Path(__file__).resolve().parents[1] / "static"
SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 300">
<text id="row" x="60" y="100" font-size="34" fill="#111">周六<tspan id="a" x="150">晚</tspan><tspan id="b" x="200">20:00</tspan></text>
<text id="other" x="60" y="200" font-size="20" fill="#111">Other</text>
</svg>'''
SCHEMA = {
    "canvas": {"width": 600, "height": 300},
    "images": [],
    "texts": [
        {
            "id": "row",
            "kind": "text",
            "text": "周六晚20:00",
            "targets": [{"id": "row", "kind": "text", "member_ids": ["row", "a", "b"]}],
        }
    ],
}


def _rect(page, selector):
    return page.evaluate(
        "(s) => { const r = document.querySelector(s).getBoundingClientRect();"
        " return {x: r.x, y: r.y, w: r.width}; }",
        selector,
    )


def test_drag_keeps_split_glyphs_rigid():
    with sync_playwright() as p:
        try:
            browser = _launch(p)
        except BrowserUnavailable as exc:
            pytest.skip(str(exc))
        try:
            page = browser.new_page(viewport={"width": 1550, "height": 950})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def handle(route):
                path = route.request.url.split("poster.test", 1)[1].split("?", 1)[0]
                if path.startswith("/static/"):
                    route.fulfill(path=STATIC / path.removeprefix("/static/"))
                    return
                if path.endswith("/preview"):
                    route.fulfill(body=SVG, content_type="image/svg+xml")
                    return
                body = {}
                if path.endswith("/templates"):
                    body = [{"name": "demo", "analyzed": True, "thumbnail_url": "/blank"}]
                elif path.endswith("/schema"):
                    body = SCHEMA
                elif path.endswith("/assets"):
                    body = []
                route.fulfill(body=json.dumps(body), content_type="application/json")

            page.route("http://poster.test/**", handle)
            page.goto("http://poster.test/static/poster.html")
            page.locator(".poster-stage svg").wait_for()

            before_row, before_a, before_b = (
                _rect(page, f".poster-stage #{key}") for key in ("row", "a", "b")
            )
            point = page.locator(".poster-stage #row").bounding_box()
            start = (point["x"] + 8, point["y"] + point["height"] / 2)
            page.mouse.move(*start)
            page.mouse.down()
            page.mouse.move(start[0] + 80, start[1] + 50, steps=6)
            page.wait_for_function(
                "(x) => document.querySelector('.poster-stage #a').getBoundingClientRect().x > x",
                arg=before_a["x"] + 40,
            )
            page.mouse.up()

            after_row, after_a, after_b = (
                _rect(page, f".poster-stage #{key}") for key in ("row", "a", "b")
            )
            moved_row = after_row["x"] - before_row["x"]
            moved_a = after_a["x"] - before_a["x"]
            # A child that moved further than its parent means the offset was
            # applied twice; the glyphs then tear apart.
            assert moved_a == pytest.approx(moved_row, abs=2)
            assert after_b["x"] - after_a["x"] == pytest.approx(before_b["x"] - before_a["x"], abs=1)
            assert not errors
        finally:
            browser.close()
