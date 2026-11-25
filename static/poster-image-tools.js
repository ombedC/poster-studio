function posterImageTools() {
  const ns = 'http://www.w3.org/2000/svg';
  const xlink = 'http://www.w3.org/1999/xlink';
  let isolation = null;
  let clipSerial = 0;
  const frameBounds = new WeakMap();
  let dropTarget = null;
  let dropOutline = null;
  let dropHint = null;

  function clipValue(node) {
    const value = getComputedStyle(node).clipPath;
    return value && value !== 'none' ? value : '';
  }

  function clipDefinition(node) {
    const id = /url\(["']?[^)]*#([^"')]+)["']?\)/.exec(clipValue(node))?.[1];
    const clip = id && node.ownerSVGElement?.getElementById(id);
    return clip?.localName === 'clipPath' ? clip : null;
  }

  function clippedGroup(image) {
    for (let node = image.parentElement; node?.localName === 'g'; node = node.parentElement) {
      if (node.hasAttribute('data-image-frame')) return node;
      if (node.querySelectorAll('image').length !== 1 || node.querySelector('text')) break;
      const clip = clipDefinition(node);
      if (clip && clip.getAttribute('clipPathUnits') !== 'objectBoundingBox') return node;
    }
    return null;
  }

  function transformBox(box, matrix) {
    const points = [[box.x, box.y], [box.x + box.width, box.y],
      [box.x, box.y + box.height], [box.x + box.width, box.y + box.height]]
      .map(([x, y]) => new DOMPoint(x, y).matrixTransform(matrix));
    const x = Math.min(...points.map(point => point.x));
    const y = Math.min(...points.map(point => point.y));
    return { x, y, width: Math.max(...points.map(point => point.x)) - x,
      height: Math.max(...points.map(point => point.y)) - y };
  }

  function localFrame(image, slot) {
    if (slot.local_frame) return slot.local_frame;
    const parent = image.parentElement.getScreenCTM();
    const root = image.ownerSVGElement.getScreenCTM();
    return parent && root && slot.frame ? transformBox(slot.frame, parent.inverse().multiply(root)) : image.getBBox();
  }

  function svgRect(box) {
    const rect = document.createElementNS(ns, 'rect');
    for (const attr of ['x', 'y', 'width', 'height']) rect.setAttribute(attr, box[attr]);
    return rect;
  }

  function matrixText(matrix) {
    return `matrix(${matrix.a} ${matrix.b} ${matrix.c} ${matrix.d} ${matrix.e} ${matrix.f})`;
  }

  function imageMatrix(image) {
    return image.parentElement.getScreenCTM().inverse().multiply(image.getScreenCTM());
  }

  function setImageMatrix(image, matrix) {
    image.setAttribute('transform', matrixText(matrix));
    image.style.setProperty('transform', `matrix(${[matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f].join(',')})`, 'important');
    image.style.setProperty('transform-origin', '0 0', 'important');
    image.style.setProperty('transform-box', 'view-box', 'important');
  }

  function restoreAttribute(node, name, value) {
    if (value === null) node.removeAttribute(name);
    else node.setAttribute(name, value);
  }

  function copyClipGeometry(clip) {
    const geometry = document.createElementNS(ns, 'g');
    if (clip.hasAttribute('transform')) geometry.setAttribute('transform', clip.getAttribute('transform'));
    if (clip.hasAttribute('clip-rule')) geometry.setAttribute('clip-rule', clip.getAttribute('clip-rule'));
    for (const child of clip.children) geometry.append(child.cloneNode(true));
    geometry.querySelectorAll('[id]').forEach(node => node.removeAttribute('id'));
    return geometry;
  }

  function ensurePortraitFrame(image) {
    const directClip = clipDefinition(image);
    const existing = clippedGroup(image);
    if (existing && !directClip) return existing;
    const svg = image.ownerSVGElement;
    const parent = image.parentElement;
    const matrix = parent.getScreenCTM().inverse().multiply(image.getScreenCTM());
    const box = image.getBBox();
    if (!(box.width > 0 && box.height > 0) || !Number.isFinite(matrix.a)) return null;

    let id;
    do { id = `poster-image-frame-${++clipSerial}`; } while (svg.getElementById(id));
    const definition = document.createElementNS(ns, 'clipPath');
    definition.id = id;
    if (directClip) {
      const normalized = directClip.getAttribute('clipPathUnits') === 'objectBoundingBox';
      const geometry = copyClipGeometry(directClip);
      // Keep SVG clip shapes as direct children of clipPath for browser rendering.
      const mapped = normalized ? matrix.translate(box.x, box.y).scale(box.width, box.height) : matrix;
      const prefix = [matrixText(mapped), geometry.getAttribute('transform')];
      for (const child of [...geometry.children]) {
        child.setAttribute('transform', [...prefix, child.getAttribute('transform')].filter(Boolean).join(' '));
        if (geometry.hasAttribute('clip-rule') && !child.hasAttribute('clip-rule')) {
          child.setAttribute('clip-rule', geometry.getAttribute('clip-rule'));
        }
        definition.append(child);
      }
      image.removeAttribute('clip-path');
      if (clipValue(image)) image.style.setProperty('clip-path', 'none', 'important');
    } else {
      const rect = svgRect(box);
      rect.setAttribute('transform', matrixText(matrix));
      definition.append(rect);
    }
    const frame = document.createElementNS(ns, 'g');
    frame.setAttribute('data-image-frame', '');
    frame.setAttribute('clip-path', `url(#${id})`);
    const defs = document.createElementNS(ns, 'defs');
    defs.append(definition);
    parent.insertBefore(frame, image);
    frame.append(defs, image);
    return frame;
  }

  function avatarFrameBox(image, slot) {
    // 头像可能被多层 clip 包住（Illustrator 常见：外层大矩形 + 内层圆）。
    // 不能直接取最近的那层，必须挑「与 schema 坑位框尺寸一致」的那层圆/框。
    const svg = image.ownerSVGElement;
    const parent = image.parentElement;
    const root = svg && svg.getScreenCTM();
    const parentCTM = parent && parent.getScreenCTM();
    const target = slot && slot.frame;

    if (root && parentCTM) {
      const candidates = [];
      for (let node = parent; node && node.localName === 'g'; node = node.parentElement) {
        const ctm = node.getScreenCTM();
        if (!ctm) continue;
        const clip = clipDefinition(node);
        if (!clip || clip.getAttribute('clipPathUnits') === 'objectBoundingBox') continue;
        const box = clipBox(node);
        if (!(box.width > 0 && box.height > 0)) continue;
        candidates.push(transformBox(box, root.inverse().multiply(ctm)));
      }
      let chosen = null;
      if (target && target.width > 0) {
        chosen = candidates.find((box) => Math.abs(box.width - target.width) <= target.width * 0.15
          && Math.abs(box.height - target.height) <= target.height * 0.15) || null;
      }
      if (!chosen && !target && candidates.length) {
        chosen = candidates.slice().sort((a, b) => a.width * a.height - b.width * b.height)[0];
      }
      if (chosen) {
        // 画布坐标反算回 <image> 的父级坐标系（保留父级 g 的定位 transform）
        return transformBox(chosen, parentCTM.inverse().multiply(root));
      }
    }

    if (slot && slot.local_frame) return slot.local_frame;
    if (slot && slot.frame) {
      try {
        const box = localFrame(image, slot);
        if (box && box.width > 0 && box.height > 0) return box;
      } catch (error) { /* 兜底失败就让调用方保持原样 */ }
    }
    return null;
  }

  function clipBox(frame) {
    const saved = frameBounds.get(frame);
    if (saved) return saved;
    const clip = clipDefinition(frame);
    if (!clip) return frame.getBBox();
    const probe = copyClipGeometry(clip);
    probe.setAttribute('visibility', 'hidden');
    // getBBox excludes the queried element's own transform.
    const wrapper = document.createElementNS(ns, 'g');
    wrapper.append(probe);
    frame.append(wrapper);
    try {
      const bounds = wrapper.getBBox();
      if (bounds.width > 0 && bounds.height > 0) frameBounds.set(frame, bounds);
      return bounds;
    } finally { wrapper.remove(); }
  }

  function positionOverlay(area, overlay, rect) {
    if (!rect) { overlay.hidden = true; return; }
    const origin = area.getBoundingClientRect();
    overlay.hidden = false;
    Object.assign(overlay.style, { left: `${rect.x - origin.x - area.clientLeft}px`,
      top: `${rect.y - origin.y - area.clientTop}px`, width: `${rect.width}px`, height: `${rect.height}px` });
  }

  function isImageFile(file) {
    return /^image\//i.test(file.type) || (!file.type && /\.(avif|bmp|gif|heic|heif|ico|jpe?g|jfif|png|svg|tiff?|webp)$/i.test(file.name));
  }

  return {
    imageIsolation: null,

    initImageTools() {
      window.addEventListener('pointerdown', event => this.imageIsolationPointerDown(event), true);
      window.addEventListener('pointermove', event => this.imageIsolationPointerMove(event), true);
      for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
        window.addEventListener(type, event => {
          if (isolation?.drag?.id === event.pointerId) isolation.drag = null;
        }, true);
      }
      window.addEventListener('keydown', event => {
        if (!isolation || !this.canvasKeysAllowed(event)) return;
        const key = event.key.toLowerCase();
        if (key === 'escape') {
          this.finishImageIsolation();
          event.preventDefault();
          event.stopImmediatePropagation();
        } else if ((event.ctrlKey || event.metaKey) && ['z', 'y'].includes(key)) {
          this.finishImageIsolation();
        } else if (event.code === 'Space' || key.startsWith('arrow')) {
          event.preventDefault();
          event.stopImmediatePropagation();
        }
      }, true);
      const area = this.$refs.canvasArea;
      dropOutline = document.createElement('div');
      dropOutline.dataset.imageDropOutline = '';
      dropOutline.style.cssText = 'position:absolute;z-index:29;pointer-events:none;border:2px dashed #6366f1;background:#6366f122';
      dropHint = document.createElement('div');
      dropHint.dataset.imageDropHint = '';
      dropHint.setAttribute('role', 'status');
      dropHint.style.cssText = 'position:absolute;z-index:29;pointer-events:none;bottom:1rem;left:50%;transform:translateX(-50%);border-radius:6px;background:#312e81;color:white;padding:6px 10px;font-size:12px';
      dropOutline.hidden = dropHint.hidden = true;
      area.append(dropOutline, dropHint);
      area.addEventListener('click', event => {
        if (isolation && event.target === isolation.surface) event.stopImmediatePropagation();
      }, true);
      window.addEventListener('blur', () => this.finishImageIsolation());
      for (const type of ['dragenter', 'dragover', 'drop']) {
        window.addEventListener(type, event => this.handleImageFileDrop(event), true);
      }
      window.addEventListener('dragleave', event => {
        if (!event.relatedTarget) this.clearImageDrop();
      }, true);
      window.addEventListener('dragend', () => this.clearImageDrop(), true);
    },

    preparePortraitFrames(svg) {
      for (const slot of this.imageSlots()) {
        if (slot.kind !== 'teacher') continue;
        const image = svg.getElementById(slot.image_id || slot.id);
        if (image?.localName === 'image') ensurePortraitFrame(image);
      }
    },

    imageOuterNode(host) {
      return host?.localName === 'image' ? clippedGroup(host) || host : host;
    },

    imageFrameRect(node) {
      if (node?.localName !== 'g' || !clipDefinition(node) || !node.querySelector('[data-image-key]')) return null;
      const matrix = node.getScreenCTM();
      return matrix ? DOMRect.fromRect(transformBox(clipBox(node), matrix)) : null;
    },

    refreshImageToolOverlays() {
      const area = this.$refs.canvasArea;
      if (isolation) positionOverlay(area, isolation.surface, this.imageFrameRect(isolation.frame));
      if (dropOutline) positionOverlay(area, dropOutline, dropTarget?.isConnected ? this.objectRect(dropTarget) : null);
    },

    beginPortraitIsolation(event, host) {
      if (this.mode !== 'editor' || this.previewLoading || host?.dataset.kind !== 'image') return false;
      const slot = this.imageSlots().find(item => item.key === host.dataset.entryId);
      if (slot?.kind !== 'teacher') return false;
      const image = host.ownerSVGElement.getElementById(slot.image_id || slot.id);
      if (image?.localName !== 'image' || !image.getAttribute('href') && !image.getAttributeNS(xlink, 'href')) return false;
      if (isolation?.image === image) return true;
      this.finishImageIsolation();
      this.commitInline();
      this.flushNudge();
      this.closeImagePopover();
      const frame = ensurePortraitFrame(image);
      if (!frame) return false;
      const box = clipBox(frame);
      if (!(box.width > 0 && box.height > 0)) return false;
      this.selectEntry(slot.key, frame, 'image', slot);
      this.pushHistory();
      if (this.moveable) this.moveable.target = null;
      const surface = document.createElement('div');
      surface.dataset.imageIsolationSurface = '';
      surface.style.cssText = 'position:absolute;z-index:28;outline:2px dashed #6366f1;box-shadow:0 0 0 1000vmax rgb(24 24 24 / 24%);touch-action:none;cursor:grab';
      this.$refs.canvasArea.append(surface);
      isolation = { image, frame, surface, key: slot.key, scale: 1, changed: false, drag: null };
      this.imageIsolation = slot.key;
      this.refreshImageToolOverlays();
      event.preventDefault();
      return true;
    },

    finishImageIsolation() {
      if (!isolation) return false;
      const { image, surface, changed } = isolation;
      isolation = null;
      this.imageIsolation = null;
      surface.remove();
      if (changed && image.isConnected) {
        image.setAttribute('data-image-content-adjusted', '');
        this.structuralEdits = true;
        this.finishChange();
      }
      this.queueSelectionRect();
      return true;
    },

    imageIsolationPointerDown(event) {
      if (!isolation) return;
      if (event.target !== isolation.surface) {
        if (!event.target.closest?.('[data-image-isolation-ui]')) this.finishImageIsolation();
        return;
      }
      event.stopImmediatePropagation();
      if (event.button !== 0 || isolation.drag) return;
      event.preventDefault();
      const { image, surface } = isolation;
      const inverse = image.getScreenCTM()?.inverse();
      if (!inverse || !Number.isFinite(inverse.a)) return;
      isolation.drag = { id: event.pointerId, x: event.clientX, y: event.clientY, inverse,
        matrix: imageMatrix(image), transform: image.getAttribute('transform'), style: image.getAttribute('style'), changed: isolation.changed };
      surface.setPointerCapture(event.pointerId);
    },

    imageIsolationPointerMove(event) {
      const drag = isolation?.drag;
      if (!drag || drag.id !== event.pointerId) return;
      event.stopImmediatePropagation();
      const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
      const moved = Math.abs(dx) + Math.abs(dy) > 0.1;
      const { image } = isolation;
      if (moved) {
        const { a, b, c, d } = drag.inverse;
        setImageMatrix(image, drag.matrix.translate(a * dx + c * dy, b * dx + d * dy));
      } else {
        restoreAttribute(image, 'transform', drag.transform);
        restoreAttribute(image, 'style', drag.style);
      }
      isolation.changed = moved || drag.changed;
    },

    handleImageIsolationWheel(event) {
      if (!isolation) return false;
      event.preventDefault();
      if (event.target !== isolation.surface || isolation.drag) return true;
      const { image, scale } = isolation;
      const m = image.getScreenCTM()?.inverse();
      if (!m || !Number.isFinite(m.a)) return true;
      const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 400 : 1);
      const next = Math.min(16, Math.max(0.1, scale * Math.exp(Math.max(-1, Math.min(1, -delta * 0.002)))));
      if (next === scale) return true;
      const anchor = new DOMPoint(event.clientX, event.clientY).matrixTransform(m);
      setImageMatrix(image, imageMatrix(image).translate(anchor.x, anchor.y).scale(next / scale).translate(-anchor.x, -anchor.y));
      isolation.scale = next;
      isolation.changed = true;
      return true;
    },

    handleImageFileDrop(event) {
      const transfer = event.dataTransfer;
      if (this.mode !== 'editor' || !transfer) return;
      const file = Array.from(transfer.files).find(isImageFile);
      const maybeImage = file || Array.from(transfer.items).some(item => item.kind === 'file' &&
        (!item.type || /^image\//i.test(item.type))) || (!transfer.items.length && Array.from(transfer.types).includes('Files'));
      if (!maybeImage) return;
      event.preventDefault();
      const enabled = !this.galleryOpen && !this.deleteTarget && !this.ai.settingsOpen;
      const isolatedTarget = isolation && event.target === isolation.surface;
      const host = enabled && this.eventHost(event.target);
      let target = isolatedTarget ? isolation.frame : this.imageOuterNode(host);
      let key = isolatedTarget ? isolation.key : host?.dataset.imageKey;
      if (enabled && !host && !isolatedTarget && this.$refs.stage.contains(event.target)) {
        const images = [...this.$refs.stage.querySelectorAll('image[data-image-key]')].reverse();
        for (const image of images) {
          if (!this.editableHost(image)) continue;
          const frame = this.imageOuterNode(image);
          const bounds = this.imageFrameRect(frame);
          if (bounds && event.clientX >= bounds.left && event.clientX <= bounds.right &&
              event.clientY >= bounds.top && event.clientY <= bounds.bottom) {
            key = image.dataset.imageKey;
            target = frame;
            break;
          }
        }
      }
      const slot = enabled && this.imageSlots().find(item => item.key === key);
      if (event.type === 'drop') {
        this.clearImageDrop();
        if (slot && file) {
          this.finishImageIsolation();
          this.uploadImageFile(slot, file, { preserveGeometry: true });
        } else if (enabled && file) {
          this.notify('请将图片拖到头像或二维码上');
        }
        return;
      }
      if (event.type === 'dragover') transfer.dropEffect = slot ? 'copy' : 'none';
      dropTarget = slot ? target : null;
      dropHint.hidden = !enabled;
      dropHint.textContent = slot ? `松开以替换${slot.title}` : '将图片拖到头像或二维码上';
      this.refreshImageToolOverlays();
    },

    clearImageDrop() {
      dropTarget = null;
      dropHint.hidden = true;
      dropOutline.hidden = true;
    },

    applyImageToEntry(key, url, preserveGeometry = true) {
      const slot = this.imageSlots().find(item => item.key === key);
      const image = slot && this.$refs.stage.querySelector('svg')?.getElementById(slot.image_id || slot.id);
      if (image?.localName !== 'image') return;
      const absolute = new URL(url, window.location.origin).href;
      this.images[key] = absolute;
      if (slot.kind === 'teacher') {
        // 头像：忽略模板残留的 matrix 与原始像素尺寸，统一贴合 clipPath 真实包围盒
        const box = avatarFrameBox(image, slot);
        if (box) {
          image.removeAttribute('transform');
          image.style.removeProperty('transform');
          image.style.removeProperty('transform-origin');
          image.style.removeProperty('transform-box');
          for (const attr of ['x', 'y', 'width', 'height']) image.setAttribute(attr, box[attr]);
          image.removeAttribute('data-image-content-adjusted');
        }
      } else if (slot.frame && !preserveGeometry && !image.hasAttribute('data-image-content-adjusted')) {
        const frame = localFrame(image, slot);
        image.removeAttribute('transform');
        image.style.removeProperty('transform');
        for (const attr of ['x', 'y', 'width', 'height']) image.setAttribute(attr, frame[attr]);
      }
      image.setAttribute('preserveAspectRatio', slot.kind === 'qrcode' ? 'xMidYMid meet' : 'xMidYMid slice');
      image.setAttribute('href', absolute);
      image.setAttributeNS(xlink, 'xlink:href', absolute);
      this.queueSelectionRect();
    },

    async uploadImageFile(slot, file, { popover = null, preserveGeometry = true } = {}) {
      if (this.uploading[slot.key]) return;
      const documentId = this.documentId;
      this.uploading[slot.key] = true;
      if (popover) popover.uploading = true;
      try {
        const form = new FormData();
        form.append('file', file);
        form.append('kind', slot.kind);
        form.append('mode', slot.mode || 'head_shoulder');
        form.append('label', file.name);
        if (slot.frame) {
          form.append('width', String(Math.round(slot.frame.width * 2)));
          form.append('height', String(Math.round(slot.frame.height * 2)));
        }
        const response = await fetch('/api/v1/posters/assets/upload', { method: 'POST', body: form });
        const body = await response.json();
        if (documentId !== this.documentId) return;
        if (!response.ok) throw new Error(body.message || '上传处理失败');
        this.finishImageIsolation();
        this.commitInline();
        this.pushHistory();
        this.applyImageToEntry(slot.key, body.asset.url, preserveGeometry);
        this.finishChange();
        this.showWarnings(body.warnings);
        if (popover && this.imagePopover === popover) this.closeImagePopover();
        this.loadAssets(slot.kind);
        this.notify(popover || preserveGeometry ? '图片已替换' : '已处理并应用「' + slot.title + '」');
      } catch (error) {
        if (documentId === this.documentId) this.notify(error.message, 'error');
      } finally {
        if (documentId === this.documentId) this.uploading[slot.key] = false;
        if (popover) popover.uploading = false;
      }
    },

    async uploadInlineImage(event) {
      const input = event.target;
      const file = input.files?.[0];
      const popover = this.imagePopover;
      const slot = popover && this.imageSlots().find(item => item.key === popover.key);
      if (file && slot) await this.uploadImageFile(slot, file, { popover });
      input.value = '';
    },

    async uploadSlot(slot, event) {
      const input = event.target;
      const file = input.files?.[0];
      if (file) await this.uploadImageFile(slot, file);
      input.value = '';
    },
  };
}
