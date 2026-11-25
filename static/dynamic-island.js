/* 灵动岛：全局异步任务反馈壳层。
 * 零依赖、自注入样式；不触碰页面 SVG / 草图状态，只操作自己的 DOM。
 */
(function () {
  'use strict';
  if (typeof window === 'undefined' || window.DynamicIsland) return;

  const MAX_VISIBLE = 3;
  const SUCCESS_HOLD = 3500;
  const COLLAPSE_DELAY = 180;
  const EASE = 'cubic-bezier(0.32, 1.25, 0.45, 1)';
  const SOUND_KEY = 'poster.island.sound';
  const NOTIFY_KEY = 'poster.island.notify';
  const GREEN = '#34d399';
  const CORAL = '#fb7185';

  const readFlag = (key, fallback) => {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? fallback : raw === '1';
    } catch (error) {
      return fallback;
    }
  };
  const writeFlag = (key, on) => {
    try { localStorage.setItem(key, on ? '1' : '0'); } catch (error) { /* 隐私模式忽略 */ }
  };

  let soundOn = readFlag(SOUND_KEY, true);
  let notifyOn = readFlag(NOTIFY_KEY, false);

  const tasks = new Map();
  const el = {};
  let root = null;
  let hovered = false;
  let expanded = false;
  let collapseTimer = 0;
  let holdTimer = 0;
  let holdDeadline = 0;
  let holdRemaining = 0;
  let tickTimer = 0;
  let audio = null;
  let attentionToken = 0;
  let savedTitle = null;
  let savedIcon = null;

  const icon = (name) => ({
    sound: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6.5 9H3v6h3.5L11 19z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18 6a8.5 8.5 0 0 1 0 12"/></svg>',
    mute: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6.5 9H3v6h3.5L11 19z"/><path d="M16 9.5l4 5M20 9.5l-4 5"/></svg>',
    bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M18 15.5V10a6 6 0 1 0-12 0v5.5L4.5 18h15z"/><path d="M10 21h4"/></svg>',
    bellOff: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6 15.5V10a6 6 0 0 1 9.3-5.1"/><path d="M18 11.2v4.3L19.5 18h-15L6 15.5"/><path d="M4 4l16 16"/><path d="M10 21h4"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5 10 17.5 19 7"/></svg>',
    alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 7v6M12 17h.01"/><circle cx="12" cy="12" r="9"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M7 7l10 10M17 7 7 17"/></svg>',
  })[name] || '';

  const mmss = (ms) => {
    const total = Math.max(0, Math.floor(ms / 1000));
    return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
  };
  const active = () => tasks.size > 0;
  const ordered = () => [...tasks.values()];
  const clip = (text, max = 18) => {
    const value = String(text || '').replace(/\s+/g, ' ').trim();
    return value.length > max ? value.slice(0, max - 1) + '…' : value;
  };

  /* ---------- 样式 ---------- */
  const STYLE = `
.di-root { position: fixed; top: 6px; left: 50%; transform: translateX(-50%); z-index: 60;
  pointer-events: none; font-family: 'Segoe UI', 'Microsoft YaHei UI', system-ui, sans-serif;
  color: #f8fafc; -webkit-font-smoothing: antialiased; }
.di-root[hidden] { display: none; }
.di-shell { position: relative; pointer-events: auto; overflow: hidden; box-sizing: border-box;
  background: #141418; border: 1px solid rgba(255, 255, 255, 0.09);
  box-shadow: 0 14px 34px -14px rgba(0, 0, 0, 0.72), 0 3px 10px -6px rgba(0, 0, 0, 0.55),
    inset 0 1px 0 rgba(255, 255, 255, 0.07);
  contain: layout paint; will-change: width, height;
  border-radius: 9999px; width: 200px; height: 38px;
  transition: width .30s ${EASE}, height .30s ${EASE}, border-radius .30s ${EASE};
  transform-origin: 50% 0; }
.di-root.di-enter .di-shell { animation: di-in .30s ${EASE} both; }
@keyframes di-in { from { opacity: 0; transform: scale(.9); } to { opacity: 1; transform: none; } }
.di-layer { position: absolute; top: 0; left: 0; overflow: hidden; }
.di-collapsed { height: 38px; width: max-content; max-width: 270px; min-width: 200px;
  display: flex; align-items: center; gap: 8px; padding: 0 12px; box-sizing: border-box;
  transition: opacity .14s linear; }
.di-expanded { width: min(360px, calc(100vw - 24px)); padding: 10px 12px 12px; box-sizing: border-box;
  opacity: 0; transform: translate3d(0, -4px, 0);
  transition: opacity .14s linear, transform .18s ease;
  pointer-events: none; }
.di-root.di-open .di-collapsed { opacity: 0; pointer-events: none; }
.di-root.di-open .di-expanded { opacity: 1; transform: translate3d(0, 0, 0); pointer-events: auto;
  transition: opacity .22s linear .06s, transform .26s ${EASE} .06s; }
.di-ring { flex: none; width: 14px; height: 14px; border-radius: 9999px; box-sizing: border-box; }
.di-ring[data-state="running"] { border: 2px solid rgba(129, 140, 248, 0.28); border-top-color: #818cf8;
  animation: di-spin .9s linear infinite; }
.di-ring[data-state="success"] { background: ${GREEN}; box-shadow: 0 0 0 3px rgba(52, 211, 153, 0.16); }
.di-ring[data-state="fail"] { background: ${CORAL}; box-shadow: 0 0 0 3px rgba(251, 113, 133, 0.16); }
.di-ring[data-state="pulse"] { background: #818cf8; box-shadow: 0 0 0 3px rgba(129, 140, 248, 0.18);
  animation: di-pulse 1.4s ease-in-out infinite; }
@keyframes di-spin { to { transform: rotate(360deg); } }
@keyframes di-pulse { 50% { opacity: .45; } }
.di-phase { flex: 1 1 auto; min-width: 0; font-size: 12px; line-height: 1; color: #e2e8f0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.di-time { flex: none; font-size: 11px; line-height: 1; color: #94a3b8;
  font-family: 'Cascadia Mono', Consolas, ui-monospace, monospace; font-variant-numeric: tabular-nums; }
.di-badge { flex: none; font-size: 10px; line-height: 1; padding: 3px 6px; border-radius: 9999px;
  background: rgba(255, 255, 255, 0.14); color: #e2e8f0; }
.di-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.di-title { flex: 1 1 auto; min-width: 0; font-size: 12px; font-weight: 600; color: #f8fafc;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.di-tools { flex: none; display: flex; align-items: center; gap: 2px; }
.di-tool { width: 24px; height: 24px; display: grid; place-items: center; padding: 0; border: 0;
  border-radius: 7px; background: transparent; color: #94a3b8; cursor: pointer;
  transition: background-color .16s ease, color .16s ease; }
.di-tool:hover { background: rgba(255, 255, 255, 0.1); color: #f8fafc; }
.di-tool:focus-visible { outline: 2px solid #818cf8; outline-offset: 1px; }
.di-tool[aria-pressed="true"] { color: #c7d2fe; }
.di-tool { position: relative; }
.di-tool[data-guide="1"]::after { content: ''; position: absolute; top: 2px; right: 2px;
  width: 6px; height: 6px; border-radius: 9999px; background: ${CORAL};
  box-shadow: 0 0 0 2px #141418; }
.di-hint { margin: -2px 0 8px; padding: 5px 8px; width: 100%; border: 1px dashed rgba(251, 113, 133, 0.5);
  border-radius: 9px; background: rgba(251, 113, 133, 0.12); color: #fecdd3;
  font-size: 11px; font-weight: 600; cursor: pointer; text-align: left; }
.di-hint:hover { background: rgba(251, 113, 133, 0.2); }
.di-hint:focus-visible { outline: 2px solid #fb7185; outline-offset: 1px; }
.di-tool svg { width: 15px; height: 15px; }
.di-list { display: grid; gap: 8px; }
.di-task { border-radius: 14px; background: rgba(255, 255, 255, 0.045); padding: 8px 10px; }
.di-task-top { display: flex; align-items: center; gap: 8px; }
.di-task-title { flex: 1 1 auto; min-width: 0; font-size: 12px; font-weight: 600;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.di-task-phase { margin-top: 3px; font-size: 11px; line-height: 1.35; color: #cbd5e1;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.di-bar { position: relative; height: 3px; margin-top: 7px; border-radius: 9999px;
  background: rgba(255, 255, 255, 0.12); overflow: hidden; }
.di-bar > span { position: absolute; inset: 0 auto 0 0; width: 0%; border-radius: 9999px;
  background: #818cf8; transition: width .3s ease; }
.di-bar[data-indeterminate="1"] > span { width: 38%; animation: di-slide 1.25s ease-in-out infinite; }
.di-task[data-state="success"] .di-bar > span { width: 100%; background: ${GREEN}; }
.di-task[data-state="fail"] .di-bar > span { width: 100%; background: ${CORAL}; }
@keyframes di-slide { 0% { left: -38%; } 100% { left: 100%; } }
.di-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.di-action { border: 0; border-radius: 8px; padding: 4px 9px; font-size: 11px; font-weight: 600;
  background: rgba(129, 140, 248, 0.2); color: #e0e7ff; cursor: pointer;
  transition: background-color .16s ease; }
.di-action:hover { background: rgba(129, 140, 248, 0.34); }
.di-action[data-tone="ghost"] { background: rgba(255, 255, 255, 0.08); color: #cbd5e1; }
.di-action[data-tone="ghost"]:hover { background: rgba(255, 255, 255, 0.16); }
.di-action:focus-visible { outline: 2px solid #818cf8; outline-offset: 1px; }
@media (prefers-reduced-motion: reduce) {
  .di-shell, .di-collapsed, .di-expanded, .di-action, .di-tool { transition: none !important; }
  .di-root.di-enter .di-shell { animation: none !important; }
  .di-ring[data-state="running"], .di-ring[data-state="pulse"], .di-bar[data-indeterminate="1"] > span { animation: none !important; }
}
`;

  /* ---------- 结构 ---------- */
  function build() {
    const style = document.createElement('style');
    style.id = 'dynamic-island-style';
    style.textContent = STYLE;
    document.head.appendChild(style);

    root = document.createElement('div');
    root.id = 'dynamic-island-root';
    root.className = 'di-root';
    root.hidden = true;
    root.innerHTML = `
      <div class="di-shell" role="status" aria-live="polite" data-island-state="idle">
        <div class="di-layer di-collapsed">
          <span class="di-ring" data-state="running" aria-hidden="true"></span>
          <span class="di-phase">准备中…</span>
          <span class="di-time">00:00</span>
          <span class="di-badge" hidden></span>
        </div>
        <div class="di-layer di-expanded">
          <div class="di-head">
            <span class="di-title">后台任务</span>
            <span class="di-tools">
              <button type="button" class="di-tool" data-tool="sound" aria-label="提示音开关"></button>
              <button type="button" class="di-tool" data-tool="notify" aria-label="系统通知开关"></button>
            </span>
          </div>
          <button type="button" class="di-hint" data-hint="notify" hidden>🔔 点此开启切屏提醒（切到微信也能收到通知）</button>
          <div class="di-list"></div>
        </div>
      </div>`;
    document.body.appendChild(root);

    el.shell = root.querySelector('.di-shell');
    el.collapsed = root.querySelector('.di-collapsed');
    el.expanded = root.querySelector('.di-expanded');
    el.phase = root.querySelector('.di-phase');
    el.time = root.querySelector('.di-time');
    el.badge = root.querySelector('.di-badge');
    el.ring = root.querySelector('.di-ring');
    el.title = root.querySelector('.di-title');
    el.list = root.querySelector('.di-list');
    el.sound = root.querySelector('[data-tool="sound"]');
    el.notify = root.querySelector('[data-tool="notify"]');
    el.hint = root.querySelector('[data-hint="notify"]');

    el.shell.addEventListener('pointerenter', () => { if (!isTouch()) { hovered = true; open(); pauseHold(); } });
    el.shell.addEventListener('pointerleave', () => {
      if (isTouch()) return;
      hovered = false;
      clearTimeout(collapseTimer);
      collapseTimer = setTimeout(() => { close(); resumeHold(); }, COLLAPSE_DELAY);
    });
    el.shell.addEventListener('click', (event) => {
      if (isTouch() && !event.target.closest('button')) toggle();
    });
    el.sound.addEventListener('click', (event) => { event.stopPropagation(); api.setSound(!soundOn); });
    el.notify.addEventListener('click', (event) => { event.stopPropagation(); api.setNotify(!notifyOn); });
    el.hint.addEventListener('click', (event) => { event.stopPropagation(); api.setNotify(true); });
    el.list.addEventListener('click', onListClick);

    renderTools();
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('focus', onVisibility);
    window.addEventListener('blur', onBlur);
  }

  const isTouch = () => {
    try { return window.matchMedia('(hover: none)').matches; } catch (error) { return false; }
  };

  function notifyGuide() {
    if (typeof Notification === 'undefined') return false;
    return !notifyOn && Notification.permission === 'default';
  }

  function renderTools() {
    if (!el.sound || !el.notify) return;
    el.sound.innerHTML = icon(soundOn ? 'sound' : 'mute');
    el.sound.setAttribute('aria-pressed', String(soundOn));
    el.sound.title = soundOn ? '提示音已开（点击可试听）' : '提示音已关';
    el.notify.innerHTML = icon(notifyOn ? 'bell' : 'bellOff');
    el.notify.setAttribute('aria-pressed', String(notifyOn));
    const guide = notifyGuide();
    if (guide) {
      el.notify.dataset.guide = '1';
      el.notify.title = '点此开启切屏提醒';
      el.notify.setAttribute('aria-label', '系统通知开关（点此开启切屏提醒）');
    } else {
      delete el.notify.dataset.guide;
      el.notify.title = notifyOn ? '系统通知已开' : '系统通知已关';
      el.notify.setAttribute('aria-label', '系统通知开关');
    }
    if (el.hint) el.hint.hidden = !guide;
  }

  /* ---------- 尺寸与展开 ---------- */
  function measureCollapsed() {
    const width = Math.max(200, Math.min(270, Math.ceil(el.collapsed.scrollWidth) || 200));
    return { width, height: 38 };
  }

  function measureExpanded() {
    const width = Math.min(360, Math.max(240, window.innerWidth - 24));
    const previous = el.expanded.style.width;
    el.expanded.style.width = `${width}px`;
    const height = Math.max(38, Math.ceil(el.expanded.scrollHeight));
    el.expanded.style.width = previous;
    return { width, height };
  }

  function applySize() {
    if (!root || root.hidden) return;
    const size = expanded ? measureExpanded() : measureCollapsed();
    el.shell.style.width = `${size.width}px`;
    el.shell.style.height = `${size.height}px`;
    el.shell.style.borderRadius = expanded ? '24px' : '9999px';
  }

  function open() {
    if (!root || root.hidden || expanded) return;
    expanded = true;
    root.classList.add('di-open');
    applySize();
  }

  function close() {
    if (!root || !expanded) return;
    expanded = false;
    root.classList.remove('di-open');
    applySize();
  }

  function toggle() {
    if (expanded) { hovered = false; close(); } else { hovered = true; open(); }
  }

  /* ---------- 渲染 ---------- */
  function statusOf(task) {
    if (task.state === 'success') return 'success';
    if (task.state === 'fail') return 'fail';
    return 'running';
  }

  function primary() {
    const all = ordered();
    return all.find((task) => task.state === 'running') || all[all.length - 1];
  }

  function render() {
    if (!root) return;
    if (!active()) {
      root.hidden = true;
      el.shell.setAttribute('data-island-state', 'idle');
      el.shell.removeAttribute('data-island-count');
      el.list.replaceChildren();
      stopTick();
      return;
    }
    const wasHidden = root.hidden;
    root.hidden = false;
    const list = ordered();
    const head = primary();
    const state = statusOf(head);
    el.shell.setAttribute('data-island-state', state);
    el.shell.setAttribute('data-island-count', String(list.length));

    el.phase.textContent = head.state === 'success' ? `${clip(head.title, 12)}已完成`
      : head.state === 'fail' ? `${clip(head.title, 12)}失败` : (head.phase || head.title);
    el.ring.dataset.state = state === 'running' ? 'pulse' : state;
    el.time.textContent = mmss((head.finishedAt || Date.now()) - head.startedAt);
    const extra = list.length - 1;
    el.badge.hidden = extra <= 0;
    el.badge.textContent = `+${extra}`;
    el.title.textContent = list.length > 1 ? `${list.length} 个后台任务` : (head.title || '后台任务');

    el.list.replaceChildren(...list.slice(0, MAX_VISIBLE).map(renderTask));
    if (wasHidden) {
      root.classList.remove('di-enter');
      void root.offsetWidth;
      root.classList.add('di-enter');
      setTimeout(() => root.classList.remove('di-enter'), 360);
    }
    applySize();
    startTick();
  }

  function renderTask(task) {
    const node = document.createElement('div');
    node.className = 'di-task';
    node.dataset.taskId = task.id;
    node.dataset.state = statusOf(task);
    const progress = task.state === 'running' && Number.isFinite(task.progress) && task.progress >= 0
      ? `${Math.round(task.progress)}%` : '';
    node.innerHTML = `
      <div class="di-task-top">
        <span class="di-ring" data-state="${statusOf(task)}" aria-hidden="true"></span>
        <span class="di-task-title"></span>
        <span class="di-time">${task.state === 'running' ? progress : icon(statusOf(task) === 'success' ? 'check' : 'alert')}</span>
        <button type="button" class="di-tool" data-act="dismiss" aria-label="关闭该任务">${icon('close')}</button>
      </div>
      <p class="di-task-phase"></p>
      <div class="di-bar" data-indeterminate="${task.state === 'running' && !Number.isFinite(task.progress) ? '1' : '0'}"><span style="width:${progress || ''}"></span></div>
      ${task.actions && task.actions.length ? '<div class="di-actions"></div>' : ''}`;
    node.querySelector('.di-task-title').textContent = task.title || '任务';
    node.querySelector('.di-task-phase').textContent = task.state === 'success' ? (task.detail || '已完成')
      : task.state === 'fail' ? (task.detail || '执行失败') : (task.phase || '处理中…');
    const timeNode = node.querySelector('.di-time');
    if (task.state === 'running') timeNode.textContent = mmss(Date.now() - task.startedAt);
    else timeNode.innerHTML = icon(statusOf(task) === 'success' ? 'check' : 'alert');
    if (task.actions && task.actions.length) {
      const row = node.querySelector('.di-actions');
      for (const [index, action] of task.actions.entries()) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'di-action';
        button.dataset.act = 'action';
        button.dataset.index = String(index);
        if (action.tone === 'ghost') button.dataset.tone = 'ghost';
        button.textContent = action.label;
        row.appendChild(button);
      }
    }
    return node;
  }

  function onListClick(event) {
    const button = event.target.closest('[data-act]');
    if (!button) return;
    const taskNode = button.closest('[data-task-id]');
    const task = taskNode && tasks.get(taskNode.dataset.taskId);
    if (!task) return;
    event.stopPropagation();
    if (button.dataset.act === 'dismiss') { api.dismiss(task.id); return; }
    const action = task.actions && task.actions[Number(button.dataset.index)];
    if (!action) return;
    try { action.onClick && action.onClick(); } catch (error) { /* 动作失败不影响 UI */ }
    api.dismiss(task.id);
  }

  function startTick() {
    if (tickTimer) return;
    tickTimer = setInterval(() => {
      if (!active()) { stopTick(); return; }
      const head = primary();
      el.time.textContent = mmss((head.finishedAt || Date.now()) - head.startedAt);
      for (const node of el.list.querySelectorAll('[data-task-id]')) {
        const task = tasks.get(node.dataset.taskId);
        if (!task || task.state !== 'running') continue;
        const timeNode = node.querySelector('.di-time');
        if (timeNode) timeNode.textContent = mmss(Date.now() - task.startedAt);
      }
    }, 1000);
  }

  function stopTick() {
    clearInterval(tickTimer);
    tickTimer = 0;
  }

  /* ---------- 退场计时 ---------- */
  function settled() {
    const list = ordered();
    return list.length > 0 && list.every((task) => task.state !== 'running');
  }

  function pauseHold() {
    if (!holdTimer) return;
    holdRemaining = Math.max(0, holdDeadline - Date.now());
    clearTimeout(holdTimer);
    holdTimer = 0;
  }

  function resumeHold() {
    if (!settled() || hovered) return;
    if (ordered().some((task) => task.state === 'fail')) return;
    const wait = holdRemaining || SUCCESS_HOLD;
    holdRemaining = 0;
    clearTimeout(holdTimer);
    holdDeadline = Date.now() + wait;
    holdTimer = setTimeout(() => {
      holdTimer = 0;
      if (hovered) return;
      for (const task of ordered()) if (task.state === 'success') tasks.delete(task.id);
      render();
    }, wait);
  }

  /* ---------- 提醒层 ---------- */
  function restoreAttention() {
    attentionToken += 1;
    if (savedTitle !== null) {
      document.title = savedTitle;
      savedTitle = null;
    }
    if (savedIcon) {
      if (savedIcon.created) savedIcon.link.remove();
      else if (savedIcon.link.isConnected) savedIcon.link.setAttribute('href', savedIcon.href);
      savedIcon = null;
    }
  }

  async function paintFavicon(ok, token) {
    try {
      const size = 64;
      const canvas = document.createElement('canvas');
      canvas.width = size;
      canvas.height = size;
      const ctx = canvas.getContext('2d');
      if (!ctx) return null;
      const link = savedIcon ? savedIcon.link : null;
      let painted = false;
      if (link) {
        const href = link.getAttribute('href') || '';
        if (href) {
          try {
            const image = new Image();
            image.src = href;
            await image.decode();
            ctx.drawImage(image, 0, 0, size, size);
            painted = true;
          } catch (error) { /* 画不出来就退回纯色底图 */ }
        }
      }
      if (!painted) {
        ctx.fillStyle = '#111114';
        const radius = 14;
        ctx.beginPath();
        ctx.moveTo(radius, 0);
        ctx.arcTo(size, 0, size, size, radius);
        ctx.arcTo(size, size, 0, size, radius);
        ctx.arcTo(0, size, 0, 0, radius);
        ctx.arcTo(0, 0, size, 0, radius);
        ctx.closePath();
        ctx.fill();
      }
      if (token !== attentionToken) return null;
      ctx.beginPath();
      ctx.arc(size - 14, size - 14, 11, 0, Math.PI * 2);
      ctx.fillStyle = ok ? GREEN : CORAL;
      ctx.fill();
      ctx.lineWidth = 4;
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.9)';
      ctx.stroke();
      return canvas.toDataURL('image/png');
    } catch (error) {
      return null;
    }
  }

  async function applyAttention(ok, label) {
    const token = ++attentionToken;
    if (savedTitle === null) savedTitle = document.title;
    const prefix = ok ? '✅' : '⚠️';
    document.title = `${prefix} ${clip(label, 14)}${ok ? '已完成' : '失败'} - ${savedTitle}`;
    if (!savedIcon) {
      const existing = document.querySelector('link[rel="icon"], link[rel="shortcut icon"]');
      if (existing) {
        savedIcon = { link: existing, href: existing.getAttribute('href') || '', created: false };
      } else {
        const link = document.createElement('link');
        link.rel = 'icon';
        document.head.appendChild(link);
        savedIcon = { link, href: '', created: true };
      }
    }
    const data = await paintFavicon(ok, token);
    if (!data || token !== attentionToken || !savedIcon) return;
    savedIcon.link.setAttribute('href', data);
    savedIcon.link.setAttribute('type', 'image/png');
  }

  function chime(ok) {
    if (!soundOn) return;
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      audio = audio || new Ctx();
      if (audio.state === 'suspended') audio.resume().catch(() => {});
      const now = audio.currentTime;
      const notes = ok ? [880, 1318.5] : [392, 294];
      notes.forEach((freq, index) => {
        const osc = audio.createOscillator();
        const gain = audio.createGain();
        osc.type = ok ? 'sine' : 'triangle';
        osc.frequency.value = freq;
        const at = now + index * 0.12;
        gain.gain.setValueAtTime(0.0001, at);
        gain.gain.exponentialRampToValueAtTime(0.28, at + 0.012);
        gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.19);
        osc.connect(gain).connect(audio.destination);
        osc.start(at);
        osc.stop(at + 0.22);
      });
    } catch (error) { /* 音频不可用则静默 */ }
  }

  function systemNotify(ok, label, detail) {
    if (!notifyOn || typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
    try {
      const notification = new Notification(ok ? `${label}已完成` : `${label}失败`, {
        body: detail || (ok ? '点击回到工作台查看结果' : '请回到工作台处理'),
        tag: 'poster-island',
        silent: true,
      });
      notification.onclick = () => {
        try { window.focus(); } catch (error) { /* 忽略 */ }
        notification.close();
      };
    } catch (error) { /* 通知失败不影响主流程 */ }
  }

  function backgroundFeedback(task, ok) {
    if (foreground()) return;
    applyAttention(ok, task.title || '任务');
    chime(ok);
    systemNotify(ok, task.title || '任务', task.detail);
  }

  /** 真正“视线离开”：页面隐藏或窗口失去焦点（Windows 下切到微信不算 hidden）。 */
  function foreground() {
    if (typeof document === 'undefined') return true;
    if (document.hidden) return false;
    if (typeof document.hasFocus === 'function' && !document.hasFocus()) return false;
    return true;
  }

  function onVisibility() {
    if (!foreground()) return;
    restoreAttention();
  }

  function onBlur() {
    // 窗口失焦后浏览器不会触发 visibilitychange，靠 focus/blur 兜底判断
    try { document.hasFocus(); } catch (error) { /* 忽略 */ }
  }

  /* ---------- 全局 API ---------- */
  function ensure(id) {
    return tasks.get(String(id)) || null;
  }

  const api = {
    start(id, options) {
      if (!root && document.body) build();
      if (!root || !id) return;
      if (notifyGuide()) renderTools();
      const key = String(id);
      const existing = tasks.get(key);
      if (existing) { api.update(key, options || {}); return; }
      tasks.set(key, {
        id: key,
        title: (options && options.title) || '后台任务',
        phase: (options && options.phase) || '准备中…',
        detail: (options && options.detail) || '',
        progress: options && Number.isFinite(options.progress) ? options.progress : undefined,
        kind: (options && options.kind) || 'generic',
        actions: (options && options.actions) || [],
        state: 'running',
        startedAt: Date.now(),
        finishedAt: 0,
      });
      holdRemaining = 0;
      render();
    },
    update(id, patch) {
      const task = ensure(id);
      if (!task || !root) return;
      const next = patch || {};
      if (typeof next.title === 'string') task.title = next.title;
      if (typeof next.phase === 'string') task.phase = next.phase;
      if (typeof next.detail === 'string') task.detail = next.detail;
      if (Array.isArray(next.actions)) task.actions = next.actions;
      if (next.progress === null) task.progress = undefined;
      else if (Number.isFinite(next.progress)) task.progress = next.progress;
      if (task.state === 'running') task.finishedAt = 0;
      render();
    },
    succeed(id, options) {
      const task = ensure(id);
      if (!task || !root) return;
      const next = options || {};
      task.state = 'success';
      task.finishedAt = Date.now();
      if (typeof next.detail === 'string') task.detail = next.detail;
      if (next.title) task.title = next.title;
      if (Array.isArray(next.actions)) task.actions = next.actions;
      task.progress = 100;
      render();
      backgroundFeedback(task, true);
      resumeHold();
    },
    fail(id, message) {
      const task = ensure(id);
      if (!task || !root) return;
      task.state = 'fail';
      task.finishedAt = Date.now();
      task.detail = typeof message === 'string' && message ? message : '执行失败';
      task.actions = [{ label: '关闭', tone: 'ghost', onClick: () => {} }];
      render();
      backgroundFeedback(task, false);
    },
    dismiss(id) {
      if (!tasks.delete(String(id))) return;
      if (!active()) {
        clearTimeout(holdTimer);
        holdTimer = 0;
        holdRemaining = 0;
      }
      if (!active() && foreground()) restoreAttention();
      render();
    },
    primeAudio() {
      if (!soundOn) return;
      try {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return;
        audio = audio || new Ctx();
        if (audio.state === 'suspended') audio.resume().catch(() => {});
      } catch (error) { /* 静默 */ }
    },
    setSound(on) {
      soundOn = Boolean(on);
      writeFlag(SOUND_KEY, soundOn);
      if (soundOn) {
        this.primeAudio();
        chime(true);  // 点开开关即试听，确认音量与音色
      }
      renderTools();
      return soundOn;
    },
    setNotify(on) {
      const wanted = Boolean(on);
      if (!wanted) {
        notifyOn = false;
        writeFlag(NOTIFY_KEY, false);
        renderTools();
        return false;
      }
      if (typeof Notification === 'undefined') { renderTools(); return false; }
      const finish = (granted) => {
        notifyOn = Boolean(granted);
        writeFlag(NOTIFY_KEY, notifyOn);
        renderTools();
      };
      if (Notification.permission === 'granted') { finish(true); return true; }
      if (Notification.permission === 'denied') { finish(false); return false; }
      try {
        const result = Notification.requestPermission((value) => finish(value === 'granted'));
        if (result && typeof result.then === 'function') result.then((value) => finish(value === 'granted')).catch(() => finish(false));
      } catch (error) {
        finish(false);
      }
      return false;
    },
    get sound() { return soundOn; },
    get notify() { return notifyOn; },
  };

  window.DynamicIsland = api;

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', build, { once: true });
  else build();
})();
