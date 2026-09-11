// Per-frame perception. ONE function, injected once per frame per observation.
//
// Full pass:  evaluate(js, {gen, ...selectors}) -> {gen, url, title, controls, tables, text_digest}
//   Registers every returned element in window.__us.nodes; each item carries `idx` into that
//   array. `gen` guards the registry: a ref from an older observation raises instead of silently
//   resolving to whatever now sits at that index.
// Query pass: evaluate_handle(js, {query: {...}, ...selectors}) -> Element[]
//   Same label/table inference, no registry mutation. Used by the resolver's inferred_label and
//   table_cell rungs so resolution never depends on (or disturbs) the ref registry.
//
// Why a DOM pass at all: HTML-AAM does not compute an accessible name from an adjacent <td>, so
// the AX tree says `textbox ""` for the member-number input. The label ladder below is how the
// legacy case gets a name.
(cfg) => {
  cfg = cfg || {};
  const SEL = {
    table: cfg.table_selector || 'table',
    row: cfg.row_selector || 'tr',
    cell: cfg.cell_selector || 'td,th',
    header: cfg.header_selector || 'th',
  };
  const CONTROL = 'input,button,select,textarea,a[href]';
  const doc = document;

  const clean = (s) => (s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim().replace(/[\s:*]+$/, '').trim();
  const rect = (el) => { const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height }; };
  const visible = (el) => {
    if (!(el instanceof Element)) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0';
  };
  const text = (el) => clean(el.innerText !== undefined ? el.innerText : el.textContent);
  const oneLine = (el) => !/[\n\t]/.test((el.innerText || '').trim());

  const role = (el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : null;
    if (tag === 'button') return 'button';
    if (tag === 'select') return (el.multiple || el.size > 1) ? 'listbox' : 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'submit' || t === 'button' || t === 'reset' || t === 'image') return 'button';
      if (t === 'checkbox' || t === 'radio') return t;
      if (t === 'hidden' || t === 'file') return null;
      return 'textbox';
    }
    return null;
  };

  // The AX name, as HTML-AAM would compute it for the roles we care about. "" is the common case.
  const axName = (el, r) => {
    const aria = clean(el.getAttribute('aria-label'));
    if (aria) return aria;
    if (el.id) { const l = doc.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return clean(l.textContent); }
    const wrap = el.closest('label');
    if (wrap) return clean(wrap.textContent);
    if (r === 'button') return clean(el.tagName === 'INPUT' ? (el.type === 'image' ? el.alt : el.value) : el.textContent);
    if (r === 'link') return text(el) || clean((el.querySelector('img') || {}).alt);
    return '';
  };

  const isLabelCell = (c) => {
    if (!c || !c.matches(SEL.cell)) return false;
    if (c.classList.contains('lbl') || c.tagName === 'TH') return true;
    const raw = (c.textContent || '').replace(/\u00a0/g, ' ').trim();
    if (/:$/.test(raw)) return true;
    const b = c.querySelector('b,strong');
    return !!b && clean(b.textContent) === clean(raw) && raw !== '';
  };

  // Nearest short visible text node to the LEFT (>=50% vertical overlap), else ABOVE within
  // 60px. This is what unifies tables and divs: it does not care what element holds the text.
  let textRects = null;
  const geometricLabel = (el) => {
    const r = el.getBoundingClientRect();
    if (textRects === null) {
      textRects = [];
      const w = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT);
      for (let n = w.nextNode(); n; n = w.nextNode()) {
        const t = clean(n.textContent);
        if (!t || t.length > 60) continue;
        const p = n.parentElement;
        if (!p || p.closest('script,style') || p.closest(CONTROL) || !visible(p)) continue;
        const range = doc.createRange(); range.selectNodeContents(n);
        const tr = range.getBoundingClientRect();
        if (tr.width >= 1) textRects.push({ t, tr });
        if (textRects.length > 2000) break;
      }
    }
    let left = null, above = null, dl = Infinity, da = Infinity;
    for (const { t, tr } of textRects) {
      const vOverlap = Math.min(tr.bottom, r.bottom) - Math.max(tr.top, r.top);
      const hOverlap = Math.min(tr.right, r.right) - Math.max(tr.left, r.left);
      if (tr.right <= r.left + 2 && vOverlap >= 0.5 * Math.min(r.height, tr.height)) {
        const d = r.left - tr.right; if (d < dl) { dl = d; left = t; }
      } else if (tr.bottom <= r.top + 2 && r.top - tr.bottom <= 60 && (hOverlap > 0 || Math.abs(tr.left - r.left) <= 40)) {
        const d = r.top - tr.bottom; if (d < da) { da = d; above = t; }
      }
    }
    return left || above;
  };

  // Returns [label, source, confidence].
  const inferLabel = (el, r) => {
    if (r === 'button') { const v = axName(el, r); return v ? [v, 'value', 0.9] : ['', 'none', 0]; }
    if (r === 'link') { const v = axName(el, r); return v ? [v, 'aria', 0.9] : ['', 'none', 0]; }
    if (el.id) {
      const l = doc.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && clean(l.textContent)) return [clean(l.textContent), 'label_for', 1.0];
    }
    const wrap = el.closest('label');
    if (wrap && clean(wrap.textContent)) return [clean(wrap.textContent), 'label_wrap', 0.95];
    const cell = el.closest(SEL.cell);
    if (cell) {
      let p = cell.previousElementSibling;
      while (p && !text(p)) p = p.previousElementSibling;
      if (p && p.matches(SEL.cell) && !p.querySelector(CONTROL) && text(p)) return [text(p), 'row_cell', 0.9];
    }
    let node = el;
    for (let up = 0; up < 3 && node && node !== doc.body; up++) {
      let s = node.previousSibling;
      while (s) {
        if (s.nodeType === 3) {
          const t = clean(s.textContent);
          if (t) { if (t.length <= 60) return [t, 'preceding_text', 0.75]; s = null; break; }
        } else if (s.nodeType === 1) {
          if (s.matches(CONTROL) || s.querySelector(CONTROL)) { s = null; break; }
          const t = text(s);
          // A multi-line block (e.g. a whole row of headers) is not this control's label.
          if (t) { if (t.length <= 60 && oneLine(s)) return [t, 'preceding_text', 0.75]; s = null; break; }
        }
        s = s && s.previousSibling;
      }
      node = node.parentElement;
    }
    const g = visible(el) ? geometricLabel(el) : null;
    if (g) return [g, 'geometric', 0.6];
    if (el.placeholder && clean(el.placeholder)) return [clean(el.placeholder), 'placeholder', 0.55];
    if (el.name) return [el.name, 'name_attr', 0.4];
    return ['', 'none', 0];
  };

  // ---- tables -------------------------------------------------------------------------------
  // Starts with a letter and holds no digit: "Regular Shares" and "Lovelace, Ada" are anchors;
  // "SAV-001" and "$2,499.00" are per-record data that would pin a recording to one member.
  const wordish = (s) => /^[A-Za-z][^0-9]*$/.test(s);
  const tables = [];             // {el, caption, columns, anchorIdx, rows: [{cells, isHeader}]}
  const cellPos = new Map();     // cell element -> table_position
  for (const t of doc.querySelectorAll(SEL.table)) {
    if (!visible(t)) continue;
    const rows = [...t.querySelectorAll(SEL.row)].filter((r) => r.closest(SEL.table) === t);
    const cellsOf = (r) => [...r.querySelectorAll(SEL.cell)].filter((c) => c.closest(SEL.row) === r);
    let header = null, hi = -1;
    for (let i = 0; i < rows.length; i++) {
      const cs = cellsOf(rows[i]);
      if (cs.length >= 2 && cs.every((c) => c.matches(SEL.header) || isLabelCell(c)) && cs.every((c) => text(c))) { header = cs; hi = i; break; }
      if (cs.length) break; // a header row is the first non-empty row or nothing
    }
    if (!header) continue;
    const columns = header.map(text);
    const data = rows.slice(hi + 1).map(cellsOf).filter((cs) => cs.length >= 2 && !cs.every(isLabelCell));
    // Anchor column: the first whose values read as words (a type, a name), not an id or an
    // amount. "Regular Shares" is a stable anchor for a row; "S-001" is data that varies per record.
    let anchorIdx = 0;
    for (let i = 0; i < columns.length; i++) {
      if (data.length && data.every((cs) => cs[i] && wordish(text(cs[i])))) { anchorIdx = i; break; }
    }
    const info = { el: t, caption: clean((t.querySelector('caption') || {}).textContent), columns, anchorIdx, data };
    tables.push(info);
    for (const cs of data) {
      const anchor = text(cs[anchorIdx]);
      cs.forEach((c, i) => { if (i < columns.length) cellPos.set(c, { row_anchor: anchor, column: columns[i], anchor_column: columns[anchorIdx] }); });
    }
  }

  const valueCellLabel = (c) => {
    if (cellPos.has(c) || isLabelCell(c)) return null;
    let p = c.previousElementSibling;
    while (p && !text(p)) p = p.previousElementSibling;
    return p && isLabelCell(p) && text(p) ? text(p) : null;
  };

  const matches = (want, s) => want.some((w) => w === s.toLowerCase());
  const innerControl = (cell, r) => [...cell.querySelectorAll(CONTROL)].find((e) => role(e) === r && visible(e)) || null;

  // ---- query mode (resolver rungs) ----------------------------------------------------------
  if (cfg.query) {
    const q = cfg.query;
    if (q.kind === 'inferred_label') {
      const want = q.labels.map((s) => s.toLowerCase());
      const out = [];
      if (q.role === 'cell') {
        for (const c of doc.querySelectorAll(SEL.cell)) {
          const l = visible(c) ? valueCellLabel(c) : null;
          if (l && matches(want, l)) out.push(c);
        }
        return out;
      }
      for (const el of doc.querySelectorAll(CONTROL)) {
        if (role(el) !== q.role || !visible(el)) continue;
        if (matches(want, inferLabel(el, q.role)[0])) out.push(el);
      }
      return out;
    }
    if (q.kind === 'table_cell') {
      const col = q.column.map((s) => s.toLowerCase());
      const anchorCol = (q.anchor_column || []).map((s) => s.toLowerCase());
      const anchors = q.row_anchor.map((s) => s.toLowerCase());
      const out = [];
      for (const t of tables) {
        const ci = t.columns.findIndex((c) => matches(col, c));
        const ai = anchorCol.length ? t.columns.findIndex((c) => matches(anchorCol, c)) : 0;
        if (ci < 0 || ai < 0) continue;
        for (const cs of t.data) {
          if (!cs[ci] || !cs[ai] || !matches(anchors, text(cs[ai]))) continue;
          const target = q.inner_role ? innerControl(cs[ci], q.inner_role) : cs[ci];
          if (target && visible(target)) out.push(target);
        }
      }
      return out;
    }
    throw Error('unknown query kind ' + q.kind);
  }

  // ---- full pass ----------------------------------------------------------------------------
  const nodes = [];
  const idxOf = new Map();
  window.__us = { gen: cfg.gen, nodes };
  const register = (el) => { if (!idxOf.has(el)) idxOf.set(el, nodes.push(el) - 1); return idxOf.get(el); };

  const controls = [];
  let cellCount = 0;
  for (const el of doc.querySelectorAll(CONTROL + ',' + SEL.cell)) {
    if (!visible(el)) continue;
    if (el.matches(SEL.cell)) {
      if (cellCount >= 600) continue;
      const pos = cellPos.get(el) || null;
      const label = pos ? null : valueCellLabel(el);
      if (!pos && !label) continue;
      const v = text(el);
      if (!v) continue; // a cell whose only content is a control is covered by that control
      cellCount++;
      controls.push({
        idx: register(el), role: 'cell', name: '', inferred_label: label || '',
        label_source: label ? 'row_cell' : 'none', label_confidence: label ? 0.9 : 0,
        attrs: {}, box: rect(el), enabled: true, value: v.slice(0, 120), table_position: pos,
      });
      continue;
    }
    const r = role(el);
    if (!r) continue;
    const [label, source, conf] = inferLabel(el, r);
    const attrs = {};
    for (const a of ['name', 'id', 'placeholder', 'type', 'value', 'href', 'title', 'alt']) {
      if (a === 'value' && el.type === 'password') continue;
      const v = el.getAttribute(a);
      if (v) attrs[a] = v;
    }
    let value = null;
    if (el.type === 'password') value = null;
    else if (r === 'checkbox' || r === 'radio') value = el.checked ? 'checked' : null;
    else if (r === 'combobox' || r === 'listbox') value = clean((el.selectedOptions[0] || {}).text) || null;
    else if (r === 'textbox') value = el.value || null;
    const cell = el.closest(SEL.cell);
    controls.push({
      idx: register(el), role: r, name: axName(el, r), inferred_label: label, label_source: source,
      label_confidence: conf, attrs, box: rect(el), enabled: !el.disabled, value,
      table_position: (cell && cellPos.get(cell)) || null,
    });
  }

  const tablesOut = tables.map((t) => ({
    idx: register(t.el), caption: t.caption, columns: t.columns, anchor_column: t.columns[t.anchorIdx],
    rows: t.data.map((cs) => Object.fromEntries(t.columns.map((h, i) => [h, cs[i] ? text(cs[i]) : '']))),
    cell_refs: t.data.map((cs) => Object.fromEntries(t.columns.map((h, i) => [h, cs[i] ? register(cs[i]) : -1]))),
  }));

  // ---- text digest: headings, then table rows, then the rest --------------------------------
  const lines = [];
  const seen = new Set();
  const push = (s) => { s = clean(s); const k = s.replace(/\W/g, '').toLowerCase(); if (s && !seen.has(k)) { seen.add(k); lines.push(s); } };
  for (const h of doc.querySelectorAll('h1,h2,h3,h4,h5,h6,b,strong,font')) {
    if (!visible(h)) continue;
    const t = text(h);
    if (h.tagName[0] === 'H' || (t.length >= 6 && h.parentElement && text(h.parentElement) === t)) push(t);
  }
  for (const t of doc.querySelectorAll(SEL.table)) {
    if (!visible(t)) continue;
    for (const r of t.querySelectorAll(SEL.row)) {
      if (r.closest(SEL.table) !== t) continue;
      push([...r.querySelectorAll(SEL.cell)].filter((c) => c.closest(SEL.row) === r).map(text).filter(Boolean).join(' | '));
    }
  }
  for (const l of (doc.body ? doc.body.innerText : '').split('\n')) push(l);
  let digest = lines.join('\n');
  if (digest.length > 4000) digest = digest.slice(0, 4000);

  return { gen: cfg.gen, url: location.href, title: doc.title, controls, tables: tablesOut, text_digest: digest };
}
