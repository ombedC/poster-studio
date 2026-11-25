from pathlib import Path

import pytest

from app.errors import BrowserUnavailable
from app.services import svg_browser, text_revival


def test_neighbor_must_be_aligned_close_and_same_visual_height():
    near = {"id": "near", "box": [20, 40, 200, 105], "anchor": "start",
            "scale": [1, 1], "weight": 700, "size": 154}
    target = (20, 180, 210, 100)
    assert text_revival._title_reference(target, [near], (600, 500)) is near
    assert text_revival._title_reference(target, [{**near, "box": [20, 40, 200, 109]}], (600, 500)) is None
    assert text_revival._title_reference(target, [{**near, "box": [300, 40, 200, 105]}], (600, 500)) is None


def test_browser_revival_matches_outline_in_both_dimensions(tmp_path: Path):
    source = tmp_path / "ink.svg"
    source.write_text('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 600">
      <text id="neighbor" x="400" y="175" text-anchor="middle" font-family="Arial" font-size="154" font-weight="700">TITLE</text>
    </svg>''', encoding="utf-8")
    try:
        with svg_browser.text_ink_probe(source, (800, 600)) as probe:
            reference = {**probe("neighbor")[0], "id": "neighbor", "container_id": "__root__"}
    except BrowserUnavailable as exc:
        pytest.skip(str(exc))
    x, y, w, h = reference["box"]
    target = (x, y + 160, w, h)
    root = text_revival._parse_svg(source.read_bytes())
    path = root.makeelement(f"{{{text_revival.SVG_NS}}}path", id="outline", d=f"M{x} {y+160}h{w}v{h}h{-w}z")
    root.append(path)
    line = {"text": "TITLE", "bbox": target}
    result, report, reason = text_revival._try_line(root, line, [{"paths": [{"id": "outline"}], "box": target}],
                                                   (800, 600), tmp_path, 1, [reference])
    assert reason is None, reason
    assert report["font_size"] == 154 and report["font_weight"] == 700
    assert report["reference_text_id"] == "neighbor"
    assert max(report["width_error"], report["height_error"]) < 0.01
    assert result.xpath('//*[@id="outline"]') == []
    mismatch = (x, y + 160, 0.65 * w, h)
    result, report, reason = text_revival._try_line(root, line, [{"paths": [{"id": "outline"}], "box": mismatch}],
                                                   (800, 600), tmp_path, 2, [])
    assert result is report is None and "1%" in reason
    assert root.xpath('//*[@id="outline"]') == [path]
