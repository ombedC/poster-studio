"""灵动岛修复回归：失焦提醒、通知引导点、试听提示音、纯色 GPU 外壳。"""
from playwright.sync_api import expect

from test_dynamic_island_browser import SHELL, island  # noqa: F401  (pytest fixture)


def test_island_shell_is_solid_and_gpu_friendly(island):  # noqa: F811
    page, _ = island
    page.get_by_role('button', name='导出高清 PNG').click()
    page.wait_for_selector(f'{SHELL}[data-island-state="success"]')
    styles = page.evaluate(
        """() => {
            const root = document.querySelector('#dynamic-island-root');
            const shell = root.querySelector('.di-shell');
            const expanded = root.querySelector('.di-expanded');
            const cs = getComputedStyle(shell);
            return {
                top: getComputedStyle(root).top,
                background: cs.backgroundColor,
                backdrop: cs.backdropFilter || cs.webkitBackdropFilter || 'none',
                contain: cs.contain,
                shellTransition: cs.transitionProperty,
                expandedTransition: getComputedStyle(expanded).transitionProperty,
                expandedFilter: getComputedStyle(expanded).filter,
            };
        }"""
    )
    assert styles['top'] == '6px'
    assert styles['background'] == 'rgb(20, 20, 24)'
    assert styles['backdrop'] in ('none', '')
    assert 'blur' not in styles['shellTransition']
    assert 'filter' not in styles['expandedTransition']
    assert styles['expandedFilter'] == 'none'
    assert styles['contain'] == 'layout paint'


def test_blurred_window_with_visible_page_triggers_background_feedback(island):  # noqa: F811
    """切到微信时 document.hidden 仍是 false，只靠 blur + hasFocus 也要提醒。"""
    page, _ = island
    original = page.evaluate('() => document.title')
    page.evaluate(
        """() => {
            window.__notes = [];
            window.__tones = 0;
            class FakeNotification {
                static permission = 'granted';
                static requestPermission() { return Promise.resolve('granted'); }
                constructor(title, options) { window.__notes.push([title, options]); }
                close() {}
            }
            window.Notification = FakeNotification;
            window.AudioContext = class {
                constructor() { this.currentTime = 0; this.state = 'running'; this.destination = {}; }
                resume() { return Promise.resolve(); }
                createOscillator() { window.__tones += 1;
                    return { type: '', frequency: { value: 0 }, connect: (n) => n, start() {}, stop() {} }; }
                createGain() { return { gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} },
                    connect: (n) => n }; }
            };
            Object.defineProperty(document, 'hasFocus', {configurable: true, value: () => false});
            window.dispatchEvent(new Event('blur'));
            DynamicIsland.setNotify(true);
        }"""
    )
    assert page.evaluate('() => document.hidden') is False

    page.get_by_role('button', name='导出高清 PNG').click()
    page.wait_for_function("document.title.startsWith('✅')")
    assert page.evaluate('() => window.__notes.length') == 1
    assert page.evaluate('() => window.__tones >= 2') is True
    assert page.evaluate(
        '() => document.querySelector("link[rel=icon]").href.startsWith("data:image/png")'
    ) is True

    page.evaluate(
        """() => {
            Object.defineProperty(document, 'hasFocus', {configurable: true, value: () => true});
            window.dispatchEvent(new Event('focus'));
        }"""
    )
    page.wait_for_function('() => !document.title.startsWith("✅")', timeout=4000)
    assert page.evaluate('() => document.title') == original
    assert page.evaluate(
        '() => document.querySelector("link[rel=icon]").href.startsWith("data:image/png")'
    ) is False


def test_notification_permission_guide_dot_and_hint(island):  # noqa: F811
    page, _ = island
    page.evaluate(
        """() => {
            class PendingNotification {
                static permission = 'default';
                static requestPermission() { this.permission = 'granted'; return Promise.resolve('granted'); }
            }
            window.Notification = PendingNotification;
            DynamicIsland.setNotify(false);
            DynamicIsland.start('guide', {title: '引导测试'});
        }"""
    )
    bell = page.locator('[data-tool="notify"]')
    assert bell.get_attribute('data-guide') == '1'
    page.locator(SHELL).hover()
    page.wait_for_function(
        "getComputedStyle(document.querySelector('#dynamic-island-root .di-shell'))"
        ".borderTopLeftRadius === '24px'"
    )
    hint = page.locator('[data-hint="notify"]')
    expect(hint).to_be_visible()
    hint.click()
    assert page.evaluate('() => DynamicIsland.notify') is True
    assert bell.get_attribute('data-guide') is None
    expect(hint).to_be_hidden()


def test_sound_toggle_plays_preview_chime(island):  # noqa: F811
    page, _ = island
    page.evaluate(
        """() => {
            window.__tones = 0;
            window.AudioContext = class {
                constructor() { this.currentTime = 0; this.state = 'running'; this.destination = {}; }
                resume() { return Promise.resolve(); }
                createOscillator() { window.__tones += 1;
                    return { type: '', frequency: { value: 0 }, connect: (n) => n, start() {}, stop() {} }; }
                createGain() { return { gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} },
                    connect: (n) => n }; }
            };
            DynamicIsland.setSound(true);
        }"""
    )
    assert page.evaluate('() => window.__tones') >= 2
