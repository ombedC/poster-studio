function posterAiSvg() {
  const api = '/api/ai-svg';
  const models = ['gpt-image-2.6-sunburst', 'gpt-image-2.6-flare'];
  const presets = {
    flat: ['扁平矢量海报', '使用清晰轮廓、扁平色块和独立分层。少用渐变，避免写实摄影质感。'],
    blank: ['无字留白底图', '在画面顶部和中部留出干净的排版空间。禁止生成任何文字、字符或水印。'],
    sticker: ['透明底图标/贴纸', '在透明背景上居中绘制独立元素，边缘干净且四周留白。'],
  };
  const request = async (path, options = {}) => {
    const response = await fetch(api + path, { ...options, cache: 'no-store' });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = body.message || (typeof body.detail === 'string' ? body.detail : null);
      throw Object.assign(new Error(message || `请求失败（HTTP ${response.status}）`), { body, status: response.status });
    }
    return body;
  };
  const json = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const asset = (url) => typeof url === 'string' && /^\/api\/ai-svg\/history\/[\w-]+\/(?:preview|download)$/.test(url) ? url : '';
  const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));
  const pending = (job) => ['queued', 'processing'].includes(job?.status);
  let historyPoll;
  return {
    models, presets, prompt: '', promptOverride: null, preset: 'flat',
    model: '', size: '1024x1536', quality: 'high', complexity: 'high', background: 'opaque',
    checker: true, settingsOpen: false, reveal: false, configured: false, saving: false, loadingConfig: false,
    config: { base_url: '', api_key: '', model: models[0], svg_model: 'gpt-5.4-mini', compatibility_mode: false, provider: 'apimart' },
    configError: '', generating: false, importing: false, elapsed: 0, status: '', error: '', debug: null,
    current: null, history: [],
    asset,
    get promptFull() {
      return this.promptOverride ?? [this.prompt.trim(), '输出原生 SVG，保留独立图形和可编辑文本节点。', presets[this.preset][1]].filter(Boolean).join('\n\n');
    },
    choosePreset(preset) {
      this.preset = preset;
      this.promptOverride = null;
      this.background = preset === 'sticker' ? 'transparent' : 'opaque';
    },
    async loadConfig() {
      this.loadingConfig = true;
      try {
        const { base_url, model, svg_model, compatibility_mode, provider, has_api_key } = await request('/config');
        this.configured = Boolean(has_api_key);
        this.config = { base_url, model, svg_model, compatibility_mode, provider, api_key: '' };
      } finally { this.loadingConfig = false; }
    },
    async refresh() {
      await Promise.allSettled([this.loadConfig(), this.refreshHistory()]);
    },
    async refreshHistory() {
      clearTimeout(historyPoll);
      try {
        const body = await request('/history');
        this.history = body.items || [];
        const selected = this.history.find(item => item.id === this.current?.id);
        const pendingItem = this.history.find(pending);
        if (!this.generating) {
          if (selected) this.select(selected);
          else if (!this.current && pendingItem) this.select(pendingItem);
        }
      } catch (error) { this.error = error.message; }
      finally {
        clearTimeout(historyPoll);
        if (this.history.some(pending)) historyPoll = setTimeout(() => this.refreshHistory(), 3000);
      }
    },
    jobLabel(status) {
      return { queued: '排队中', processing: '生成中', completed: '已完成', failed: '生成失败' }[status] || 'SVG';
    },
    async openSettings() {
      this.configError = '';
      this.reveal = false;
      this.settingsOpen = true;
      try { await this.loadConfig(); }
      catch (error) { this.configError = error.message; }
    },
    closeSettings() {
      if (this.saving) return;
      this.config.api_key = '';
      this.reveal = false;
      this.settingsOpen = false;
    },
    async saveSettings() {
      this.saving = true;
      this.configError = '';
      try {
        const { base_url, api_key, model, svg_model, compatibility_mode, provider } = this.config;
        const payload = { base_url, model, svg_model, compatibility_mode, provider };
        if (api_key.trim()) payload.api_key = api_key.trim();
        await request('/config', json('PUT', payload));
        await this.loadConfig();
        this.saving = false;
        this.closeSettings();
      } catch (error) { this.configError = error.message; }
      finally { this.saving = false; }
    },
    select(item) {
      this.current = item;
      this.debug = { ...item.debug, request: item.request, usage: item.usage, error: item.error };
      this.status = this.jobLabel(item.status);
      this.error = item.error?.message || '';
    },
    async generate() {
      if (this.generating || this.importing || !this.prompt.trim() || !this.promptFull.trim()) return;
      this.generating = true;
      this.elapsed = 0;
      this.status = '正在提交生成请求';
      this.error = '';
      const island = window.DynamicIsland;
      island?.primeAudio();
      island?.start('ai-generate', { title: 'AI 生图', phase: '正在提交生成请求…', kind: 'ai' });
      const studio = () => window.Alpine?.$data(document.body);
      const start = Date.now();
      const timer = setInterval(() => { this.elapsed = Math.floor((Date.now() - start) / 1000); }, 1000);
      try {
        const { size, quality, complexity, background } = this;
        const payload = { prompt: this.promptFull.trim(), model: this.model.trim() || this.config.model, size, quality, background, complexity };
        this.debug = { request: payload };
        let body = await request('/generate', { ...json('POST', payload), signal: AbortSignal.timeout(185000) });
        const task = body.id;
        while (task && !['completed', 'failed', 'cancelled'].includes(body.status)) {
          if (Date.now() - start >= 23 * 60 * 1000) throw new Error('生成等待超时，可在历史记录中刷新结果。');
          this.status = Number.isFinite(body.progress) ? `生成中 · ${body.progress}%` : '等待中转站返回 SVG';
          island?.update('ai-generate', {
            phase: this.status,
            progress: Number.isFinite(body.progress) ? body.progress : undefined,
          });
          await wait(3000);
          body = await request('/jobs/' + encodeURIComponent(task), { signal: AbortSignal.timeout(185000) });
        }
        if (['failed', 'cancelled'].includes(body.status)) throw Object.assign(new Error(body.error?.message || 'SVG 生成失败'), { body });
        const item = body.item || body;
        if (!asset(item.preview_url)) throw new Error('响应中没有可预览的 SVG');
        const debug = { request: payload, ...body.debug, ...item.debug, usage: body.usage ?? item.usage };
        this.select(item);
        this.debug = debug;
        this.history = [item, ...this.history.filter(other => other.id !== item.id)];
        this.status = 'SVG 已生成';
        island?.succeed('ai-generate', {
          detail: 'SVG 已生成，可直接载入编辑器',
          actions: [
            { label: '查看结果', tone: 'ghost', onClick: () => { try { studio()?.switchMode('ai'); } catch (error) { /* 忽略 */ } } },
            { label: '一键载入编辑器', onClick: () => { try { studio()?.importAiSvg(); } catch (error) { /* 忽略 */ } } },
          ],
        });
      } catch (error) {
        this.debug = { ...this.debug, http_status: error.body?.error?.status_code ?? error.body?.upstream_status ?? error.status, error: error.body?.error?.summary ?? error.body?.detail ?? error.body?.error ?? error.message };
        this.error = error.name === 'TimeoutError' ? '等待响应超时，请先查看历史或任务状态。' : error.message;
        island?.fail('ai-generate', this.error);
      } finally {
        clearInterval(timer);
        this.elapsed = Math.floor((Date.now() - start) / 1000);
        this.generating = false;
      }
    },
    async importCurrent() {
      if (!this.current?.id || this.importing || this.generating) return null;
      this.importing = true;
      this.error = '';
      try { return await request('/history/' + encodeURIComponent(this.current.id) + '/import', { method: 'POST' }); }
      catch (error) { this.error = error.message; return null; }
      finally { this.importing = false; }
    },
    formatBytes(size) {
      if (!Number.isFinite(size)) return '—';
      return size >= 1024 ? `${(size / 1024).toFixed(1)} KB` : `${size} B`;
    },
  };
}
