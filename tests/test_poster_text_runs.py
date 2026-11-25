"""Illustrator per-glyph runs must merge into one editable field, not many."""

from lxml import etree

from app.services import template_analyzer as analyzer
from app.services import template_renderer as renderer


MERGE_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 300">
  <text id="t" transform="translate(50 100)" font-size="40" fill="#fff">成<tspan x="72.66" y="0">为</tspan><tspan x="120" y="0">真</tspan><tspan x="170" y="0">正</tspan></text>
  <text id="s" transform="translate(50 200)" font-size="30" fill="#fff"><tspan x="0" y="0" font-size="40">—</tspan><tspan x="80" y="0" font-size="40" letter-spacing="0.05em">—</tspan><tspan x="160" y="0" font-size="22">用</tspan><tspan x="200" y="0" font-size="22">书</tspan></text>
  <text id="m" transform="translate(20 280)" font-size="20"><tspan x="20 60 90" y="0">字</tspan></text>
</svg>'''.encode("utf-8")


def _schema(monkeypatch):
    monkeypatch.setattr(analyzer.llm_client, "llm_configured", lambda: False)
    return analyzer.analyze_template(
        MERGE_SVG, name="碎字合并", use_llm=False, use_browser=False, force=True
    )


def test_identical_glyph_runs_become_one_field(monkeypatch):
    schema = _schema(monkeypatch)
    entries = {entry["text"]: entry for entry in schema["texts"]}

    assert "成为真正" in entries, sorted(entries)
    merged = entries["成为真正"]
    assert merged["kind"] == "text"
    assert merged["parent_id"] == "t"
    # A single field must not be split into per-character siblings any more.
    assert not [entry for entry in schema["texts"] if entry["parent_id"] == "t" and entry["id"] != "t"]


def test_dashes_join_but_different_sizes_stay_separate(monkeypatch):
    schema = _schema(monkeypatch)
    root = etree.parse(str(analyzer.schema_svg_path(schema))).getroot()
    runs = root.xpath('//*[@id="s"]/*[local-name()="tspan"]')

    # Two identical 40px dashes merge into one run starting at the first origin.
    assert [run.text for run in runs] == ["——", "用书"]
    assert runs[0].get("x") == "0"  # first fragment keeps its origin
    # The differently sized follow-on run keeps its own origin.
    assert runs[1].get("x") == "160"
    # Mixed sizes stay different runs, so the small text keeps its own size.
    assert [run.get("font-size") for run in runs] == ["40", "22"]
    assert runs[0].get("letter-spacing") is None  # fitted glyph spacing dropped


def test_multi_value_x_keeps_only_the_first_origin(monkeypatch):
    schema = _schema(monkeypatch)
    root = etree.parse(str(analyzer.schema_svg_path(schema))).getroot()
    for element in root.iter():
        for name in ("x", "y"):
            raw = element.get(name)
            if raw:
                assert len(raw.split()) == 1, (element.get("id"), name, raw)


def test_positioned_qr_tip_and_subtitle_survive_normalization():
    source = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1242 2201">
      <g text-anchor="start">
        <text id="qr" transform="translate(87.72 1981.42)" font-size="68.8"><tspan letter-spacing="0.05em">123456</tspan><tspan x="426.82" y="0">7</tspan></text>
        <text id="sub" transform="matrix(.9 0 0 1 327.56 1657.22)" font-size="67.18"><tspan font-size="65"><tspan x="-227.83" y="90">A</tspan><tspan x="-152.16" y="90" letter-spacing="0.12em">B</tspan></tspan></text>
      </g>
    </svg>'''
    schema = analyzer.analyze_template(source, use_llm=False, use_browser=False)
    rendered, _ = renderer.build_svg(schema, {entry['id']: entry['text'] for entry in schema['texts']})
    root = etree.fromstring(rendered.encode())
    qr, sub = (root.xpath('//*[@id=$key]', key=key)[0] for key in ('qr', 'sub'))
    assert [(run.text, run.get('x'), run.get('letter-spacing')) for run in qr] == [
        ('123456', None, '0.05em'), ('7', '426.82', None),
    ]
    assert [(run.text, run.get('x'), run.get('letter-spacing')) for run in sub[0]] == [
        ('A', '-227.83', None), ('B', '-152.16', '0.12em'),
    ]
    assert sub.get('transform') == 'matrix(.9 0 0 1 327.56 1657.22)'
    assert sub.getparent().get('text-anchor') == 'start'


def test_styles_translate_a_parent_and_child_field_only_once():
    root = etree.fromstring(
        b'<svg xmlns="http://www.w3.org/2000/svg"><text id="t"><tspan id="u">a</tspan></text></svg>'
    )
    schema = {
        "texts": [
            {"id": "t", "kind": "text", "targets": [{"id": "t", "kind": "text"}]},
            {"id": "u", "kind": "tspan", "targets": [{"id": "u", "kind": "tspan"}]},
        ]
    }
    renderer.apply_text_styles(root, schema, {"t": {"dx": 5, "dy": -2}, "u": {"dx": 5, "dy": -2}})
    text = root.find("{http://www.w3.org/2000/svg}text")
    assert text.get("transform") == "translate(5 -2)"
