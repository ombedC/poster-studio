// Shared by the workbench and the Playwright exporter.
globalThis.fitPosterText = function (svg, requests) {
  const groups = new Map();
  const results = {};
  const measure = (row) => {
    const { node, req } = row;
    let rect = node.getBoundingClientRect();
    if (req.direct_text || (node.localName === 'text' && node.firstChild?.nodeType === 3 && node.children.length)) {
      const parts = [...node.childNodes].filter(n => n.nodeType === 3 && n.textContent.trim());
      const boxes = parts.map(part => {
        const range = document.createRange();
        range.selectNodeContents(part);
        return range.getBoundingClientRect();
      });
      if (!boxes.length) return 0;
      rect = { width: Math.max(...boxes.map(b => b.right)) - Math.min(...boxes.map(b => b.left)) };
    }
    const scale = svg.getScreenCTM();
    return rect.width / (Math.hypot(scale?.a || 0, scale?.b || 0) || 1);
  };
  const apply = (row, size) => {
    row.node.style.setProperty('font-size', `${size}px`, 'important');
    row.node.setAttribute('font-size', String(size));
  };
  for (const [id, req] of Object.entries(requests)) {
    const node = svg.getElementById(id);
    const size = Number(req.base_font_size ?? req.font_size);
    if (!node || !(size > 0)) continue;
    const text = req.direct_text ? [...node.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('') : node.textContent;
    if (!text.trim()) continue;
    const key = req.font_fit_group || id;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push({ id, node, req, size });
  }
  // Set all baselines before measuring so parent text cannot shrink inherited peers.
  for (const rows of groups.values()) for (const row of rows) {
    const manual = Number(row.req.manual_font_size);
    if (!row.req.preserve_base && !row.req.manual_font_size) apply(row, row.size);
    if (typeof row.req.manual_font_size === 'number' && Number.isFinite(manual) && manual > 0) apply(row, manual);
  }
  for (const rows of groups.values()) {
    const auto = rows.filter(row => !row.req.manual_font_size && !row.req.preserve_base);
    if (!auto.length) continue;
    const base = Math.min(...auto.map(row => row.size));
    const floor = Math.max(0.01, ...auto.map(row => base * (row.req.min_ratio ?? 0)));
    let size = base;
    for (const row of auto) apply(row, size);
    const widthRatio = () => Math.min(1, ...auto.map(row => {
      const width = measure(row);
      const limit = Number(row.req.max_width);
      return limit > 0 && width > limit * 1.03 ? limit / width : 1;
    }));
    for (let pass = 0; pass < 8 && size > floor; pass++) {
      const ratio = widthRatio();
      if (ratio === 1) break;
      size = Math.max(floor, Math.floor(size * ratio * 100) / 100);
      for (const row of auto) apply(row, size);
    }
    for (const row of auto) {
      const length = measure(row);
      results[row.id] = { font_size: size, length, overflow: row.req.max_width > 0 && length > row.req.max_width * 1.03 };
    }
  }
  return results;
};
