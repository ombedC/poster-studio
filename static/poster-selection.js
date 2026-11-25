function posterSelectionTools() {
  const bounds = boxes => new DOMRect(
    Math.min(...boxes.map(r => r.left)), Math.min(...boxes.map(r => r.top)),
    Math.max(...boxes.map(r => r.right)) - Math.min(...boxes.map(r => r.left)),
    Math.max(...boxes.map(r => r.bottom)) - Math.min(...boxes.map(r => r.top)),
  );
  let picker = null;
  const uniqueId = prefix => prefix + '-' + Array.from(crypto.getRandomValues(new Uint32Array(4)), n => n.toString(16).padStart(8, '0')).join('');
  return {
    selection: [],
    alignTo: 'board',
    pickingColor: false,

    selectionObject(host) {
      const id = host.dataset.entryId;
      const kind = host.dataset.kind;
      const entry = kind === 'text' ? this.findEntry(id) : null;
      const wrapper = kind === 'image' ? this.imageOuterNode(host) : this.objectWrapper(host);
      return { id, kind, entry, wrapper, image: kind === 'image' ? this.imageSlots().find(slot => slot.key === id) : null };
    },

    objectsInRect(rect) {
      const hits = new Map();
      const board = this.$refs.paper.getBoundingClientRect();
      for (const host of this.$refs.stage.querySelectorAll('[data-entry-id]')) {
        if (!this.editableHost(host)) continue;
        const object = this.selectionObject(host);
        const box = this.objectRect(object.wrapper);
        if (!box.width || !box.height) continue;
        const hidden = object.wrapper.checkVisibility && !object.wrapper.checkVisibility({ opacityProperty: true, visibilityProperty: true });
        if (hidden) continue;
        const left = this.trimView ? Math.max(box.left, board.left) : box.left;
        const right = this.trimView ? Math.min(box.right, board.right) : box.right;
        const top = this.trimView ? Math.max(box.top, board.top) : box.top;
        const bottom = this.trimView ? Math.min(box.bottom, board.bottom) : box.bottom;
        if (left < right && top < bottom && left <= rect.right && right >= rect.left && top <= rect.bottom && bottom >= rect.top) hits.set(object.id, object);
      }
      return [...hits.values()];
    },

    setSelection(objects) {
      this.clearSelection();
      if (!objects.length) return;
      if (objects.length === 1) {
        const { id, wrapper, kind, image } = objects[0];
        this.selectEntry(id, wrapper, kind, image);
        return;
      }
      this.selection = objects;
      this.alignTo = 'selection';
      this.queueSelectionRect();
    },

    selectedObjects() {
      return this.selection.filter(({ wrapper }) => wrapper.isConnected && this.editableHost(wrapper));
    },

    selectionBounds(objects) {
      return bounds(objects.map(object => this.objectRect(object.wrapper)));
    },

    selectionParts(objects) {
      const parts = [...new Map(objects.flatMap(object => this.positionParts(object.wrapper, object.entry)).map(part => [part.el, part])).values()];
      return parts.filter(part => !parts.some(other => other !== part && other.el.contains(part.el)));
    },

    drawCanvasRect(element, rect) {
      const area = this.$refs.canvasArea.getBoundingClientRect();
      element.style.cssText = `left:${rect.left - area.left}px;top:${rect.top - area.top}px;width:${rect.width}px;height:${rect.height}px`;
      element.hidden = false;
    },

    marqueeRect(gesture) {
      return new DOMRect(gesture.x + Math.min(0, gesture.dx), gesture.y + Math.min(0, gesture.dy), Math.abs(gesture.dx), Math.abs(gesture.dy));
    },

    alignSelection(action) {
      const objects = this.selectedObjects();
      if (!objects.length || this.imageIsolation) return;
      const rows = objects.map(object => ({ object, rect: this.objectRect(object.wrapper) }));
      const reference = this.alignTo === 'board' ? this.$refs.paper.getBoundingClientRect() : bounds(rows.map(row => row.rect));
      const horizontal = ['left', 'center', 'right', 'distribute-x'].includes(action);
      const start = horizontal ? 'left' : 'top';
      const size = horizontal ? 'width' : 'height';
      const distribute = action.startsWith('distribute');
      if (distribute && rows.length < 3) return;
      const anchors = { left: 0, top: 0, center: .5, middle: .5, right: 1, bottom: 1 };
      const fraction = anchors[action];
      rows.sort((a, b) => a.rect[start] - b.rect[start]);
      let cursor = reference[start];
      const gap = (reference[size] - rows.reduce((sum, row) => sum + row.rect[size], 0)) / (rows.length - 1);
      const shifts = rows.map(({ object, rect }) => {
        const delta = distribute ? cursor - rect[start] : reference[start] - rect[start] + fraction * (reference[size] - rect[size]);
        cursor += rect[size] + gap;
        return { object, delta };
      });
      if (!shifts.some(({ delta }) => Math.abs(delta) > .001)) return;
      this.pushHistory();
      const moved = new Set();
      for (const { object, delta } of shifts) {
        for (const part of this.selectionParts([object])) {
          if (moved.has(part.el) || objects.some(other => other !== object && other.wrapper.contains(part.el) && other.wrapper !== part.el)) continue;
          moved.add(part.el);
          this.moveSvgPart(part, horizontal ? delta : 0, horizontal ? 0 : delta);
        }
      }
      this.structuralEdits = true;
      this.finishChange();
      this.queueSelectionRect();
    },

    deleteSelection() {
      const objects = this.selectedObjects();
      const nodes = this.selectionParts(objects).map(part => part.el);
      if (!nodes.length) return;
      this.pushHistory();
      const ids = nodes.flatMap(node => [node, ...node.querySelectorAll('[id]')]).map(node => node.id).filter(Boolean);
      this.deletedNodeIds = [...new Set([...this.deletedNodeIds, ...ids])];
      for (const node of nodes) node.remove();
      this.structuralEdits = true;
      for (const object of objects) {
        if (object.entry && !object.entry.auto) {
          delete this.values[object.id];
          delete this.inlineTexts[object.id];
        }
        if (object.image) delete this.images[object.id];
      }
      this.clearSelection();
      this.finishChange();
    },

    lockSelection() {
      const nodes = this.selectionParts(this.selectedObjects()).map(part => part.el);
      if (!nodes.length) return;
      this.pushHistory();
      for (const node of nodes) {
        node.dataset.locked = '1';
        node.dataset.userLocked = '1';
      }
      this.structuralEdits = true;
      this.clearSelection();
      this.finishChange();
    },

    unlockAll() {
      const nodes = this.$refs.stage.querySelectorAll('[data-user-locked="1"]');
      if (!nodes.length) return;
      this.pushHistory();
      for (const node of nodes) {
        delete node.dataset.userLocked;
        delete node.dataset.locked;
      }
      this.finishChange();
    },

    orderSelection(direction, extreme) {
      const nodes = this.selectionParts(this.selectedObjects()).map(part => part.el);
      const selected = new Set(nodes);
      const plans = [];
      for (const parent of new Set(nodes.map(node => node.parentElement))) {
        const before = [...parent.children];
        const protectedLayer = node => node.matches('defs, style, [data-layout-role="bg"], [data-decompose-layer="bg"]') ||
          node.querySelector('[data-layout-role="bg"], [data-decompose-layer="bg"]') ||
          (node.localName === 'image' && !this.editableHost(node) && !node.matches('[data-locked]'));
        const floor = before.reduce((index, node, i) => protectedLayer(node) ? i + 1 : index, 0);
        const after = [...before];
        if (extreme) {
          const range = before.slice(floor);
          const moving = range.filter(node => selected.has(node));
          const rest = range.filter(node => !selected.has(node));
          after.splice(floor, range.length, ...(direction > 0 ? [...rest, ...moving] : [...moving, ...rest]));
        } else {
          const start = direction > 0 ? after.length - 2 : floor + 1;
          for (let i = start; direction > 0 ? i >= floor : i < after.length; i -= direction) {
            const neighbor = i + direction;
            if (selected.has(after[i]) && !selected.has(after[neighbor])) [after[i], after[neighbor]] = [after[neighbor], after[i]];
          }
        }
        if (after.some((node, index) => node !== before[index])) plans.push({ parent, after });
      }
      if (!plans.length) return;
      this.pushHistory();
      this.structuralEdits = true;
      for (const { parent, after } of plans) {
        let next = null;
        for (const node of after.toReversed()) {
          if (node.nextElementSibling !== next) parent.insertBefore(node, next);
          next = node;
        }
      }
      this.finishChange();
      this.queueSelectionRect();
    },

    copyDragObjects(gesture) {
      if (gesture.copy) return;
      const originals = gesture.parts;
      for (const part of originals) this.moveSvgPart(part, 0, 0);
      const records = [];
      const roots = [];
      for (const object of gesture.objects) {
        const members = originals.filter(part => part.el === object.wrapper ||
          object.wrapper.contains(part.el) || part.el.dataset.entryId === object.id);
        if (!members.length) continue;
        const id = uniqueId('free');
        let wrapper;
        for (const { el } of members) {
          const clone = el.cloneNode(true);
          const ids = new Map();
          for (const node of [clone, ...clone.querySelectorAll('*')]) {
            if (node.id) { const fresh = uniqueId('copy'); ids.set(node.id, fresh); node.id = fresh; }
            for (const attr of ['data-entry-id', 'data-kind', 'data-image-key', 'data-entry-wrap', 'data-locked', 'data-user-locked', 'data-free-entry']) node.removeAttribute(attr);
            node.classList.remove('text-selected');
          }
          for (const node of [clone, ...clone.querySelectorAll('*')]) {
            for (const attr of [...node.attributes]) {
              if (attr.name === 'id') continue;
              let value = attr.value.replace(/url\((['"]?)#([^)'"\s]+)\1\)/g, (match, quote, key) => ids.has(key) ? `url(${quote}#${ids.get(key)}${quote})` : match);
              if (attr.localName === 'href' && value.startsWith('#') && ids.has(value.slice(1))) value = '#' + ids.get(value.slice(1));
              if (value !== attr.value) node.setAttributeNS(attr.namespaceURI, attr.name, value);
            }
          }
          clone.id ||= uniqueId('copy');
          clone.dataset.freeEntry = id;
          el.after(clone);
          roots.push(clone);
          wrapper ||= clone;
        }
        if (wrapper) records.push(id);
      }
      if (!roots.length) return;
      gesture.copy = { roots, originals, selection: gesture.objects };
      this.annotateCanvas(this.$refs.stage.querySelector('svg'), true);
      gesture.objects = records.map(id => this.selectionObject(roots.find(node => node.dataset.freeEntry === id)));
      gesture.parts = this.selectionParts(gesture.objects);
      this.setSelection(gesture.objects);
      this.$refs.canvasArea.classList.add('is-copying');
    },

    async startColorPick() {
      if (this.mode !== 'editor' || this.selection.length !== 1 || this.selectedKind !== 'text' || this.editing || this.imageIsolation) return;
      this.cancelColorPick();
      const session = { documentId: this.documentId, target: this.selectedWrapper, controller: new AbortController() };
      picker = session;
      this.pickingColor = true;
      if (!window.EyeDropper) return;
      try {
        const result = await new EyeDropper().open({ signal: session.controller.signal });
        this.applyPickedColor(result.sRGBHex, session);
      } catch (error) {
        if (error.name === 'AbortError' && picker === session) this.cancelColorPick();
      }
    },

    cancelColorPick() {
      const active = picker;
      picker = null;
      this.pickingColor = false;
      active?.controller.abort();
    },

    applyPickedColor(color, session = picker) {
      if (!session || session !== picker) return;
      const valid = session.documentId === this.documentId && this.mode === 'editor' && session.target === this.selectedWrapper && session.target.isConnected;
      this.cancelColorPick();
      if (valid && /^#[0-9a-f]{6}$/i.test(color)) this.updateTextProperty('fill', color);
    },

    sampleCanvasColor(target) {
      if (!this.pickingColor) return false;
      const shape = target.closest?.('path, rect, circle, ellipse, polygon, polyline, line, text, tspan, use');
      if (shape && this.$refs.stage.contains(shape)) {
        const fill = getComputedStyle(shape).fill;
        if (!/^(none|url\()/i.test(fill)) { this.applyPickedColor(this.toHexColor(fill)); return true; }
      }
      this.notify('请选择有纯色填充的对象');
      return true;
    },
  };
}
