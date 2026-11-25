/* Seedream 5.5 生图 / 圈画局改 / 拆图层工作台。
 * 作为 studio() 的方法集合展开，状态集中在 sd 对象里，避免污染编辑器状态。
 */
function posterSeedream() {
  const FILE_PREFIX = '/api/seedream';
  let boundKeys = false;
  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

  return {
    sd: {
      ready: false,
      enabled: false,
      model: '',
      maxRefs: 10,
      prompt: '',
      editPrompt: '',
      size: '2K',
      background: 'opaque',
      refs: [],
      versions: [],
      current: null,
      projectId: null,
      busy: '',
      drawMode: false,
      tool: 'rect',
      popover: { open: false, left: 0, top: 0, width: 300, text: '' },
      compare: false,
      maxLayers: 20,
      error: '',
      catalog: { channels: [], intents: [], styles: [], prompts_dir: '', warnings: [] },
      llmConfigured: false,
      channelId: '',
      intentId: 'direct',
      styleId: '',
      sizeMode: 'preset',
      customSize: '1242x2208',
      stylesBusy: false,
      optimizing: false,
      promptSnapshot: null,
      usedFiles: [],
      promptWarnings: [],
      lastIntent: '',
      catalogError: '',
      hasOptimized: false,
      arkKey: '',
      arkStatus: '',
      arkSaving: false,
      arkModel: '',
      arkTesting: false,
      history: { open: false, view: 'mine', q: '', items: [], loading: false, loaded: false, error: '' },
    },

    async sdLoadConfig() {
      this.sd.catalogError = '';
      try {
        const response = await fetch(`${FILE_PREFIX}/config`, { cache: 'no-store' });
        if (!response.ok) {
          this.sd.catalogError = response.status === 404
            ? '后端还没有 Seedream 接口（HTTP 404）：请重启后端服务，然后点「重试」。'
            : `读取生图配置失败（HTTP ${response.status}）：请重启后端服务后重试。`;
          this.sd.enabled = false;
          return false;
        }
        const body = await response.json();
        this.sd.enabled = Boolean(body.enabled);
        this.sd.model = body.model || '';
        this.sd.maxRefs = body.max_references || 10;
        this.sdApplyCatalog(body);
        this.features = { ...(this.features || {}), ai_svg: Boolean(body.enable_ai_svg), seedream: Boolean(body.enabled) };
        return true;
      } catch (error) {
        this.sd.catalogError = `无法连接后端：${error.message}`;
        this.sd.enabled = false;
        return false;
      } finally {
        this.sd.ready = true;
      }
    },

    async sdInit() {
      await this.sdLoadConfig();
      if (boundKeys) return;
      boundKeys = true;
      window.addEventListener('keydown', (event) => this.sdHandleKey(event));
    },

    sdApplyCatalog(body) {
      this.sd.catalog = {
        channels: body.channels || [],
        intents: body.intents || [],
        styles: body.styles || [],
        prompts_dir: body.prompts_dir || '',
        warnings: body.warnings || [],
      };
      this.sd.llmConfigured = Boolean(body.llm_configured);
      // 选中项失效时回落到默认，避免刷新风格库后出现空选择
      if (this.sd.styleId && !this.sd.catalog.styles.some((item) => item.id === this.sd.styleId)) this.sd.styleId = '';
      if (!this.sd.catalog.intents.some((item) => item.id === this.sd.intentId)) this.sd.intentId = 'direct';
      if (this.sd.channelId && !this.sd.catalog.channels.some((item) => item.id === this.sd.channelId)) {
        this.sd.channelId = '';
        this.sd.sizeMode = 'preset';
      }
    },

    async sdRefreshCatalog() {
      if (this.sd.stylesBusy) return;
      this.sd.stylesBusy = true;
      try {
        if (await this.sdLoadConfig()) {
          this.notify(`已刷新：${this.sd.catalog.channels.length} 个渠道 · ${this.sd.catalog.styles.length} 套风格`);
        } else {
          this.notify(this.sd.catalogError || '刷新失败', 'error');
        }
      } finally {
        this.sd.stylesBusy = false;
      }
    },

    sdChooseChannel(channelId) {
      const channel = this.sd.catalog.channels.find((item) => item.id === channelId);
      if (!channel) return;
      this.sd.channelId = channel.id;
      this.sd.sizeMode = 'channel';
      this.sd.size = channel.size;
    },

    sdChooseCustomSize() {
      this.sd.channelId = '';
      this.sd.sizeMode = 'preset';
      if (/^\d{2,5}x\d{2,5}$/i.test(this.sd.size) && this.sd.size !== this.sd.customSize) this.sd.size = '2K';
      if (this.sd.sizeMode === 'preset' && this.sd.size === 'custom') this.sd.size = '2K';
    },

    sdSetSize(value) {
      if (value === 'custom') {
        this.sd.sizeMode = 'pixel';
        this.sd.size = this.sd.customSize;
        return;
      }
      this.sd.sizeMode = 'preset';
      this.sd.size = value;
    },

    sdSetCustomSize(value) {
      this.sd.customSize = value.trim();
      if (/^\d{2,5}x\d{2,5}$/i.test(this.sd.customSize)) this.sd.size = this.sd.customSize;
    },

    sdChooseIntent(intentId) {
      this.sd.intentId = intentId;
    },

    sdChooseStyle(styleId) {
      this.sd.styleId = this.sd.styleId === styleId ? '' : styleId;
    },

    sdChannelHint() {
      const channel = this.sd.catalog.channels.find((item) => item.id === this.sd.channelId);
      if (!channel) return `自由尺寸 · ${this.sd.size}`;
      return `${channel.ratio} · ${channel.size}`;
    },

    sdAppliedText() {
      const channel = this.sd.catalog.channels.find((item) => item.id === this.sd.channelId);
      const intent = this.sd.catalog.intents.find((item) => item.id === this.sd.intentId);
      const style = this.sd.catalog.styles.find((item) => item.id === this.sd.styleId);
      const parts = [channel ? channel.name : '自定义尺寸', intent ? intent.name : '', style ? style.name : ''];
      return parts.filter(Boolean).join(' + ');
    },

    sdOnPromptInput() {
      // 用户手动改过文本：原话快照失效，下次帮写以新内容为原话
      this.sd.promptSnapshot = null;
      this.sd.hasOptimized = false;
      this.sd.usedFiles = [];
      this.sd.promptWarnings = [];
    },

    async sdOptimizePrompt() {
      if (this.sd.optimizing) return;
      const raw = (this.sd.promptSnapshot ?? this.sd.prompt).trim();
      if (!raw) { this.notify('先写下你的需求或粘贴一段通知', 'error'); return; }
      if (!this.sd.llmConfigured) { this.notify('未配置大模型：请点右上角 API 设置或写入 .env', 'error'); return; }
      this.sd.optimizing = true;
      try {
        const response = await fetch(`${FILE_PREFIX}/optimize-prompt`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            raw_input: raw,
            channel_id: this.sd.channelId || 'wechat_banner',
            intent_id: this.sd.intentId,
            style_id: this.sd.styleId || null,
            has_ref_images: this.sd.refs.length > 0,
          }),
        });
        if (!response.ok) throw new Error(await this.sdFailure(response, '扩写失败'));
        const body = await response.json().catch(() => ({}));
        this.sd.promptSnapshot = raw;
        this.sd.hasOptimized = true;
        this.sd.prompt = body.prompt || '';
        this.sd.usedFiles = body.used_files || [];
        this.sd.promptWarnings = body.warnings || [];
        if (body.channel && body.channel.size) {
          this.sd.channelId = body.channel.id || this.sd.channelId;
          this.sd.sizeMode = body.channel.id ? 'channel' : this.sd.sizeMode;
          this.sd.size = body.channel.size;
        }
        this.notify('已生成专业提示词，可直接微调后生成');
      } catch (error) {
        this.notify(error.message, 'error');
      } finally {
        this.sd.optimizing = false;
      }
    },

    sdRevertPrompt() {
      if (this.sd.promptSnapshot === null) return;
      this.sd.prompt = this.sd.promptSnapshot;
      this.sd.promptSnapshot = null;
      this.sd.hasOptimized = false;
      this.sd.usedFiles = [];
      this.sd.promptWarnings = [];
    },

    async sdVerifyKey() {
      if (this.sd.arkTesting) return;
      this.sd.arkTesting = true;
      this.sd.arkStatus = '正在测试连接…';
      try {
        const response = await fetch(`${FILE_PREFIX}/verify`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ api_key: this.sd.arkKey.trim() || null }),
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail || body.message || `测试失败（HTTP ${response.status}）`);
        this.sd.arkStatus = (body.ok ? '✅ ' : '❌ ') + (body.message || '测试失败');
      } catch (error) {
        this.sd.arkStatus = '❌ ' + error.message;
      } finally {
        this.sd.arkTesting = false;
      }
    },

    async sdSaveArkKey() {
      const key = this.sd.arkKey.trim();
      const model = this.sd.arkModel.trim();
      if (!key && !model) { this.sd.arkStatus = '请先填写 ARK_API_KEY 或模型 ID'; return; }
      this.sd.arkSaving = true;
      this.sd.arkStatus = '';
      try {
        const response = await fetch(`${FILE_PREFIX}/config`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...(key ? { api_key: key } : {}), ...(model ? { model } : {}) }),
        });
        if (!response.ok) throw new Error(await this.sdFailure(response, '保存失败'));
        const body = await response.json().catch(() => ({}));
        this.sdApplyCatalog(body);
        this.sd.enabled = Boolean(body.enabled);
        if (body.model) this.sd.model = body.model;
        this.sd.arkKey = '';
        this.sd.arkModel = '';
        this.sd.arkStatus = '已保存并立即生效';
        this.notify('ARK_API_KEY 已保存');
      } catch (error) {
        this.sd.arkStatus = error.message;
      } finally {
        this.sd.arkSaving = false;
      }
    },

    async sdClearArkKey() {
      this.sd.arkSaving = true;
      this.sd.arkStatus = '';
      try {
        const response = await fetch(`${FILE_PREFIX}/config`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ clear_api_key: true }),
        });
        if (!response.ok) throw new Error(await this.sdFailure(response, '清除失败'));
        const body = await response.json().catch(() => ({}));
        this.sdApplyCatalog(body);
        this.sd.enabled = Boolean(body.enabled);
        this.sd.arkStatus = '已清除';
      } catch (error) {
        this.sd.arkStatus = error.message;
      } finally {
        this.sd.arkSaving = false;
      }
    },

    async sdFailure(response, fallback) {
      try {
        const body = await response.clone().json();
        const detail = body.detail || body.message;
        if (typeof detail === 'string' && detail.trim()) return detail;
      } catch (error) { /* 非 JSON 响应 */ }
      try {
        const text = (await response.text()).trim();
        if (text && !/^Internal Server Error$/i.test(text)) {
          return `${fallback}（HTTP ${response.status}）：${text.slice(0, 180)}`;
        }
      } catch (error) { /* 忽略 */ }
      return `${fallback}（HTTP ${response.status}）`;
    },

    sdHandleKey(event) {
      if (this.mode !== 'ai' || event.defaultPrevented || event.isComposing) return;
      const typing = event.target?.closest?.('input, textarea, [contenteditable]');
      if (event.key === 'Escape') {
        if (this.sd.popover.open) { this.sdCancelPopover(); event.preventDefault(); return; }
        if (this.sd.drawMode) { this.sdClearMarks(); this.sd.drawMode = false; event.preventDefault(); }
        return;
      }
      if (typing || event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key.toLowerCase() === 'e') {
        event.preventDefault();
        this.sdToggleDraw();
      }
    },

    /* ---------------- 生图 ---------------- */
    async sdGenerate() {
      if (this.sd.busy || !this.sd.prompt.trim()) return;
      const island = window.DynamicIsland;
      island?.primeAudio();
      island?.start('sd-generate', { title: 'Seedream 生图', phase: '正在提交生成请求…', kind: 'ai' });
      this.sd.busy = 'generate';
      this.sd.error = '';
      try {
        const response = await fetch(`${FILE_PREFIX}/generate`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            prompt: this.sd.prompt.trim(),
            size: this.sd.size,
            background: this.sd.background,
            images: this.sd.refs.map((ref) => ref.url),
            project_id: this.sd.projectId || null,
            channel_id: this.sd.channelId || '',
            intent_id: this.sd.intentId || '',
            style_id: this.sd.styleId || '',
          }),
        });
        if (!response.ok) throw new Error(await this.sdFailure(response, '生成失败'));
        const body = await response.json().catch(() => ({}));
        this.sdApplyProject(body.project, body.version);
        this.sd.lastIntent = this.sd.intentId;
        const followUp = this.sd.intentId === 'clean_bg'
          ? { label: '作为底图进编辑器', onClick: () => this.sdBackgroundTemplate() }
          : { label: '拆图层并进编辑器', onClick: () => this.sdDecompose() };
        island?.succeed('sd-generate', {
          detail: `已生成 ${body.version.width}×${body.version.height}`,
          actions: [{ label: '查看结果', tone: 'ghost', onClick: () => this.sdFocusCurrent() }, followUp],
        });
      } catch (error) {
        this.sd.error = error.message;
        island?.fail('sd-generate', error.message);
      } finally {
        this.sd.busy = '';
      }
    },

    sdApplyProject(project, version) {
      if (project?.id) this.sd.projectId = project.id;
      if (Array.isArray(project?.versions) && project.versions.length) this.sd.versions = project.versions;
      else if (version) this.sd.versions = [...this.sd.versions, version];
      this.sd.current = version || this.sd.versions[this.sd.versions.length - 1] || null;
      this.sd.popover.open = false;
      this.$nextTick(() => this.sdResizeCanvas());
    },

    sdFocusCurrent() {
      this.sd.compare = false;
      this.$nextTick(() => this.$refs.sdStage?.scrollIntoView({ block: 'center', behavior: 'smooth' }));
    },

    sdSelectVersion(version) {
      if (!version || this.sd.busy) return;
      this.sd.current = version;
      this.sdClearMarks();
      this.$nextTick(() => this.sdResizeCanvas());
    },

    /* ---------------- 参考图 / 外部海报 ---------------- */
    sdReadFile(file) {
      return new Promise((resolve, reject) => {
        if (!file || !/^image\/(png|jpe?g|webp)$/i.test(file.type)) {
          reject(new Error('只支持 PNG / JPG / WebP 图片'));
          return;
        }
        const reader = new FileReader();
        reader.onload = () => resolve({ url: String(reader.result || ''), name: file.name });
        reader.onerror = () => reject(new Error('读取图片失败'));
        reader.readAsDataURL(file);
      });
    },

    async sdUploadRefs(event) {
      const files = [...(event.target.files || [])];
      event.target.value = '';
      for (const file of files) {
        if (this.sd.refs.length >= this.sd.maxRefs) { this.notify(`参考图最多 ${this.sd.maxRefs} 张`, 'error'); break; }
        try {
          this.sd.refs.push(await this.sdReadFile(file));
        } catch (error) {
          this.notify(error.message, 'error');
        }
      }
    },

    sdRemoveRef(index) {
      this.sd.refs.splice(index, 1);
    },

    async sdUploadImage(event) {
      const file = (event.target.files || [])[0];
      event.target.value = '';
      if (!file) return;
      try {
        const image = await this.sdReadFile(file);
        this.sd.projectId = null;
        this.sd.versions = [{ id: 'v1', index: 1, url: image.url, kind: 'upload', prompt: image.name }];
        this.sd.current = this.sd.versions[0];
        this.sdClearMarks();
        this.$nextTick(() => this.sdResizeCanvas());
        this.notify('已载入本地海报，可直接圈画局改或拆图层');
      } catch (error) {
        this.notify(error.message, 'error');
      }
    },

    async sdDropImage(event) {
      const file = event.dataTransfer?.files?.[0];
      if (!file) return;
      await this.sdUploadImage({ target: { files: [file], value: '' } });
    },

    /* ---------------- 圈画 ---------------- */
    sdResizeCanvas() {
      const image = this.$refs.sdImage;
      const mask = this.$refs.sdMask;
      const preview = this.$refs.sdPreview;
      if (!image || !mask || !preview || !image.naturalWidth) return;
      for (const canvas of [mask, preview]) {
        canvas.width = image.naturalWidth;
        canvas.height = image.naturalHeight;
        const context = canvas.getContext('2d');
        context.clearRect(0, 0, canvas.width, canvas.height);
      }
    },

    sdToggleDraw() {
      if (!this.sd.current) { this.notify('先生成或上传一张海报', 'error'); return; }
      this.sd.drawMode = !this.sd.drawMode;
      if (!this.sd.drawMode) this.sdClearMarks();
    },

    sdSetTool(tool) {
      this.sd.tool = tool;
      this.sdClearMarks();
    },

    sdClearMarks() {
      this.sd.popover.open = false;
      const preview = this.$refs.sdPreview;
      const mask = this.$refs.sdMask;
      for (const canvas of [preview, mask]) {
        canvas?.getContext('2d')?.clearRect(0, 0, canvas.width, canvas.height);
      }
    },

    sdPoint(event) {
      const canvas = this.$refs.sdMask;
      const rect = canvas.getBoundingClientRect();
      return {
        x: (event.clientX - rect.left) * (canvas.width / Math.max(1, rect.width)),
        y: (event.clientY - rect.top) * (canvas.height / Math.max(1, rect.height)),
        scale: canvas.width / Math.max(1, rect.width),
      };
    },

    sdPointerDown(event) {
      if (!this.sd.drawMode || this.sd.busy || event.button !== 0 || !this.sd.current) return;
      event.preventDefault();
      this.sd.popover.open = false;
      const point = this.sdPoint(event);
      const preview = this.$refs.sdPreview;
      const context = preview.getContext('2d');
      context.clearRect(0, 0, preview.width, preview.height);
      context.strokeStyle = '#ff2d55';
      context.fillStyle = 'rgba(255, 45, 85, 0.22)';
      context.lineWidth = Math.max(3, 4 * point.scale);
      context.lineCap = 'round';
      context.lineJoin = 'round';
      const drawing = { start: point, last: point, box: { x: point.x, y: point.y, w: 0, h: 0 } };
      this.sd._drawing = drawing;
      if (this.sd.tool === 'brush') {
        context.beginPath();
        context.moveTo(point.x, point.y);
        context.lineTo(point.x + 0.1, point.y + 0.1);
        context.stroke();
      }
      event.currentTarget.setPointerCapture?.(event.pointerId);
    },

    sdPointerMove(event) {
      const drawing = this.sd._drawing;
      if (!drawing) return;
      const point = this.sdPoint(event);
      const preview = this.$refs.sdPreview;
      const context = preview.getContext('2d');
      if (this.sd.tool === 'rect') {
        context.clearRect(0, 0, preview.width, preview.height);
        const x = Math.min(drawing.start.x, point.x);
        const y = Math.min(drawing.start.y, point.y);
        context.fillRect(x, y, Math.abs(point.x - drawing.start.x), Math.abs(point.y - drawing.start.y));
        context.strokeRect(x, y, Math.abs(point.x - drawing.start.x), Math.abs(point.y - drawing.start.y));
      } else {
        context.beginPath();
        context.moveTo(drawing.last.x, drawing.last.y);
        context.lineTo(point.x, point.y);
        context.stroke();
      }
      drawing.last = point;
      drawing.box = {
        x: Math.min(drawing.start.x, drawing.last.x, drawing.box.x),
        y: Math.min(drawing.start.y, drawing.last.y, drawing.box.y),
        w: Math.max(Math.abs(drawing.start.x - drawing.last.x), drawing.box.w),
        h: Math.max(Math.abs(drawing.start.y - drawing.last.y), drawing.box.h),
      };
    },

    sdPointerUp(event) {
      const drawing = this.sd._drawing;
      if (!drawing) return;
      this.sd._drawing = null;
      const preview = this.$refs.sdPreview;
      const mask = this.$refs.sdMask;
      const context = mask.getContext('2d');
      // 画笔轨迹已经是完整的；矩形需要从预览层搬进遮罩层
      if (this.sd.tool === 'rect') {
        context.drawImage(preview, 0, 0);
      } else {
        context.drawImage(preview, 0, 0);
        preview.getContext('2d').clearRect(0, 0, preview.width, preview.height);
      }
      const box = drawing.box;
      const minSize = 12 * (drawing.start.scale || 1);
      if (box.w < minSize || box.h < minSize) {
        this.sdClearMarks();
        this.notify('圈选范围太小，请重新框选');
        return;
      }
      this.sdOpenPopover(box);
    },

    sdOpenPopover(box) {
      const stage = this.$refs.sdStage;
      const canvas = this.$refs.sdMask;
      const rect = canvas.getBoundingClientRect();
      const stageRect = stage.getBoundingClientRect();
      const scaleX = rect.width / Math.max(1, canvas.width);
      const scaleY = rect.height / Math.max(1, canvas.height);
      const width = 300;
      const left = clamp(rect.left - stageRect.left + box.x * scaleX, 8, Math.max(8, rect.width - width - 8));
      const below = rect.top - stageRect.top + (box.y + box.h) * scaleY + 8;
      const estimated = 132;
      const top = below + estimated > stageRect.height
        ? Math.max(8, rect.top - stageRect.top + box.y * scaleY - estimated - 8)
        : below;
      this.sd.popover = { open: true, left, top, width, text: '' };
      const focus = () => {
        if (!this.sd.popover.open) return;
        this.$refs.sdInstruction?.focus({ preventScroll: true });
      };
      this.$nextTick(focus);
      // 弹框刚显示时可能还在布局中，补一次微延迟聚焦，避免偶发丢焦点
      setTimeout(focus, 40);
    },

    sdCancelPopover() {
      this.sd.popover.open = false;
      this.sdClearMarks();
    },

    async sdSubmitEdit() {
      const popover = this.sd.popover;
      if (!popover.open || this.sd.busy) return;
      if (!popover.text.trim()) { this.$refs.sdInstruction?.focus(); return; }
      const instruction = popover.text.trim();
      const island = window.DynamicIsland;
      island?.primeAudio();
      island?.start('sd-edit', { title: '圈画局改', phase: '正在上传标注…', kind: 'ai' });
      this.sd.busy = 'edit';
      this.sd.error = '';
      try {
        const mask = this.$refs.sdMask;
        const merged = document.createElement('canvas');
        merged.width = mask.width;
        merged.height = mask.height;
        const context = merged.getContext('2d');
        context.drawImage(this.$refs.sdPreview, 0, 0);
        context.drawImage(mask, 0, 0);
        const maskUrl = merged.toDataURL('image/png');
        island?.update('sd-edit', { phase: '云端正在修改标注区域…' });
        const response = await fetch(`${FILE_PREFIX}/edit`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            instruction,
            base_image: this.sd.current.url,
            mask_image: maskUrl,
            size: 'auto',
            project_id: this.sd.projectId || null,
          }),
        });
        if (!response.ok) throw new Error(await this.sdFailure(response, '修改失败'));
        const body = await response.json().catch(() => ({}));
        this.sdApplyProject(body.project, body.version);
        island?.succeed('sd-edit', {
          detail: `已生成 v${body.version.index}`,
          actions: [{ label: '查看结果', tone: 'ghost', onClick: () => this.sdFocusCurrent() }],
        });
      } catch (error) {
        this.sd.error = error.message;
        island?.fail('sd-edit', error.message);
      } finally {
        this.sd.busy = '';
      }
    },

    async sdSubmitGlobal() {
      if (!this.sd.current || this.sd.busy) return;
      const text = this.sd.editPrompt.trim();
      if (!text) { this.notify('先填写修改说明', 'error'); return; }
      const island = window.DynamicIsland;
      island?.primeAudio();
      island?.start('sd-edit', { title: '整图修改', phase: '正在提交修改…', kind: 'ai' });
      this.sd.busy = 'edit';
      this.sd.error = '';
      try {
        const response = await fetch(`${FILE_PREFIX}/edit`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            instruction: text,
            base_image: this.sd.current.url,
            size: 'auto',
            project_id: this.sd.projectId || null,
          }),
        });
        if (!response.ok) throw new Error(await this.sdFailure(response, '修改失败'));
        const body = await response.json().catch(() => ({}));
        this.sdApplyProject(body.project, body.version);
        this.sd.editPrompt = '';
        island?.succeed('sd-edit', { detail: `已生成 v${body.version.index}` });
      } catch (error) {
        this.sd.error = error.message;
        island?.fail('sd-edit', error.message);
      } finally {
        this.sd.busy = '';
      }
    },

    /* ---------------- 拆图层 ---------------- */
    sdScopeLabel(scope) {
      return { public: '公开', team: '团队', private: '私密' }[scope] || '公开';
    },

    sdScopeClass(scope) {
      if (scope === 'private') return 'bg-rose-50 text-rose-600';
      if (scope === 'team') return 'bg-amber-50 text-amber-700';
      return 'bg-emerald-50 text-emerald-700';
    },

    async sdOpenHistory() {
      this.sd.history.open = true;
      if (!this.sd.history.loaded) await this.sdLoadHistory();
    },

    sdCloseHistory() {
      this.sd.history.open = false;
    },

    async sdSetHistoryView(view) {
      this.sd.history.view = view;
      this.sd.history.loaded = false;
      await this.sdLoadHistory();
    },

    async sdLoadHistory() {
      const state = this.sd.history;
      state.loading = true;
      state.error = '';
      try {
        const params = new URLSearchParams({ view: state.view, limit: '60' });
        if (state.q.trim()) params.set('q', state.q.trim());
        const response = await fetch(`${FILE_PREFIX}/history?${params.toString()}`, { cache: 'no-store' });
        if (!response.ok) throw new Error(await this.sdFailure(response, '读取历史失败'));
        const body = await response.json();
        state.items = body.items || [];
        state.loaded = true;
        if (state.view === 'team' && !body.team_id) {
          state.error = '还没有加入团队：去右上角「工作区名片」新建或加入一个团队';
        }
      } catch (error) {
        state.error = error.message;
      } finally {
        state.loading = false;
      }
    },

    async sdApplyGeneration(item) {
      try {
        const response = await fetch(`${FILE_PREFIX}/history/${encodeURIComponent(item.generation_id)}`, { cache: 'no-store' });
        if (!response.ok) throw new Error(await this.sdFailure(response, '读取作品失败'));
        const body = await response.json();
        this.sd.projectId = body.generation_id;
        this.sd.versions = (body.versions || []).map((version, index) => ({
          id: 'v' + (version.version || index + 1),
          index: version.version || index + 1,
          url: version.url,
          width: version.width,
          height: version.height,
          kind: version.action || version.kind || 'generate',
          prompt: version.instruction || version.prompt || '',
        }));
        this.sd.current = this.sd.versions[this.sd.versions.length - 1] || null;
        this.sd.prompt = body.prompt || body.raw_input || this.sd.prompt;
        if (body.channel_id) this.sd.channelId = body.channel_id;
        if (body.intent_id) this.sd.intentId = body.intent_id;
        if (body.style_id) this.sd.styleId = body.style_id;
        if (body.size) this.sd.size = body.size;
        this.sd.hasOptimized = false;
        this.sd.promptSnapshot = null;
        this.sd.popover.open = false;
        this.sd.history.open = false;
        this.$nextTick(() => this.sdResizeCanvas());
        this.notify('已加载历史作品，可以继续圈画局改');
      } catch (error) {
        this.notify(error.message, 'error');
      }
    },

    sdReusePrompt(item) {
      this.sd.prompt = item.prompt || item.raw_input || '';
      if (item.channel_id) {
        this.sd.channelId = item.channel_id;
        const channel = this.sd.catalog.channels.find((entry) => entry.id === item.channel_id);
        if (channel) { this.sd.size = channel.size; this.sd.sizeMode = 'channel'; }
      }
      if (item.intent_id) this.sd.intentId = item.intent_id;
      if (item.style_id) this.sd.styleId = item.style_id;
      this.sd.promptSnapshot = null;
      this.sd.hasOptimized = false;
      this.sd.history.open = false;
      this.notify('提示词已复刻到左侧表单，可直接生成');
    },

    async sdCycleScope(item) {
      if (!item.owned) return;
      const order = ['public', 'team', 'private'];
      const next = order[(order.indexOf(item.scope) + 1) % order.length];
      try {
        const response = await fetch(`${FILE_PREFIX}/history/${encodeURIComponent(item.generation_id)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ scope: next }),
        });
        if (!response.ok) throw new Error(await this.sdFailure(response, '修改可见范围失败'));
        const body = await response.json();
        item.scope = body.scope;
        this.notify('可见范围已改为「' + this.sdScopeLabel(body.scope) + '」');
        if (this.sd.history.view === 'public' && body.scope !== 'public') await this.sdLoadHistory();
      } catch (error) {
        this.notify(error.message, 'error');
      }
    },

    async sdDeleteGeneration(item) {
      try {
        const response = await fetch(`${FILE_PREFIX}/history/${encodeURIComponent(item.generation_id)}`, { method: 'DELETE' });
        if (!response.ok && response.status !== 204) {
          throw new Error(await this.sdFailure(response, '删除失败'));
        }
        this.sd.history.items = this.sd.history.items.filter((entry) => entry.generation_id !== item.generation_id);
        if (this.sd.projectId === item.generation_id) {
          this.sd.projectId = null;
          this.sd.versions = [];
          this.sd.current = null;
        }
        this.notify('已删除该作品');
      } catch (error) {
        this.notify(error.message, 'error');
      }
    },

    sdOpenImagePicker() {
      this.$refs.sdImageInput?.click();
    },

    async sdDownloadCurrent() {
      const current = this.sd.current;
      if (!current) return;
      try {
        const response = await fetch(current.url);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const blob = await response.blob();
        const objectUrl = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = objectUrl;
        link.download = current.kind === 'upload' ? 'poster.png' : `seedream-v${current.index || 1}.png`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(objectUrl), 5000);
      } catch (error) {
        this.notify(`下载失败：${error.message}`, 'error');
      }
    },

    async sdBackgroundTemplate() {
      if (!this.sd.current || this.sd.busy) return;
      const island = window.DynamicIsland;
      island?.primeAudio();
      island?.start('sd-bg-template', { title: '底图载入编辑器', phase: '正在封装背景层…', kind: 'template' });
      this.sd.busy = 'background';
      this.sd.error = '';
      try {
        const response = await fetch(`${FILE_PREFIX}/background-template`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            image: this.sd.current.url,
            project_id: this.sd.projectId || null,
          }),
        });
        if (!response.ok) throw new Error(await this.sdFailure(response, '底图载入失败'));
        const body = await response.json().catch(() => ({}));
        const template = body.template;
        if (!template?.name) throw new Error('底图已封装但未生成模板');
        await this.sdOpenTemplate(template.name);
        island?.succeed('sd-bg-template', {
          detail: '背景层已锁定在最底层，可直接套字',
          actions: [{ label: '进编辑器', tone: 'ghost', onClick: () => this.sdOpenTemplate(template.name) }],
        });
      } catch (error) {
        this.sd.error = error.message;
        island?.fail('sd-bg-template', error.message);
      } finally {
        this.sd.busy = '';
      }
    },

    async sdDecompose() {
      if (!this.sd.current || this.sd.busy) return;
      const island = window.DynamicIsland;
      island?.primeAudio();
      island?.start('sd-decompose', { title: '拆图层并装配', phase: '正在拆分图层…', kind: 'template' });
      this.sd.busy = 'decompose';
      this.sd.error = '';
      try {
        island?.update('sd-decompose', { phase: '下载图层并组装 SVG…' });
        const response = await fetch(`${FILE_PREFIX}/decompose`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            image: this.sd.current.url,
            max_images: this.sd.maxLayers,
            project_id: this.sd.projectId || null,
            name: 'seedream',
          }),
        });
        if (!response.ok) throw new Error(await this.sdFailure(response, '拆图层失败'));
        const body = await response.json().catch(() => ({}));
        const template = body.decomposition?.template;
        if (!template?.name) throw new Error('拆图层完成但未生成模板');
        island?.update('sd-decompose', { phase: '正在打开编辑器…' });
        await this.sdOpenTemplate(template.name);
        island?.succeed('sd-decompose', {
          detail: `已拆出 ${body.decomposition.layers.length} 个图层，可在编辑器继续修改`,
          actions: [{ label: '进编辑器', tone: 'ghost', onClick: () => this.sdOpenTemplate(template.name) }],
        });
      } catch (error) {
        this.sd.error = error.message;
        island?.fail('sd-decompose', error.message);
      } finally {
        this.sd.busy = '';
      }
    },

    async sdOpenTemplate(name) {
      this.switchMode('editor');
      await this.selectTemplate({ name, analyzed: true, display_name: name });
    },
  };
}
