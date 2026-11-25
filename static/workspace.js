/* 轻量身份与团队：localStorage + X-User-Id / X-Team-Id 请求头。
 * 口令只允许 ASCII（HTTP 头限制），中文昵称存服务端 users.name。
 */
(function () {
  const USER_KEY = 'poster.ws.user';
  const TEAM_KEY = 'poster.ws.team';
  const NAME_KEY = 'poster.ws.name';
  const USER_RE = /^[A-Za-z0-9_-]{4,40}$/;

  function randomId() {
    const alphabet = 'abcdefghijkmnpqrstuvwxyz23456789';
    let out = '';
    for (let i = 0; i < 6; i += 1) out += alphabet[Math.floor(Math.random() * alphabet.length)];
    return 'u_' + out;
  }

  function read(key, fallback = '') {
    try { return localStorage.getItem(key) || fallback; } catch (error) { return fallback; }
  }

  function write(key, value) {
    try { localStorage.setItem(key, value); } catch (error) { /* 隐私模式忽略 */ }
  }

  function validUserId(value) {
    return USER_RE.test(String(value || '').trim());
  }

  // ?ws= 恢复链接优先：先覆盖本地口令，再用服务端返回的团队做权威同步
  let fromLink = false;
  try {
    const params = new URLSearchParams(location.search);
    const token = (params.get('ws') || '').trim();
    if (token && validUserId(token)) {
      write(USER_KEY, token);
      fromLink = true;
      params.delete('ws');
      const query = params.toString();
      history.replaceState(null, '', location.pathname + (query ? '?' + query : '') + location.hash);
    }
  } catch (error) { /* 忽略 */ }

  let user = read(USER_KEY);
  if (!validUserId(user)) {
    user = randomId();
    write(USER_KEY, user);
  }
  let team = read(TEAM_KEY);
  let name = read(NAME_KEY);

  const originalFetch = window.fetch.bind(window);
  window.fetch = function patchedFetch(input, init = {}) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const sameOrigin = !/^https?:\/\//i.test(url) || url.startsWith(location.origin);
    if (sameOrigin && url.includes('/api/')) {
      const headers = new Headers(init.headers || (input && input.headers) || {});
      if (!headers.has('X-User-Id')) headers.set('X-User-Id', user);
      if (team && !headers.has('X-Team-Id')) headers.set('X-Team-Id', team);
      init = { ...init, headers };
    }
    return originalFetch(input, init);
  };

  function apply(payload) {
    if (!payload) return payload;
    if (payload.user_id) { user = payload.user_id; write(USER_KEY, user); }
    if (typeof payload.team_id === 'string') { team = payload.team_id; write(TEAM_KEY, team); }
    if (typeof payload.name === 'string') { name = payload.name; write(NAME_KEY, name); }
    return payload;
  }

  async function refresh() {
    const response = await window.fetch('/api/workspace/me', { cache: 'no-store' });
    if (!response.ok) throw new Error('工作区加载失败');
    return apply(await response.json());
  }

  const ready = refresh().catch(() => null);

  window.Workspace = {
    ready,
    fromLink: () => fromLink,
    user: () => user,
    team: () => team,
    name: () => name,
    validUserId,
    apply,
    refresh,
    setUser(next) {
      const cleaned = String(next || '').trim();
      if (!validUserId(cleaned)) throw new Error('口令只能包含字母、数字、下划线、短横线（4-40 位）');
      user = cleaned;
      write(USER_KEY, cleaned);
      team = '';
      write(TEAM_KEY, '');
      return user;
    },
    setTeam(next) {
      team = String(next || '').trim();
      write(TEAM_KEY, team);
      return team;
    },
    recoveryLink() {
      return `${location.origin}${location.pathname}?ws=${encodeURIComponent(user)}`;
    },
  };
})();
