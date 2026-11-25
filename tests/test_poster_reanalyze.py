from lxml import etree

from app.services import template_analyzer as analyzer, text_revival


def test_force_reanalyze_reuses_backup_without_reverting_current_layers(client, monkeypatch):
    name = 'retry-fonts'
    source = analyzer.source_svg_path(name)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300">
        <rect id="edited" width="11" height="13"/>
        <text id="live" font-family="AlibabaPuHuiTi" style="font-weight:bold" x="10" y="35">Live</text>
        <text id="revived-text-001" data-revived="1" font-size="148.17" font-weight="800" x="70" y="190">Recovered</text>
    </svg>''', encoding='utf-8')
    backup = analyzer.revival_backup_path(name)
    backup.write_text('''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300">
        <path id="original-path" d="M 10,10 H 100 V 40 Z"/>
    </svg>''', encoding='utf-8')
    old_backup = backup.read_bytes()

    def fake_revival(trial, *, use_llm):
        root = etree.parse(str(trial)).getroot()
        assert root.xpath('//*[@id="original-path"]')
        el = etree.SubElement(root, f'{{{analyzer.SVG_NS}}}text', {
            'id': 'revived-text-005', 'data-revived': '1', 'font-size': '154', 'font-weight': '700',
        })
        el.text = 'Recovered'
        trial.write_bytes(etree.tostring(root))
        return {'available': True, 'revived': [{'text': 'Recovered', 'new_text_id': 'revived-text-005'}],
                'skipped': [], 'warnings': [], 'revived_count': 1}

    monkeypatch.setattr(text_revival, 'revive_outlined_text', fake_revival)
    monkeypatch.setattr(analyzer.svg_browser, 'measure_svg', lambda *args: {})
    monkeypatch.setattr(analyzer.svg_browser, 'screenshot_svg', lambda path, canvas, out, **kw: out.write_bytes(b'PNG'))
    monkeypatch.setattr(analyzer.llm_client, 'llm_configured', lambda: False)
    result = client.post(f'/api/v1/posters/templates/{name}/analyze?force=true')
    assert result.status_code == 200, result.text
    assert result.json()['revived_texts'] == 1
    root = etree.parse(str(source)).getroot()
    el = root.xpath('//*[@id="revived-text-001"]')[0]
    assert (el.get('font-size'), el.get('font-weight'), el.get('x'), el.get('y')) == ('154', '700', '70', '190')
    assert root.xpath('//*[@id="edited"]')[0].get('width') == '11'
    assert not root.xpath('//*[@id="original-path"]')
    assert backup.read_bytes() == old_backup
    schema = result = client.get(f'/api/v1/posters/templates/{name}/schema').json()
    assert next(field for field in schema['texts'] if field['id'] == 'revived-text-001')['base_font_size'] == 154
