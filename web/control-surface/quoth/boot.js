/* WebUI extension: no browser storage, raw query or excerpts outside this dialog. */
(() => {
  'use strict';
  if (window.__quothControlMounted) return;
  const handle = window.hermesExt?.register?.('quoth');
  if (!handle) return;
  window.__quothControlMounted = true;
  const P = '/api/extensions/quoth/sidecar/api/memory/v1';
  const NS = 'http://www.w3.org/2000/svg';
  // [view, icon]: the memory views lead; the retrieval lab follows, quieter.
  const memoryNav = [['Overview', 'home'], ['Review queue', 'quill'], ['Memories', 'book'], ['Core', 'candle'],
    ['Recall', 'search'], ['What the assistant said', 'speech'], ['Refresh history', 'moon']];
  const views = ['Pipeline', 'Snapshots & freshness', 'Playground', 'Quality'];
  // Views the 60-second poll may re-render: no typed input, open detail or scroll position to lose.
  const autoRefresh = new Set(['Overview', 'Snapshots & freshness', 'Quality', 'Core', 'Refresh history']);
  let dialog, restore, currentRun, privateQuery = '', poll, active = 'Overview', generation = 0;
  // Memory table filters, the open detail and review marks live in page RAM only; never browser storage.
  const memoryState = {filters: {status: '', kind: '', authority: '', q: ''}, offset: 0, detail: null, back: null};
  const reviewState = {mode: 'order', drops: new Set()};
  const WHO = {owner: 'you', assistant: 'the assistant', imported: 'imported'};
  const UNAVAILABLE = {
    ledger_unconfigured: 'Ledger not configured: start the control sidecar with --ledger.',
    ledger_unavailable: 'Ledger file missing or unreadable (opened read-only).',
    core_unconfigured: 'Core not configured: start the control sidecar with --core-dir and/or --ledger.',
    core_unavailable: 'Published core.md could not be read.',
    sidecar_unconfigured: 'Quoth sidecar socket not configured: start the control sidecar with --quoth-socket.',
    sidecar_unavailable: 'Quoth sidecar not reachable on its socket; the assistant\'s own recall may be down too.',
    sidecar_outdated: 'The Quoth sidecar is older than this page; update it to the same release.',
    episodes_unconfigured: 'The Quoth sidecar runs without --episodes; nothing to search.',
    receipts_unconfigured: 'Refresh receipts not configured: start the control sidecar with --nightly-receipts.',
    busy: 'Another recall or search is still running. Try again in a moment.',
  };
  // Presort suggestions, named with the same tags the assistant uses in chat.
  const SUGGEST = {
    accept: {tag: 'remember', title: 'Remember', blurb: 'Durable and about you: the assistant suggests keeping it.'},
    keep: {tag: 'your call', title: 'Your call', blurb: 'The assistant could not judge; it stays pending under “ok”.'},
    none: {tag: 'unsorted', title: 'Not presorted yet', blurb: 'The assistant has not read these yet.'},
    duplicate: {tag: 'duplicate', title: 'Duplicates', blurb: 'Another memory already says this; that one stays.'},
    covered: {tag: 'in notes', title: 'Already in your notes', blurb: 'Your vault says it already; the note stays the source.'},
    stale: {tag: 'outdated', title: 'Outdated', blurb: 'True once, no longer.'},
    reject: {tag: 'drop', title: 'Drop', blurb: 'Wrong, one-off, or not about you.'},
  };
  const TO_READ = ['accept', 'keep', 'none'];
  const CLEANUP = ['duplicate', 'covered', 'stale', 'reject'];
  const QUEUE_REASONS = {pending_queue_large: 'more than 40 waiting', pending_queue_old: 'oldest waiting over two weeks'};
  const ICONS = {
    home: ['M4 11.5 12 5l8 6.5', 'M6 10v9h12v-9', 'M10 19v-5h4v5'],
    quill: ['M19.5 4.5C13 5 8.5 9.5 7 16l-1.5 3.5L9 18c6.5-1.5 10.5-6.5 10.5-13.5Z', 'M7 16.5l5-5'],
    book: ['M6 4.5h9.5A2.5 2.5 0 0 1 18 7v12.5H8.5A2.5 2.5 0 0 1 6 17Z', 'M6 17a2.5 2.5 0 0 1 2.5-2.5H18', 'M9.5 8h5'],
    candle: ['M12 3.5c1.6 1.8 1.6 3.4 0 4.8-1.6-1.4-1.6-3 0-4.8Z', 'M9.5 10.5h5V20h-5Z', 'M7 20h10'],
    search: ['M10.5 4.5a6 6 0 1 0 0 12 6 6 0 0 0 0-12Z', 'M15 15l4.5 4.5'],
    speech: ['M4.5 5.5h15v10h-9l-4.5 3.5v-3.5h-1.5Z', 'M9 9.5h6', 'M9 12h4'],
    moon: ['M19 14.5A7.5 7.5 0 0 1 9.5 5 7.5 7.5 0 1 0 19 14.5Z'],
    flask: ['M9.5 3.5h5', 'M10.5 3.5v5.5L5.5 18a1.8 1.8 0 0 0 1.6 2.5h9.8a1.8 1.8 0 0 0 1.6-2.5l-5-9V3.5', 'M7.5 15h9'],
    close: ['M6.5 6.5l11 11', 'M17.5 6.5l-11 11'],
    copy: ['M9 9h10v10.5H9Z', 'M6 15.5H5V4.5h10v1'],
    back: ['M14.5 6l-6 6 6 6'],
    next: ['M9.5 6l6 6-6 6'],
    down: ['M6.5 9.5l5.5 5.5 5.5-5.5'],
    alert: ['M12 4.5 20.5 19h-17Z', 'M12 10v4', 'M12 16.5v.5'],
    check: ['M5.5 12.5l4 4 9-9.5'],
  };
  // The mark: a perched raven, eye cut out (even-odd), on a 48-unit grid; the perch is drawn apart.
  const RAVEN = 'M3 16.4C5.6 14.4 8.6 12.9 11.4 12C13.2 10 16.6 9.2 19.8 9.9C22.6 10.6 24.6 12.4 26.2 14.4' +
    'C31.4 17 38 21.6 43.6 26.8L47.4 31.4L44.4 32.2C41.4 30.6 38.6 30 35.6 30.2C30.6 31.8 24 31.2 20 28.2' +
    'C17 25.8 15.4 23 14.6 20.8L13.2 21.2L13.4 19.4L11.8 19.6L12.2 18C10 17.6 6.4 17.4 3 16.4Z' +
    'M18 13.2a1.2 1.2 0 1 0 -2.4 0a1.2 1.2 0 1 0 2.4 0Z';

  function node(tag, text, cls) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = String(text);
    if (cls) el.className = cls;
    return el;
  }
  function button(label, action, parent, cls) {
    const el = node('button', label, cls);
    el.type = 'button';
    el.addEventListener('click', action);
    parent.append(el);
    return el;
  }
  function icon(name, cls) {
    const el = document.createElementNS(NS, 'svg');
    el.setAttribute('viewBox', '0 0 24 24'); el.setAttribute('aria-hidden', 'true'); el.setAttribute('focusable', 'false');
    el.setAttribute('class', 'quoth-icon' + (cls ? ' ' + cls : ''));
    for (const d of ICONS[name]) { const path = document.createElementNS(NS, 'path'); path.setAttribute('d', d); el.append(path); }
    return el;
  }
  function raven(cls) {
    const el = document.createElementNS(NS, 'svg');
    el.setAttribute('viewBox', '0 0 48 48'); el.setAttribute('aria-hidden', 'true'); el.setAttribute('focusable', 'false');
    el.setAttribute('class', 'quoth-raven' + (cls ? ' ' + cls : ''));
    const pose = document.createElementNS(NS, 'g'); pose.setAttribute('transform', 'rotate(16 24 24) translate(-1 -3)');
    const bird = document.createElementNS(NS, 'path'); bird.setAttribute('d', RAVEN); bird.setAttribute('fill-rule', 'evenodd');
    const perch = document.createElementNS(NS, 'path'); perch.setAttribute('d', 'M22.4 31 21.6 38.6M27 32.4 27.4 38.6M12 39h26');
    perch.setAttribute('class', 'quoth-perch');
    pose.append(bird); el.append(pose, perch);
    return el;
  }
  // A button with an icon and a visible (or screen-reader-only) label.
  function iconButton(name, label, action, parent, cls, hideLabel) {
    const el = button(undefined, action, parent, cls);
    el.append(icon(name), node('span', label, hideLabel ? 'quoth-sr' : undefined));
    return el;
  }
  async function api(path, method = 'GET', payload) {
    const response = await fetch(P + path, {
      method, credentials: 'same-origin', cache: 'no-store',
      headers: payload === undefined ? {} : {'Content-Type': 'application/json'},
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    if (!response.ok) {
      const error = new Error('API ' + response.status);
      error.status = response.status;
      try { error.code = (await response.json())?.error?.code; } catch (_) { error.code = undefined; }
      throw error;
    }
    return response.status === 204 ? null : response.json();
  }
  function failure(parent, error, fallback) {
    message(parent, UNAVAILABLE[error?.code] || fallback);
  }
  // A notice: ok (information), unavailable (warning or error). Text only, never markup.
  function message(parent, text, state = 'unavailable') {
    const p = node('p', undefined, 'quoth-state');
    p.dataset.state = state;
    p.append(icon(state === 'ok' ? 'check' : 'alert'), node('span', text));
    parent.append(p);
    return p;
  }
  // An empty state: plain meaning first, then (sometimes) a line of Poe.
  function empty(parent, text, epigraph) {
    const box = node('div', undefined, 'quoth-empty');
    box.append(raven('quoth-empty-mark'), node('p', text, 'quoth-empty-text'));
    if (epigraph) box.append(node('p', epigraph, 'quoth-epigraph'));
    parent.append(box);
    return box;
  }
  function field(parent, label, value) {
    const row = node('div', undefined, 'quoth-row');
    row.append(node('dt', label), node('dd', value == null ? 'Unknown — not observed' : value));
    parent.append(row);
    return row;
  }
  function heading(parent, title) { parent.append(node('h2', title)); }
  function pageHead(parent, title, lede, eyebrow) {
    const head = node('header', undefined, 'quoth-pagehead');
    if (eyebrow) head.append(node('p', eyebrow, 'quoth-eyebrow'));
    const h2 = node('h2', title); h2.tabIndex = -1; head.append(h2);
    if (lede) head.append(node('p', lede, 'quoth-lede'));
    parent.append(head);
    return head;
  }
  function section(parent, title, cls) {
    const el = node('section', undefined, 'quoth-section' + (cls ? ' ' + cls : ''));
    const h3 = node('h3', title); el.append(h3);
    parent.append(el);
    return el;
  }
  function chip(text, tone, parent) {
    const el = node('span', text, 'quoth-chip');
    el.dataset.tone = tone || 'plain';
    parent?.append(el);
    return el;
  }
  function dot(status) {
    const el = node('span', undefined, 'quoth-dot');
    el.dataset.status = status || 'unknown';
    el.setAttribute('aria-hidden', 'true');
    return el;
  }
  function meter(parent, value, max, label) {
    const bar = node('span', undefined, 'quoth-meter');
    bar.setAttribute('role', 'img'); bar.setAttribute('aria-label', label);
    const fill = node('span'); fill.style.width = Math.min(100, max ? value / max * 100 : 0).toFixed(1) + '%';
    bar.append(fill); parent.append(bar);
    return bar;
  }
  function number(n) { return n == null ? '—' : Number(n).toLocaleString('en-GB'); }
  function plural(n, one, many) { return number(n) + ' ' + (n === 1 ? one : (many || one + 's')); }
  function day(value) { return value ? String(value).slice(0, 10) : '—'; }
  function stamp(value) { return value ? String(value).replace('T', ' ').slice(0, 16) : '—'; }
  function ago(value) {
    const ms = Date.now() - new Date(value).getTime();
    if (!value || Number.isNaN(ms)) return 'unknown';
    const minutes = Math.round(ms / 60000);
    if (minutes < 1) return 'just now';
    if (minutes < 90) return minutes + ' min ago';
    const hours = Math.round(minutes / 60);
    return hours < 48 ? hours + ' h ago' : Math.round(hours / 24) + ' days ago';
  }
  // Copy a reply phrase (never memory text) to the clipboard; the live region says what happened.
  async function copyText(text, control) {
    let done = false;
    try { if (window.navigator?.clipboard) { await window.navigator.clipboard.writeText(text); done = true; } } catch (_) { done = false; }
    const label = control.querySelector('span');
    if (label) label.textContent = done ? 'Copied' : 'Select to copy';
    announce(done ? 'Copied “' + text + '”' : 'Copy unavailable here: select the text instead.');
    if (typeof setTimeout === 'function') setTimeout(() => { if (label) label.textContent = 'Copy'; }, 2000);
  }
  function say(parent, phrase, cls, onCopy) {
    const wrap = node('span', undefined, 'quoth-say' + (cls ? ' ' + cls : ''));
    const text = node('q', phrase, 'quoth-phrase');
    wrap.append(text);
    const copy = iconButton('copy', 'Copy', onCopy || (() => copyText(text.textContent, copy)), wrap, 'quoth-copy');
    copy.setAttribute('aria-label', 'Copy “' + phrase + '”');
    parent.append(wrap);
    return {wrap, text, copy};
  }
  function announce(text) {
    const live = dialog?.querySelector('.quoth-live');
    if (live) live.textContent = text;
  }
  function clearSensitive() {
    generation += 1;
    if (currentRun?.run_id) api('/lab/runs/' + currentRun.run_id, 'DELETE').catch(() => {});
    for (const run of currentRun?.comparisons || []) api('/lab/runs/' + run.run_id, 'DELETE').catch(() => {});
    currentRun = null;
    privateQuery = '';
    if (dialog) {
      for (const input of dialog.querySelectorAll('[data-private]')) input.value = '';
      for (const reveal of dialog.querySelectorAll('.quoth-reveal')) reveal.replaceChildren();
    }
  }
  function close() {
    clearInterval(poll); poll = null;
    window.removeEventListener?.('resize', railEdges);
    clearSensitive();
    reviewState.drops.clear();
    if (dialog) { dialog.remove(); dialog = null; }
    restore?.(); restore = null;
  }
  function skeleton() {
    const box = node('div', undefined, 'quoth-loading');
    box.setAttribute('aria-hidden', 'true');
    for (const width of [38, 92, 84, 70, 88, 60]) { const line = node('span'); line.style.width = width + '%'; box.append(line); }
    return box;
  }
  function renderFrame(focus) {
    if (!dialog) return;
    const pane = dialog.querySelector('.quoth-content');
    pane.replaceChildren();
    const view = node('div', undefined, 'quoth-view'); const wait = skeleton();
    pane.append(view, wait); pane.setAttribute('aria-busy', 'true');
    for (const b of dialog.querySelectorAll('.quoth-nav button')) {
      if (b.dataset.view === active) { b.setAttribute('aria-current', 'page'); revealTab(b); } else b.removeAttribute('aria-current');
    }
    const render = {
      'Overview': overview, 'Memories': memories, 'Review queue': reviewQueue, 'Core': coreView, 'Recall': recall,
      'What the assistant said': assistantSaid, 'Refresh history': refreshHistory,
      'Pipeline': pipeline, 'Snapshots & freshness': snapshots,
      'Playground': playground, 'Quality': quality,
    }[active];
    render(view).catch(error => {
      if (view.isConnected) failure(view, error, 'Private control API unavailable. Check WebUI login, consent and token; no data inferred.');
    }).finally(() => {
      wait.remove(); pane.removeAttribute('aria-busy');
      if (focus && view.isConnected) view.querySelector('h2')?.focus();
    });
  }
  // Phone tab strip: say which edges have more tabs (CSS fades them) and keep the active tab in view.
  function railEdges() {
    const rail = dialog?.querySelector('.quoth-rail');
    if (!rail) return;
    const more = rail.scrollWidth - rail.clientWidth;
    rail.dataset.edge = !(more > 2) ? 'none' : rail.scrollLeft <= 2 ? 'end' : rail.scrollLeft >= more - 2 ? 'start' : 'both';
  }
  function revealTab(tab) {
    const rail = dialog.querySelector('.quoth-rail');
    if (!rail || !(rail.scrollWidth > rail.clientWidth)) return;
    const r = rail.getBoundingClientRect(), b = tab.getBoundingClientRect();
    if (b.left < r.left + 32 || b.right > r.right - 32) rail.scrollLeft += b.left - r.left - (r.width - b.width) / 2;
    railEdges();
  }
  function go(view) { clearSensitive(); memoryState.detail = null; active = view; renderFrame(true); }
  function who(c) { return WHO[c.authority] || c.authority; }
  // Ledger actors and proposers: 'owner', 'assistant' or 'assistant:<context>'; others shown as stored.
  function actor(name) {
    const value = String(name ?? '');
    if (value === 'owner') return 'you';
    if (value === 'assistant') return 'the assistant';
    if (value.startsWith('assistant:')) return 'the assistant (' + value.slice(10) + ')';
    return value;
  }
  function statusLabel(c) {
    if (c.superseded_by) return c.review_status + ' · replaced by #' + c.superseded_by;
    if (c.valid_to) return c.review_status + ' · ended ' + String(c.valid_to).slice(0, 10);
    return c.review_status;
  }
  // Rejected, expired, replaced or forgotten: on the record, never recalled.
  function nevermore(c) { return c.review_status === 'rejected' || c.review_status === 'expired' || !!c.superseded_by || !!c.valid_to; }
  function statusChip(c, parent) {
    const tone = nevermore(c) ? 'never' : c.review_status === 'pending' ? 'wait' : c.review_status === 'accepted' ? 'ok' : 'plain';
    return chip(statusLabel(c), tone, parent);
  }
  function openMemory(id) {
    if (!memoryState.detail) memoryState.back = active;
    clearSensitive(); memoryState.detail = id; active = 'Memories'; renderFrame(true);
  }
  function memoryLink(id, parent, label) {
    const link = button(label || '#' + id, () => openMemory(id), parent);
    link.className = 'quoth-link' + (label && label !== '#' + id ? ' quoth-link-text' : '');
    link.setAttribute('aria-label', 'Open memory ' + id + (label && label !== '#' + id ? ': ' + label : ''));
    return link;
  }
  function select(parent, label, value, options, onChange) {
    const wrap = node('label', label, 'quoth-field');
    const pick = node('select');
    for (const [optionValue, text] of options) {
      const option = node('option', text); option.value = optionValue; pick.append(option);
    }
    pick.value = value;
    pick.addEventListener('change', () => onChange(pick.value));
    wrap.append(pick); parent.append(wrap);
    return pick;
  }
  function table(parent, columns, rows, caption, cls) {
    const el = node('table', undefined, 'quoth-table' + (cls ? ' ' + cls : ''));
    if (caption) el.append(node('caption', caption));
    const head = node('thead'); const headRow = node('tr'); head.append(headRow);
    for (const column of columns) { const th = node('th', column); th.setAttribute('scope', 'col'); headRow.append(th); }
    const bodyRows = node('tbody');
    for (const cells of rows) {
      const tr = node('tr'); bodyRows.append(tr);
      cells.forEach((cell, index) => {
        const td = node('td'); td.dataset.label = columns[index];
        if (cell && typeof cell === 'object') td.append(cell); else td.textContent = cell == null ? '—' : String(cell);
        if (td.textContent === '—') td.dataset.empty = 'true';
        tr.append(td);
      });
    }
    el.append(head, bodyRows);
    const scroll = node('div', undefined, 'quoth-table-wrap'); scroll.append(el); parent.append(scroll);
    return el;
  }

  // ------------------------------------------------------------ overview

  async function overview(pane) {
    pageHead(pane, 'Overview', 'What waits for you, what the assistant knows, and whether last night\'s refresh ran.');
    const [queue, ledgerTotals, core, refresh, side] = await Promise.allSettled([
      api('/memory/queue'), api('/memory/claims', 'POST', {offset: 0, limit: 1}), api('/memory/core'),
      api('/memory/refresh'), api('/memory/sidecar')]);
    if (!pane.isConnected) return;
    const grid = node('div', undefined, 'quoth-overview'); pane.append(grid);
    needsYou(grid, queue);
    assistantKnows(grid, ledgerTotals, core, side);
    health(grid, refresh, side);
  }
  function needsYou(grid, result) {
    const card = section(grid, 'Needs you', 'quoth-card quoth-hero');
    if (result.status === 'rejected') { failure(card, result.reason, 'Review queue unavailable; nothing inferred.'); return; }
    const out = result.value;
    if (!out.pending) { empty(card, 'Nothing waits for your review.', '“Only this and nothing more.”'); return; }
    const plan = out.plan || fallbackPlan(out);
    const figure = node('p', undefined, 'quoth-figure');
    figure.append(node('span', number(out.pending), 'quoth-numeral'), node('span', out.pending === 1 ? 'memory waits for your word' : 'memories wait for your word'));
    card.append(figure);
    card.append(node('p', 'After the clean-up, about ' + plural(plan.to_read, 'memory', 'memories') + ' to read: ' +
      plural(plan.rounds, 'round') + ' of ' + (plan.round_size || 10) + '.', 'quoth-plan'));
    composition(card, out);
    const act = node('div', undefined, 'quoth-actions'); card.append(act);
    const tell = node('p', 'To start, say to the assistant ', 'quoth-tell'); say(tell, 'review memory'); act.append(tell);
    iconButton('next', 'Open the review queue', () => go('Review queue'), act, 'quoth-primary');
    const meta = node('p', undefined, 'quoth-meta'); card.append(meta);
    meta.append(dot(out.status), node('span', 'Queue ' + out.status + (out.reasons.includes('pending_queue_large') ? ' · more than 40 waiting' : '') +
      ' · oldest waiting ' + plural(Math.round(out.oldest_pending_age_days ?? 0), 'day')));
  }
  // Pending split into what the assistant will offer to clear and what you read, as one bar and a legend.
  function composition(parent, out) {
    const counts = out.counts || {};
    const total = Object.values(counts).reduce((a, b) => a + b, 0) || 1;
    const bar = node('div', undefined, 'quoth-stack');
    const legend = node('dl', undefined, 'quoth-legend');
    const parts = [...TO_READ, ...CLEANUP].filter(g => counts[g]);
    bar.setAttribute('role', 'img');
    bar.setAttribute('aria-label', parts.map(g => SUGGEST[g].tag + ' ' + counts[g]).join(', '));
    for (const group of parts) {
      const part = node('span'); part.dataset.group = group;
      part.style.flexGrow = String(counts[group] / total);
      bar.append(part);
    }
    for (const [name, groups] of [['To read', TO_READ], ['Clean-up', CLEANUP]]) {
      const n = groups.reduce((a, g) => a + (counts[g] || 0), 0);
      const row = node('div', undefined, 'quoth-legend-row');
      row.append(node('dt', name + ' · ' + number(n)));
      const dd = node('dd');
      for (const g of groups.filter(g => counts[g])) {
        const item = node('span', undefined, 'quoth-key'); item.dataset.group = g;
        item.append(node('i'), node('span', SUGGEST[g].tag + ' ' + number(counts[g]))); dd.append(item);
      }
      row.append(dd); legend.append(row);
    }
    parent.append(bar, legend);
  }
  // Older sidecars return no plan: derive it from the counts the same way `digest` does.
  function fallbackPlan(out) {
    const counts = out.counts || {};
    const cleanup = CLEANUP.reduce((a, g) => a + (counts[g] || 0), 0);
    const toRead = out.pending - cleanup;
    return {cleanup, to_read: toRead, unsorted: counts.none || 0, round_size: 10, rounds: Math.ceil(toRead / 10)};
  }
  function assistantKnows(grid, totalsResult, coreResult, sideResult) {
    const card = section(grid, 'What the assistant knows', 'quoth-card');
    const stats = node('dl', undefined, 'quoth-stats'); card.append(stats);
    const totals = totalsResult.status === 'fulfilled' ? totalsResult.value.ledger_totals : null;
    const accepted = sideResult.status === 'fulfilled' ? sideResult.value.ledger?.accepted_current : null;
    const stat = (label, value, note, tone) => {
      const row = node('div', undefined, 'quoth-stat'); if (tone) row.dataset.tone = tone;
      row.append(node('dt', label), node('dd', value, 'quoth-stat-value'));
      if (note) row.append(node('dd', note, 'quoth-stat-note'));
      stats.append(row);
    };
    if (!totals && accepted == null) { failure(card, totalsResult.reason, 'Ledger unavailable; nothing inferred.'); }
    else {
      stat('Remembered', number(accepted ?? totals?.accepted), accepted != null ? 'accepted and current: what recall may serve' : 'accepted, including replaced');
      if (totals) stat('Nevermore', number((totals.rejected || 0) + (totals.expired || 0)), 'rejected or expired: kept on the record, never recalled', 'never');
    }
    const core = node('div', undefined, 'quoth-core-summary'); card.append(core);
    core.append(node('h4', 'Core block'));
    if (coreResult.status === 'rejected') { failure(core, coreResult.reason, 'Core unavailable; nothing inferred.'); return; }
    const out = coreResult.value;
    const latest = out.versions?.[0];
    core.append(node('p', (out.source === 'published' ? 'Published' + (latest ? ' ' + day(latest.compiled_at) : '') : 'Live compile, nothing published') +
      (out.live ? ' · ' + plural(out.live.claim_ids.length, 'memory', 'memories') : '') + ' · loaded at the start of every session.'));
    if (out.live) {
      meter(core, out.live.chars, out.live.limit, out.live.chars + ' of ' + out.live.limit + ' characters');
      core.append(node('p', number(out.live.chars) + ' of ' + number(out.live.limit) + ' characters' +
        (out.live.dropped_ids.length ? ' · ' + out.live.dropped_ids.length + ' dropped for budget' : ''), 'quoth-meta'));
    }
    if (out.live_differs) message(core, 'The ledger changed since this core was published. New sessions load the old one until the next compile-core --publish.');
    iconButton('next', 'Read the core', () => go('Core'), core, 'quoth-quiet');
  }
  function health(grid, refreshResult, sideResult) {
    const card = section(grid, 'Health', 'quoth-card quoth-health');
    const list = node('ul', undefined, 'quoth-checks'); card.append(list);
    const row = (status, label, detail) => {
      const li = node('li'); li.dataset.status = status || 'unknown';
      li.append(dot(status), node('span', label, 'quoth-check-label'), node('span', detail, 'quoth-check-detail'));
      list.append(li);
    };
    if (sideResult.status === 'fulfilled') {
      const s = sideResult.value;
      // The sidecar's own `status` also turns red for a long review queue; that belongs to the
      // Needs-you card. This row says only that the sidecar answered; the snapshot row follows.
      row('green', 'Sidecar', 'reachable' + (s.version ? ' · v' + s.version : ''));
      const ev = s.evidence || {};
      row(ev.status === 'ok' ? 'green' : ev.status === 'disabled' ? 'unknown' : 'red', 'Vault snapshot',
        ev.status === 'ok' ? ev.snapshot_id || 'serving' : ev.status || 'unknown');
      for (const [label, lane] of [['Assistant lane', s.episodes], ['Your lane', s.owner_episodes]]) {
        if (!lane) continue;
        row(lane.status === 'disabled' ? 'unknown' : lane.status, label, lane.status === 'disabled' ? 'disabled' :
          number(lane.messages) + ' messages · ' + number(lane.pending) + ' to embed · last tick ' + ago(lane.last_tick_at) +
          (lane.reasons?.length ? ' · ' + lane.reasons.join(', ') : ''));
      }
      row(s.quote_verification ? 'green' : 'red', 'Quote check', s.quote_verification ? 'your words are verified against chat' : 'off: approvals cannot be verified');
    } else row('unknown', 'Sidecar', UNAVAILABLE[sideResult.reason?.code] || 'unavailable');
    if (refreshResult.status === 'fulfilled') {
      const hb = refreshResult.value.heartbeat;
      if (hb?.state === 'ok') {
        row(hb.status, 'Nightly refresh', (hb.last_run?.outcome || hb.status.toUpperCase()) + ' · ' + ago(hb.last_run?.recorded_at || hb.checked_at) +
          (hb.reasons.length ? ' · ' + hb.reasons.join(', ') : ''));
        row(hb.fresh_age_hours != null && hb.max_fresh_age_hours != null && hb.fresh_age_hours > hb.max_fresh_age_hours ? 'red' : 'green',
          'Fresh as of', hb.fresh_as_of ? stamp(hb.fresh_as_of) + ' (' + hb.fresh_age_hours + ' h)' : 'unknown');
      } else row('unknown', 'Nightly refresh', 'heartbeat ' + (hb?.state || 'not configured'));
    } else row('unknown', 'Nightly refresh', UNAVAILABLE[refreshResult.reason?.code] || 'unavailable');
    iconButton('next', 'Refresh history', () => go('Refresh history'), card, 'quoth-quiet');
  }

  // ------------------------------------------------------------ memories

  async function memories(pane) {
    if (memoryState.detail) return memoryDetail(pane, memoryState.detail);
    pageHead(pane, 'Memories', 'The ledger, newest first. Read-only: every decision stays in your chat with the assistant, with your own words as approval.');
    const f = memoryState.filters;
    const out = await api('/memory/claims', 'POST', {...Object.fromEntries(Object.entries(f).filter(([, v]) => v)),
      offset: memoryState.offset, limit: 50});
    if (!pane.isConnected) return;
    const filters = node('div', undefined, 'quoth-filters'); filters.setAttribute('role', 'search');
    filters.setAttribute('aria-label', 'Filter memories'); pane.append(filters);
    const refilter = (key, value) => { f[key] = value; memoryState.offset = 0; renderFrame(); };
    select(filters, 'Status', f.status, [['', 'Any status'], ...out.filters.status.map(s => [s, s])], v => refilter('status', v));
    select(filters, 'Kind', f.kind, [['', 'Any kind'], ...out.filters.kind.map(k => [k, k.replace('_', ' ')])], v => refilter('kind', v));
    select(filters, 'From', f.authority, [['', 'Anyone'], ['owner', 'You'], ['assistant', 'Assistant'], ['imported', 'Imported']],
      v => refilter('authority', v));
    const searchLabel = node('label', 'Search text', 'quoth-field quoth-search');
    const search = node('input'); search.type = 'search'; search.maxLength = 200; search.value = f.q;
    search.placeholder = 'Words in the memory';
    search.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); refilter('q', search.value.trim()); } });
    searchLabel.append(search); filters.append(searchLabel);
    const controls = node('div', undefined, 'quoth-controls'); filters.append(controls);
    button('Search', () => refilter('q', search.value.trim()), controls, 'quoth-primary');
    const filtered = Object.values(f).some(Boolean);
    if (filtered) button('Clear filters', () => { memoryState.filters = {status: '', kind: '', authority: '', q: ''}; memoryState.offset = 0; renderFrame(); }, controls);
    const summary = node('p', undefined, 'quoth-summary'); pane.append(summary);
    summary.append(node('span', out.total ? 'Showing ' + number(out.offset + 1) + '–' + number(out.offset + out.items.length) +
      ' of ' + number(out.total) + (filtered ? ' matching' : '') : 'No matches', 'quoth-count'));
    const totals = node('span', undefined, 'quoth-totals'); summary.append(totals);
    totals.append(node('span', 'In the ledger: ', 'quoth-sr'));
    for (const [k, v] of Object.entries(out.ledger_totals)) totals.append(node('span', k + ' ' + number(v)));
    if (!Object.keys(out.ledger_totals).length) totals.append(node('span', 'ledger empty'));
    if (!out.items.length) {
      empty(pane, filtered ? 'No memories match these filters.' : 'The ledger holds no memories yet.', filtered ? '“Darkness there, and nothing more.”' : undefined);
      return;
    }
    table(pane, ['#', 'Memory', 'Kind', 'From', 'Status', 'Presort'], out.items.map(c => {
      const open = node('span'); memoryLink(c.id, open);
      const text = node('span', c.text, 'quoth-claim-text'); if (nevermore(c)) text.dataset.never = 'true';
      return [open, text, c.kind.replace('_', ' '), who(c), statusChip(c),
        c.suggestion ? SUGGEST[c.suggestion]?.tag || c.suggestion : (c.review_status === 'pending' ? 'none' : '—')];
    }), 'Memories, newest first', 'quoth-catalogue');
    const pager = node('nav', undefined, 'quoth-pager'); pager.setAttribute('aria-label', 'Pages'); pane.append(pager);
    iconButton('back', 'Previous page', () => { memoryState.offset = Math.max(0, out.offset - out.limit); renderFrame(); }, pager).disabled = out.offset === 0;
    pager.append(node('span', 'Page ' + (Math.floor(out.offset / out.limit) + 1) + ' of ' + Math.max(1, Math.ceil(out.total / out.limit)), 'quoth-meta'));
    const next = iconButton('next', 'Next page', () => { memoryState.offset = out.offset + out.limit; renderFrame(); }, pager);
    next.disabled = out.offset + out.items.length >= out.total;
  }
  async function memoryDetail(pane, id) {
    const back = memoryState.back && memoryState.back !== 'Memories' ? memoryState.back : 'Memories';
    iconButton('back', 'Back to ' + back.toLowerCase(), () => { memoryState.detail = null; active = back; memoryState.back = null; renderFrame(true); }, pane, 'quoth-back');
    const out = await api('/memory/claims/' + id);
    if (!pane.isConnected) return;
    const c = out.claim;
    pageHead(pane, 'Memory #' + c.id, undefined, c.kind.replace('_', ' ') + ' · from ' + who(c));
    const quote = node('figure', undefined, 'quoth-quotation'); pane.append(quote);
    if (nevermore(c)) quote.dataset.never = 'true';
    quote.append(node('blockquote', c.text));
    const cite = node('figcaption');
    cite.append(node('span', (c.quote_verified ? 'In your own words, verified in chat' : 'Proposed by ' + c.proposer) + ' · recorded ' + day(c.recorded_at)));
    if (c.evidence_locator) cite.append(node('cite', c.evidence_locator, 'quoth-locator'));
    quote.append(cite);
    if (nevermore(c)) {
      const gone = node('div', undefined, 'quoth-nevermore'); pane.append(gone);
      gone.append(node('p', 'Nevermore.', 'quoth-nevermore-word'));
      const why = c.superseded_by ? 'Replaced by #' + c.superseded_by + '. '
        : c.forget_reason ? 'Forgotten: ' + c.forget_reason + '. '
        : c.review_status === 'rejected' ? 'Rejected' + (c.reviewed_at ? ' on ' + day(c.reviewed_at) : '') + '. '
        : c.review_status === 'expired' || c.valid_to ? 'Ended ' + day(c.valid_to) + '. ' : '';
      gone.append(node('p', why + 'It stays on the record with its history; the assistant does not recall it.'));
      if (c.superseded_by) memoryLink(c.superseded_by, gone, 'Read #' + c.superseded_by + ', which replaced it');
    }
    const columns = node('div', undefined, 'quoth-detail'); pane.append(columns);
    const record = section(columns, 'Record');
    const dl = node('dl', undefined, 'quoth-fields'); record.append(dl);
    field(dl, 'Status', statusLabel(c) + (c.current ? ' · current' : ''));
    field(dl, 'Kind', c.kind);
    field(dl, 'From', who(c) + ' · origin ' + c.origin + ' · proposed by ' + actor(c.proposer));
    field(dl, 'Quote verified', c.quote_verified ? 'yes: found in your own message' : 'no');
    field(dl, 'Evidence locator', c.evidence_locator || '—');
    if (c.evidence_quote) field(dl, 'Quoted words', c.evidence_quote);
    if (c.reason) field(dl, 'Reason given', c.reason);
    if (c.confidence != null) field(dl, 'Confidence', c.confidence);
    if (c.subject_key) field(dl, 'Subject key', c.subject_key);
    field(dl, 'Recorded', c.recorded_at);
    field(dl, 'Valid', (c.valid_from || 'not yet') + ' → ' + (c.valid_to || 'open'));
    field(dl, 'Reviewed', c.reviewed_at ? c.reviewed_at + (c.auto_accepted ? ' (auto-accepted)' : '') : '—');
    field(dl, 'Review note', c.review_note || '—');
    if (c.forget_reason) field(dl, 'Forget reason', c.forget_reason);
    field(dl, 'Used in recall', c.use_count + ' times' + (c.last_used_at ? ', last ' + c.last_used_at : ''));
    const side = node('div', undefined, 'quoth-detail-side'); columns.append(side);
    const presort = section(side, 'Presort suggestion');
    if (!out.suggestion) presort.append(node('p', c.review_status === 'pending' ? 'None yet.' : 'None (not pending).', 'quoth-meta'));
    else {
      const s = out.suggestion, sdl = node('dl', undefined, 'quoth-fields'); presort.append(sdl);
      field(sdl, 'Suggests', (SUGGEST[s.action]?.tag || s.action) + (s.suggested_kind ? ' as ' + s.suggested_kind : ''));
      if (s.topic) field(sdl, 'Topic', s.topic);
      if (s.rewrite) field(sdl, 'Rewrite', s.rewrite);
      if (s.note) field(sdl, 'Note', s.note);
      if (s.evidence_locator) field(sdl, 'In notes', s.evidence_locator);
      field(sdl, 'By', s.suggested_by + ' · ' + s.suggested_at);
      if (s.duplicate_of) memoryLink(s.duplicate_of, presort, 'Duplicate of #' + s.duplicate_of + ': open it');
      if (c.review_status === 'pending') {
        const tell = node('p', 'Decided only in your chat: say ', 'quoth-tell'); say(tell, 'review memory'); presort.append(tell);
      }
    }
    const chain = section(side, 'Replaces / replaced by');
    if (!out.chain.length) chain.append(node('p', 'Not part of a supersede chain.', 'quoth-meta'));
    else {
      const list = node('ol', undefined, 'quoth-chain quoth-timeline'); chain.append(list);
      for (const link of out.chain) {
        const li = node('li'); list.append(li);
        if (link.self) { li.dataset.self = 'true'; li.append(node('strong', '#' + link.id + ' (this memory) · ' + link.review_status + ' · ' + link.text)); }
        else memoryLink(link.id, li, '#' + link.id + ' · ' + link.review_status + (link.current ? ' · current' : '') + ' · ' + link.text);
      }
    }
    const history = section(side, 'History');
    const events = node('ol', undefined, 'quoth-timeline'); history.append(events);
    for (const event of out.events) {
      const detail = event.detail ? ' · ' + Object.entries(event.detail).map(([k, v]) => k + ' ' + (typeof v === 'object' ? JSON.stringify(v) : v)).join(', ') : '';
      const li = node('li'); events.append(li);
      li.append(node('time', stamp(event.at)), node('span', event.action + ' by ' + actor(event.actor) + detail));
    }
  }

  // ------------------------------------------------------------ review queue

  async function reviewQueue(pane) {
    const head = pageHead(pane, 'Review queue');
    const out = await api('/memory/queue');
    if (!pane.isConnected) return;
    if (!out.pending) {
      head.append(node('p', 'Nothing is decided on this page. Every decision is made in your chat with the assistant and carries your own reply.', 'quoth-lede'));
      empty(pane, 'Nothing waits for your review.', '“Only this and nothing more.”');
      return;
    }
    head.append(node('p', number(out.pending) + ' ' + (out.pending === 1 ? 'memory waits' : 'memories wait') +
      ' for your word. Nothing is decided on this page: every decision is made in your chat with the assistant and carries your own reply.', 'quoth-lede'));
    const plan = out.plan || fallbackPlan(out);
    const layout = node('div', undefined, 'quoth-review'); pane.append(layout);
    const main = node('div', undefined, 'quoth-review-main');
    const rail = node('div', undefined, 'quoth-review-rail');
    layout.append(main, rail);
    howItGoes(rail);
    const facts = node('dl', undefined, 'quoth-plan-strip'); main.append(facts);
    for (const [label, value, note] of [['Waiting', number(out.pending), 'queue ' + out.status + ' · oldest ' + plural(Math.round(out.oldest_pending_age_days ?? 0), 'day')],
      ['Clean-up', number(plan.cleanup), 'one reply per bucket'], ['To read', number(plan.to_read), plan.unsorted ? number(plan.unsorted) + ' not presorted' : 'all presorted'],
      ['Rounds', '≈ ' + number(plan.rounds), (plan.round_size || 10) + ' memories each']]) {
      const item = node('div'); item.append(node('dt', label), node('dd', value, 'quoth-plan-value'), node('dd', note, 'quoth-meta'));
      facts.append(item);
    }
    const modes = node('div', undefined, 'quoth-segmented'); modes.setAttribute('role', 'group'); modes.setAttribute('aria-label', 'Arrange the queue');
    main.append(modes);
    for (const [mode, label] of [['order', 'In review order'], ['suggestion', 'By suggestion']]) {
      const b = button(label, () => { reviewState.mode = mode; renderFrame(); }, modes);
      b.setAttribute('aria-pressed', String(reviewState.mode === mode));
    }
    if (out.truncated) message(main, 'Only the oldest 300 pending memories are listed; the counts and plan cover all ' + number(out.pending) + '.');
    if (reviewState.mode === 'suggestion') bySuggestion(main, out);
    else inReviewOrder(main, out, plan);
  }
  // The flow, told once: it opens wide on a desktop and folds away on narrow screens.
  function howItGoes(parent) {
    const box = node('details', undefined, 'quoth-howto');
    box.open = reviewState.howto ?? !!window.matchMedia?.('(min-width: 1181px)').matches;
    box.addEventListener('toggle', () => { reviewState.howto = box.open; });
    parent.append(box);
    const summary = node('summary'); summary.append(node('h3', 'How a review goes'), icon('down', 'quoth-chevron')); box.append(summary);
    const steps = node('ol', undefined, 'quoth-steps'); box.append(steps);
    const step = (head, build) => { const li = node('li'); li.append(node('strong', head)); const p = node('p'); build(p); li.append(p); steps.append(li); };
    const words = (p, ...parts) => parts.forEach(part => p.append(Array.isArray(part) ? node('q', part[0], 'quoth-word') : part));
    step('Say it to the assistant', p => { p.append('In your chat, say '); say(p, 'review memory'); });
    step('Clear the clean-up', p => words(p, 'It offers each bucket with a few examples. Answer ', ['ok'], ' to clear it or ', ['skip'], ' to leave it.'));
    step('Read in rounds of ten', p => words(p, 'Topic by topic, numbered. Answer ', ['ok'], ', ', ['ok, drop 3 5'], ', ', ['3: new text'], ', ',
      ['skip'], ' or ', ['stop'], '. ', ['undo'], ' takes back your last answer.'));
    box.append(node('p', 'Your reply is the approval: the assistant records your exact words with each decision. This page explains and drafts; it never decides.', 'quoth-meta'));
  }
  function inReviewOrder(pane, out, plan) {
    const items = Object.values(out.groups).flat();
    const cleanup = section(pane, 'First, the clean-up', 'quoth-phase');
    cleanup.append(node('p', number(plan.cleanup) + ' memories the assistant suggests clearing, one bucket per message. Each answer clears exactly the bucket you saw.', 'quoth-lede'));
    let anyCleanup = false;
    for (const group of CLEANUP) {
      const list = out.groups[group] || [];
      const total = out.counts[group] || 0;
      if (!total) continue;
      anyCleanup = true;
      bucket(cleanup, group, list, total, out.buckets?.[group]);
    }
    if (!anyCleanup) cleanup.append(node('p', 'Nothing to clear.', 'quoth-meta'));
    const rounds = section(pane, 'Then, topic by topic', 'quoth-phase');
    rounds.append(node('p', 'Rounds of ' + (plan.round_size || 10) + ', in the order the assistant reads them. The first round of each topic is numbered as it will number it; mark what you would drop to draft your answer.', 'quoth-lede'));
    const topics = out.topics?.length ? out.topics : topicsFrom(items);
    const next = nextRound(out);
    for (const t of topics) {
      if (!t.to_read) continue;
      topic(rounds, t, items.filter(c => topicOf(c) === t.topic && TO_READ.includes(c.suggestion?.action || 'none')),
        plan.round_size || 10, next?.topic === t.topic ? next : null, next?.topic);
    }
    if (!topics.some(t => t.to_read)) rounds.append(node('p', 'Nothing left to read once the clean-up is done.', 'quoth-meta'));
  }
  function topicOf(c) { return c.suggestion?.topic || 'Other'; }
  // The round the assistant shows next: `digest`'s default group (the first topic with something to read),
  // numbered 1..n. `complete` is false when part of it is missing from the listed 300.
  function nextRound(out) {
    const size = out.plan?.round_size || 10;
    const items = Object.values(out.groups).flat();
    const t = (out.topics?.length ? out.topics : topicsFrom(items)).find(x => x.to_read);
    if (!t) return null;
    const want = Math.min(size, t.to_read);
    const round = items.filter(c => topicOf(c) === t.topic && TO_READ.includes(c.suggestion?.action || 'none') &&
      c.position != null && c.position <= want).sort((a, b) => a.position - b.position);
    const complete = round.length === want && round.every((c, i) => c.position === i + 1);
    return {topic: t.topic, round, complete, key: t.topic + '|' + round.map(c => c.position + ':' + c.id).join(',')};
  }
  // Older sidecars return no topics: group what is listed (the oldest 300) the same way.
  function topicsFrom(items) {
    const topics = {};
    for (const c of items) {
      const t = topics[topicOf(c)] ||= {topic: topicOf(c), total: 0, to_read: 0};
      t.total += 1; t.to_read += TO_READ.includes(c.suggestion?.action || 'none') ? 1 : 0;
    }
    return Object.values(topics).sort((a, b) => (a.topic === 'Other') - (b.topic === 'Other') || a.topic.localeCompare(b.topic));
  }
  function bucket(parent, group, list, total, bucketCount) {
    const details = node('details', undefined, 'quoth-bucket'); details.dataset.group = group; parent.append(details);
    details.append(bucketSummary(group, total));
    if (total > 200) details.append(node('p', 'Over 200: the assistant clears it a page per reply.', 'quoth-meta'));
    if ((bucketCount ?? total) !== list.length) details.append(node('p', 'Showing the ' + number(list.length) + ' listed of ' + number(total) + '.', 'quoth-meta'));
    slips(details, list, {limit: 12});
  }
  function bucketSummary(group, total) {
    const summary = node('summary'); const words = node('span', undefined, 'quoth-bucket-words');
    words.append(node('span', SUGGEST[group].title, 'quoth-bucket-title'), node('span', SUGGEST[group].blurb, 'quoth-bucket-blurb'));
    summary.append(chip(SUGGEST[group].tag, group), words, node('span', number(total), 'quoth-bucket-count'), icon('down', 'quoth-chevron'));
    return summary;
  }
  // A topic's items. Only the round the assistant shows next is numbered and may be drafted; a later topic is
  // renumbered when the assistant reaches it, so drafting numbers for it could map to the wrong memories.
  function topic(parent, t, items, roundSize, next, nextTopic) {
    const article = node('article', undefined, 'quoth-topic'); parent.append(article);
    if (next) article.dataset.next = 'true';
    const head = node('header'); article.append(head);
    head.append(node('h4', t.topic));
    const rounds = Math.ceil(t.to_read / roundSize);
    head.append(node('p', number(t.to_read) + ' to read · ' + plural(rounds, 'round') +
      (t.total > t.to_read ? ' · ' + number(t.total - t.to_read) + ' in the clean-up' : ''), 'quoth-meta'));
    const ordered = [...items].sort((a, b) => (a.position ?? Infinity) - (b.position ?? Infinity));
    const first = next?.complete ? next.round : [];
    const later = ordered.filter(c => !first.includes(c));
    if (first.length) {
      head.append(node('p', 'The assistant shows this round next when you say “review memory”, numbered as it will number it. ' +
        'Copy checks the queue again first and refuses if its round has changed. If you skipped “' + t.topic +
        '” in chat, this draft does not apply.', 'quoth-meta quoth-topic-note'));
      const marks = () => first.filter(c => reviewState.drops.has(c.id)).map(c => c.position);
      const draft = () => { const drop = marks(); return drop.length ? 'ok, drop ' + drop.join(' ') : 'ok'; };
      const list = slips(article, first, {numbered: true, onDrop: () => { bar.phrase.text.textContent = draft(); bar.phrase.copy.setAttribute('aria-label', 'Copy “' + draft() + '”'); }});
      list.setAttribute('aria-label', t.topic + ', round 1');
      const bar = draftBar(article, draft, next.key);
    } else if (next) {
      head.append(node('p', 'Part of this round is beyond the 300 memories listed here, so no answer is drafted. Say “review memory” and answer what the assistant shows.', 'quoth-meta quoth-topic-note'));
    } else if (nextTopic) {
      head.append(node('p', 'The assistant reaches this topic after “' + nextTopic + '” and numbers it then; nothing is drafted for it yet.', 'quoth-meta quoth-topic-note'));
    }
    if (!next && later.length) slips(article, later.splice(0, roundSize), {});
    if (later.length) {
      const more = node('details', undefined, 'quoth-later'); article.append(more);
      more.append(node('summary', 'Later rounds · ' + number(later.length) + ' more (the assistant numbers each round afresh)'));
      slips(more, later, {limit: 30});
    }
  }
  // The drafted reply, held at the bottom of the round. Copy re-reads the queue and copies only
  // if the assistant's next round is still exactly the one numbered on this page.
  function draftBar(article, draft, key) {
    const bar = node('div', undefined, 'quoth-reply'); article.append(bar);
    bar.append(node('span', 'Your answer to round 1', 'quoth-reply-label'));
    const phrase = say(bar, draft(), 'quoth-say-large', async () => {
      phrase.copy.disabled = true; status.replaceChildren();
      try {
        const now = nextRound(await api('/memory/queue'));
        if (!bar.isConnected) return;
        if (now?.complete && now.key === key) { await copyText(draft(), phrase.copy); return; }
        bar.dataset.stale = 'true';
        status.append(node('span', !now ? 'Nothing is left to read: nothing copied.'
          : now.topic !== key.split('|')[0] ? 'The assistant has moved on to “' + now.topic + '”: this draft no longer applies. Nothing copied.'
          : 'This round changed since the page loaded, so these numbers could point at other memories. Nothing copied.'));
        button('Refresh the queue', () => renderFrame(), status, 'quoth-quiet');
        announce(status.textContent);
      } catch (error) {
        if (bar.isConnected) status.append(node('span', 'Could not re-check the queue, so nothing was copied.'));
      } finally { phrase.copy.disabled = false; }
    });
    bar.append(node('span', 'as of ' + new Date().toTimeString().slice(0, 5), 'quoth-reply-stamp'));
    const status = node('p', undefined, 'quoth-reply-status'); bar.append(status);
    return {phrase};
  }
  function bySuggestion(pane, out) {
    for (const group of [...TO_READ, ...CLEANUP]) {
      const items = out.groups[group] || [];
      if (!items.length) continue;
      const details = node('details', undefined, 'quoth-bucket'); details.open = true; details.dataset.group = group; pane.append(details);
      details.append(bucketSummary(group, out.counts[group]));
      slips(details, items, {limit: 40, showTopic: true});
    }
  }
  // Index-card list of pending memories with their provenance. `limit` folds the rest behind a button.
  function slips(parent, items, {numbered = false, onDrop, limit, showTopic} = {}) {
    const list = node(numbered ? 'ol' : 'ul', undefined, 'quoth-slips'); parent.append(list);
    const draw = upto => {
      list.replaceChildren();
      for (const c of items.slice(0, upto)) list.append(slip(c, {numbered, onDrop, showTopic}));
      if (upto < items.length) {
        const li = node('li', undefined, 'quoth-more');
        button('Show all ' + number(items.length), () => draw(items.length), li);
        list.append(li);
      }
    };
    draw(limit || items.length);
    return list;
  }
  function slip(c, {numbered, onDrop, showTopic}) {
    const s = c.suggestion || {};
    const li = node('li', undefined, 'quoth-slip' + (numbered ? ' quoth-slip-numbered' : '')); li.dataset.group = s.action || 'none';
    if (numbered) li.append(node('span', String(c.position), 'quoth-num'));
    const body = node('div', undefined, 'quoth-slip-body'); li.append(body);
    body.append(node('p', s.rewrite || c.text, 'quoth-claim-text'));
    if (s.rewrite) body.append(node('p', 'Originally: ' + c.text, 'quoth-original'));
    const prov = node('div', undefined, 'quoth-prov');
    if (s.action === 'covered' && s.evidence_locator) {
      const line = node('p'); line.append(node('span', 'In your notes ', 'quoth-prov-label'), node('cite', s.evidence_locator, 'quoth-locator'));
      prov.append(line);
    }
    if (s.action === 'duplicate' && s.duplicate_of) {
      const target = c.duplicate_target;
      const line = node('p'); line.append(node('span', 'Same as ', 'quoth-prov-label'));
      memoryLink(s.duplicate_of, line);
      if (target?.text) line.append(node('q', target.text, 'quoth-target'));
      if (target) line.append(chip(target.kept ? 'stays' : 'not kept: will not apply', target.kept ? 'ok' : 'never'));
      prov.append(line);
    }
    if (s.note) { const note = node('p', undefined, 'quoth-note'); note.append(node('span', 'Assistant\'s note ', 'quoth-prov-label'), node('span', s.note)); prov.append(note); }
    if (prov.children.length) body.append(prov);
    const meta = node('p', undefined, 'quoth-meta quoth-slip-meta'); body.append(meta);
    chip(SUGGEST[s.action || 'none'].tag, s.action || 'none', meta);
    if (showTopic && s.topic) meta.append(node('span', s.topic));
    meta.append(node('span', (s.suggested_kind || c.kind).replace('_', ' '), 'quoth-kind'), node('span', 'from ' + who(c)),
      node('span', day(c.recorded_at), 'quoth-date'));
    memoryLink(c.id, meta);
    if (numbered && onDrop) {
      const label = node('label', undefined, 'quoth-drop');
      const box = node('input'); box.type = 'checkbox'; box.checked = reviewState.drops.has(c.id);
      box.setAttribute('aria-label', 'Drop ' + c.position + ' in your drafted answer');
      box.addEventListener('change', () => { if (box.checked) reviewState.drops.add(c.id); else reviewState.drops.delete(c.id); li.dataset.drop = String(box.checked); onDrop(); });
      li.dataset.drop = String(box.checked);
      label.append(box, node('span', 'drop'));
      li.append(label);
    }
    return li;
  }

  // ------------------------------------------------------------ core

  async function coreView(pane) {
    pageHead(pane, 'Core', 'The short block the assistant loads at the start of every session: the memories it keeps closest.');
    const out = await api('/memory/core');
    if (!pane.isConnected) return;
    const facts = node('dl', undefined, 'quoth-plan-strip'); pane.append(facts);
    const fact = (label, value, note) => { const d = node('div'); d.append(node('dt', label), node('dd', value, 'quoth-plan-value')); if (note) d.append(node('dd', note, 'quoth-meta')); facts.append(d); return d; };
    fact('Shown', out.source === 'published' ? 'Published' : 'Live compile', out.source === 'published' ? out.file + ' (what the assistant loads)' : 'nothing published yet');
    if (out.live) {
      fact('Memories', number(out.live.claim_ids.length), out.live.dropped_ids.length ? out.live.dropped_ids.length + ' dropped for budget' : 'none dropped');
      const size = fact('Size', number(out.live.chars), 'of ' + number(out.live.limit) + ' characters');
      meter(size, out.live.chars, out.live.limit, out.live.chars + ' of ' + out.live.limit + ' characters');
    }
    if (out.live_differs) message(pane, 'The ledger changed since this core was published; new sessions still load the published file until the next compile-core --publish.');
    manuscript(pane, out.text);
    const raw = node('details', undefined, 'quoth-raw'); pane.append(raw);
    raw.append(node('summary', 'Exact text, as the assistant receives it'), node('pre', out.text, 'quoth-pre'));
    const versions = section(pane, 'Published versions');
    if (!out.versions.length) { versions.append(node('p', 'No published versions recorded.', 'quoth-meta')); return; }
    const list = node('ol', undefined, 'quoth-versions'); versions.append(list);
    for (const v of out.versions) {
      const li = node('li'); list.append(li);
      const line = node('p');
      line.append(node('strong', '#' + v.id), node('time', stamp(v.compiled_at)),
        node('span', number(v.chars) + ' chars · ' + (v.claims ?? '?') + ' memories'), node('code', String(v.sha256).slice(0, 12)));
      li.append(line);
      const slot = node('div', undefined, 'quoth-version-text');
      button('Show version #' + v.id, async () => {
        slot.replaceChildren();
        try { const full = await api('/memory/core/versions/' + v.id); if (slot.isConnected) slot.append(node('pre', full.text, 'quoth-pre')); }
        catch (error) { failure(slot, error, 'Version unavailable.'); }
      }, li, 'quoth-quiet');
      li.append(slot);
    }
  }
  // The core as a page: '#' lines become headings, '-' lines list items, everything as plain text.
  function manuscript(parent, text) {
    const page = node('article', undefined, 'quoth-manuscript'); page.setAttribute('aria-label', 'Core block');
    let list = null;
    for (const raw of String(text || '').split('\n')) {
      const line = raw.trimEnd();
      if (!line.trim()) { list = null; continue; }
      const head = /^(#{1,3})\s+(.*)$/.exec(line);
      const item = /^\s*[-*]\s+(.*)$/.exec(line);
      if (head) { list = null; page.append(node(head[1].length === 1 ? 'h3' : 'h4', head[2])); }
      else if (item) { if (!list) { list = node('ul'); page.append(list); } list.append(node('li', item[1])); }
      else { list = null; page.append(node('p', line)); }
    }
    if (!page.children.length) page.append(node('p', '(empty)', 'quoth-meta'));
    parent.append(page);
  }

  // ------------------------------------------------------------ recall and episodes

  async function recall(pane) {
    pageHead(pane, 'Recall', 'Type a turn as you might write it; see exactly what the assistant would be given. A dry run on the Quoth sidecar: nothing is marked used, and vault evidence appears only if it is ready within the sidecar\'s budget, as on a real turn.');
    const form = node('div', undefined, 'quoth-ask'); pane.append(form);
    const label = node('label', 'Text of a turn', 'quoth-field');
    label.append(node('span', 'Stays in page memory; cleared when you leave this view.', 'quoth-hint'));
    const text = node('textarea'); text.rows = 3; text.maxLength = 2048; text.dataset.private = 'true';
    text.placeholder = 'Where did I leave the kayak paddles?';
    label.append(text); form.append(label);
    const controls = node('div', undefined, 'quoth-controls'); form.append(controls);
    const output = node('div', undefined, 'quoth-results'); output.setAttribute('aria-live', 'polite'); pane.append(output);
    const run = async () => {
      output.replaceChildren();
      if (!text.value.trim()) { message(output, 'Enter some text first.'); return; }
      const requestGeneration = generation;
      output.append(skeleton());
      try {
        const out = await api('/memory/recall', 'POST', {text: text.value});
        if (!output.isConnected || requestGeneration !== generation) return;
        output.replaceChildren();
        const grid = node('div', undefined, 'quoth-recall'); output.append(grid);
        const claims = section(grid, 'Memories · ' + out.claims.length, 'quoth-card');
        if (!out.claims.length) claims.append(node('p', 'No accepted memory would be recalled.', 'quoth-meta'));
        const list = node('ol', undefined, 'quoth-slips'); claims.append(list);
        for (const c of out.claims) {
          const li = node('li', undefined, 'quoth-slip'); list.append(li);
          const body = node('div', undefined, 'quoth-slip-body'); li.append(body);
          body.append(node('p', c.text, 'quoth-claim-text'));
          const meta = node('p', undefined, 'quoth-meta quoth-slip-meta'); body.append(meta);
          meta.append(node('span', c.match === 'cosine' ? 'meaning match, cosine ' + Number(c.cosine).toFixed(2) : 'word match'));
          if (c.match === 'cosine') meter(meta, Number(c.cosine), 1, 'cosine ' + Number(c.cosine).toFixed(2));
          meta.append(node('span', c.kind.replace('_', ' ')), node('span', 'from ' + who(c)));
          memoryLink(c.id, meta);
        }
        const evidence = section(grid, 'Vault evidence · ' + out.evidence.length, 'quoth-card');
        evidence.append(node('p', 'Status: ' + out.evidence_status, 'quoth-meta'));
        const notes = node('ol', undefined, 'quoth-plainlist'); evidence.append(notes);
        for (const e of out.evidence) notes.append(node('li', e.locator + (e.note_date ? ' · written ' + e.note_date : '')));
        const episodes = section(grid, 'Earlier, the assistant said · ' + out.episodes.length, 'quoth-card');
        episodes.append(node('p', 'Status: ' + out.episodes_status, 'quoth-meta'));
        const said = node('ol', undefined, 'quoth-plainlist'); episodes.append(said);
        for (const e of out.episodes) said.append(node('li', e.locator + ' · ' + String(e.timestamp).slice(0, 10) + ' · cosine ' + Number(e.score).toFixed(2)));
        const block = node('details', undefined, 'quoth-raw'); block.open = true;
        block.append(node('summary', 'Exact block injected into the turn'));
        block.append(node('pre', out.text || '(nothing would be injected)', 'quoth-pre')); output.append(block);
      } catch (error) { if (output.isConnected) { output.replaceChildren(); failure(output, error, 'Recall unavailable; nothing inferred.'); } }
    };
    text.addEventListener('keydown', event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); run(); } });
    button('Show what the assistant would recall', run, controls, 'quoth-primary');
    button('Clear', () => { text.value = ''; output.replaceChildren(); }, controls);
    controls.append(node('span', '⌘/Ctrl + Enter runs it', 'quoth-hint'));
  }
  async function assistantSaid(pane) {
    pageHead(pane, 'What the assistant said', 'Search the assistant\'s own earlier replies. Assistant-origin: not your words, not vault evidence, not accepted memory.');
    const form = node('div', undefined, 'quoth-ask quoth-ask-row'); pane.append(form);
    const label = node('label', 'Search', 'quoth-field quoth-grow');
    const query = node('input'); query.type = 'search'; query.maxLength = 2048; query.dataset.private = 'true';
    query.placeholder = 'Stays in page memory';
    label.append(query); form.append(label);
    const k = select(form, 'Results', '10', [['5', '5'], ['10', '10'], ['20', '20']], () => {});
    const controls = node('div', undefined, 'quoth-controls'); form.append(controls);
    const output = node('div', undefined, 'quoth-results'); output.setAttribute('aria-live', 'polite'); pane.append(output);
    const run = async () => {
      output.replaceChildren();
      if (!query.value.trim()) { message(output, 'Enter something to search for.'); return; }
      const requestGeneration = generation;
      output.append(skeleton());
      try {
        const out = await api('/memory/episodes', 'POST', {query: query.value, k: Number(k.value)});
        if (!output.isConnected || requestGeneration !== generation) return;
        output.replaceChildren();
        if (!out.hits.length) { empty(output, 'Nothing found.', '“Darkness there, and nothing more.”'); return; }
        const list = node('ol', undefined, 'quoth-said'); output.append(list);
        for (const hit of out.hits) {
          const li = node('li'); list.append(li);
          const fig = node('figure', undefined, 'quoth-quotation quoth-quotation-small'); li.append(fig);
          fig.append(node('blockquote', hit.text));
          const cap = node('figcaption');
          cap.append(node('span', 'Assistant · ' + stamp(hit.timestamp) + ' · cosine ' + Number(hit.score).toFixed(2)), node('cite', hit.locator, 'quoth-locator'));
          fig.append(cap);
        }
      } catch (error) { if (output.isConnected) { output.replaceChildren(); failure(output, error, 'Search unavailable; nothing inferred.'); } }
    };
    query.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); run(); } });
    button('Search', run, controls, 'quoth-primary');
  }

  // ------------------------------------------------------------ refresh history

  const OUTCOME_TONE = {SWITCH: 'ok', SKIP: 'plain', HOLD: 'wait', INCOMPLETE: 'wait', BUSY: 'wait', FAIL: 'never'};
  async function refreshHistory(pane) {
    pageHead(pane, 'Refresh history', 'Each night the vault index is rebuilt and checked before it may serve. Outcomes, timings and reasons only: no note text.');
    const out = await api('/memory/refresh');
    if (!pane.isConnected) return;
    const hb = out.heartbeat;
    if (!hb) message(pane, 'Heartbeat not configured.');
    else if (hb.state !== 'ok') message(pane, 'Heartbeat ' + hb.state + '.');
    else {
      message(pane, 'Heartbeat ' + hb.status.toUpperCase() + (hb.reasons.length ? ': ' + hb.reasons.join(', ') : ''), hb.status === 'green' ? 'ok' : 'unavailable');
      const dl = node('dl', undefined, 'quoth-fields quoth-fields-wide'); pane.append(dl);
      field(dl, 'Checked', stamp(hb.checked_at) + ' · ' + ago(hb.checked_at));
      field(dl, 'Fresh as of', hb.fresh_as_of ? stamp(hb.fresh_as_of) + ' (' + hb.fresh_age_hours + ' h ago, limit ' + hb.max_fresh_age_hours + ' h)' : null);
      if (hb.serving) field(dl, 'Serving snapshot frozen', stamp(hb.serving.frozen_at) + ' (' + hb.serving.age_hours + ' h)');
      if (hb.last_run) field(dl, 'Last run', hb.last_run.outcome + ' · ' + stamp(hb.last_run.recorded_at) + (hb.last_run.reasons.length ? ' · ' + hb.last_run.reasons.join(', ') : ''));
    }
    const nights = section(pane, 'Nightly receipts');
    if (out.receipts_state !== 'ok') { message(nights, 'Receipts ' + out.receipts_state + '.'); return; }
    if (!out.receipts.length) { empty(nights, 'No nightly receipts yet.'); return; }
    strip(nights, out.receipts);
    table(nights, ['Recorded', 'Outcome', 'Gate', 'Seconds', 'Reasons', 'Gold drift'], out.receipts.map(r => [
      stamp(r.recorded_at), chip(r.outcome + (r.rollback ? ' · ' + r.rollback : ''), OUTCOME_TONE[r.outcome]), r.gate_mode || '—',
      r.total_seconds == null ? '—' : r.total_seconds + ' (' + Object.entries(r.seconds).map(([k, v]) => k + ' ' + Math.round(v)).join(', ') + ')',
      r.reasons.join(', ') || '—',
      r.gold_drift ? 'renamed ' + r.gold_drift.renamed + ' · stale ' + r.gold_drift.stale : '—']), 'Newest first', 'quoth-receipts');
  }
  // One mark per night, oldest left: the shape of the last weeks at a glance, summarised for screen readers.
  function strip(parent, receipts) {
    const nights = [...receipts].reverse();
    const counts = {};
    for (const r of nights) counts[r.outcome] = (counts[r.outcome] || 0) + 1;
    const wrap = node('figure', undefined, 'quoth-nights'); parent.append(wrap);
    const row = node('div', undefined, 'quoth-nights-row');
    row.setAttribute('role', 'img');
    row.setAttribute('aria-label', 'Last ' + nights.length + ' runs: ' + Object.entries(counts).map(([k, v]) => v + ' ' + k).join(', '));
    for (const r of nights) { const mark = node('span'); mark.dataset.tone = OUTCOME_TONE[r.outcome] || 'plain'; mark.title = stamp(r.recorded_at) + ' · ' + r.outcome; row.append(mark); }
    wrap.append(row);
    const cap = node('figcaption', undefined, 'quoth-legend-inline');
    for (const [k, v] of Object.entries(counts)) { const key = node('span', undefined, 'quoth-key'); key.dataset.tone = OUTCOME_TONE[k] || 'plain'; key.append(node('i'), node('span', k + ' ' + v)); cap.append(key); }
    wrap.append(cap);
  }

  // ------------------------------------------------------------ retrieval lab

  async function pipeline(pane) {
    heading(pane, 'Pipeline');
    message(pane, 'Production retrieval remains dense → evidence bundle. Lab does not alter MCP.');
    const {spec, hash, fidelity} = await api('/lab/pipelines/production');
    const dl = node('dl', undefined, 'quoth-fields'); pane.append(dl);
    field(dl, 'Specification hash', hash); field(dl, 'Snapshot', spec.snapshot.id);
    field(dl, 'Registry', spec.registry);
    field(dl, 'Fidelity receipt', fidelity.state === 'unavailable' ? 'Unavailable: ' + fidelity.reason : fidelity.state);
    for (const stage of ['Vault input', 'Scope / authority', 'Chunk metadata', 'BGE-M3 vectors',
                          'Frozen snapshot', 'MCP evidence bundle', 'Caller answer use']) {
      const details = node('details');
      details.append(node('summary', stage));
      details.append(node('p', stage === 'Caller answer use'
        ? 'Application by the caller is not observed by this lab.'
        : 'No live stage observation supplied; this is a declared architecture stage.'));
      pane.append(details);
    }
  }
  async function snapshots(pane) {
    heading(pane, 'Snapshots & freshness');
    const status = await api('/status');
    const dl = node('dl', undefined, 'quoth-fields'); pane.append(dl);
    field(dl, 'Snapshot', status.snapshot?.id);
    field(dl, 'DB SHA-256', status.snapshot?.db_sha256);
    field(dl, 'Snapshot validation', status.state);
    field(dl, 'Effective runtime', status.effective_runtime.state);
    const inventory = await api('/freshness');
    field(dl, 'Freshness receipt', inventory.state);
    field(dl, 'Observed', inventory.observed_at);
    if (inventory.data) {
      for (const key of ['added', 'removed', 'changed']) field(dl, key, inventory.data[key]);
    }
    message(pane, 'Live/rollback activation remains unknown until it is observed independently. No refresh from this view.');
  }
  async function playground(pane) {
    heading(pane, 'Playground · constructor');
    message(pane, 'Query stays in page and sidecar RAM for at most 10 minutes. No caller rationale, answer or temporal verdict is inferred.');
    const data = await api('/lab/pipelines/production');
    const registry = await api('/lab/operators');
    const spec = data.spec;
    let draft = {...structuredClone(spec), name: 'draft-' + spec.name, parent: data.hash};
    let checked = null;
    let lastEval = null;
    const dl = node('dl', undefined, 'quoth-fields'); pane.append(dl);
    field(dl, 'Immutable production spec', data.hash);
    field(dl, 'Snapshot', spec.snapshot.id);
    const workspace = node('section', undefined, 'quoth-builder');
    workspace.setAttribute('aria-label', 'Visual pipeline constructor'); pane.append(workspace);
    const palette = node('section', undefined, 'quoth-palette'); palette.append(node('h3', 'Operator palette'));
    const canvas = node('section', undefined, 'quoth-canvas'); canvas.append(node('h3', 'Connected pipeline'));
    const linear = node('section', undefined, 'quoth-linear'); linear.append(node('h3', 'Linear builder'));
    const inspector = node('section', undefined, 'quoth-inspector'); inspector.append(node('h3', 'Node inspector'));
    workspace.append(palette, canvas, inspector, linear);
    let selected = draft.nodes[0].id;

    function port(op) { return op.input === 'CandidateList[]' ? 'lists' : op.input === 'CandidateList' ? 'in' : 'query'; }
    function sources(step) { return Object.values(step.in).flat(); }
    function compatible(op, index) {
      const candidates = [{id: '$query', type: 'Query'}, ...draft.nodes.slice(0, index).map(step =>
        ({id: step.id, type: registry.operators.find(item => item.name === step.op)?.output}))];
      return candidates.filter(item => item.type === (op.input === 'CandidateList[]' ? 'CandidateList' : op.input));
    }
    function defaultConfig(op) {
      if (op.name === 'query_guard') return {max_chars: 2048};
      if (op.name === 'filter') return {path_prefix: ''};
      if (op.name === 'fuse') return {k: 10, rrf_k: 60};
      if (op.name === 'dedupe_diversity') return {k: 10, per_source: 2};
      return op.name === 'dense_retrieve' ? {model: draft.nodes.find(s => s.op === 'dense_retrieve')?.config.model || 'bge-m3', k: 10} : {k: 10};
    }
    function changed() { checked = null; lastEval = null; validation.replaceChildren(); editor.value = JSON.stringify(draft, null, 2); draw(); }
    function add(op, index) {
      const choices = compatible(op, index);
      if (!op.available || op.name === 'query_guard' || op.name === 'evidence_bundle' ||
          (op.input === 'CandidateList[]' ? choices.length < 2 : !choices.length) ||
          index === 0 || index >= draft.nodes.length || draft.nodes.length >= 24) return;
      let id = op.name.replace(/[^A-Za-z0-9_]/g, '_'), suffix = 2;
      while (draft.nodes.some(step => step.id === id)) id = op.name + '_' + suffix++;
      const input = port(op);
      const source = input === 'lists' ? choices.slice(-2).map(choice => choice.id) : choices.at(-1).id;
      draft.nodes.splice(index, 0, {id, op: op.name, op_version: '1',
        in: {[input]: source},
        config: defaultConfig(op)});
      const successor = draft.nodes[index + 1];
      const successorOp = registry.operators.find(item => item.name === successor.op);
      if (!Array.isArray(source) && successorOp?.input === op.output &&
          Object.values(successor.in).length === 1 && Object.values(successor.in)[0] === source) {
        successor.in[Object.keys(successor.in)[0]] = id;
      }
      selected = id; changed();
    }
    function remove(index) {
      if (index === 0 || index === draft.nodes.length - 1) return;
      const removed = draft.nodes.splice(index, 1)[0];
      for (const step of draft.nodes) {
        for (const [key, value] of Object.entries(step.in)) {
          if (Array.isArray(value)) step.in[key] = value.map(id => id === removed.id ? sources(removed)[0] : id);
          else if (value === removed.id) step.in[key] = sources(removed)[0];
        }
      }
      selected = draft.nodes[Math.min(index, draft.nodes.length - 1)].id; changed();
    }
    function reorder(index, direction) {
      const next = index + direction;
      if (index === 0 || index === draft.nodes.length - 1 || next <= 0 || next >= draft.nodes.length - 1) return;
      [draft.nodes[index], draft.nodes[next]] = [draft.nodes[next], draft.nodes[index]];
      changed();
    }
    function inspect(step, index) {
      inspector.replaceChildren(node('h3', 'Node inspector · ' + step.id));
      const op = registry.operators.find(item => item.name === step.op);
      inspector.append(node('p', op.name + ' · ' + op.status + ' · ' + (op.reason || 'Executable lab operator')));
      inspector.append(node('p', 'Input ' + op.input + ' → Output ' + op.output + ' · version ' + step.op_version));
      const inputs = node('fieldset'); inputs.append(node('legend', 'Typed input connections')); inspector.append(inputs);
      const choices = compatible(op, index);
      for (const [key, value] of Object.entries(step.in)) {
        const count = Array.isArray(value) ? value.length : 1;
        for (let i = 0; i < count; i++) {
          const label = node('label', key + (count > 1 ? ' ' + (i + 1) : '') + ' · ' +
            (op.input === 'CandidateList[]' ? 'CandidateList' : op.input));
          const pick = node('select');
          for (const choice of choices) {
            const option = node('option', choice.id + ' · ' + choice.type); option.value = choice.id;
            pick.append(option);
          }
          pick.value = Array.isArray(value) ? value[i] : value;
          pick.addEventListener('change', () => {
            if (Array.isArray(step.in[key])) step.in[key][i] = pick.value;
            else step.in[key] = pick.value;
            changed();
          });
          label.append(pick); inputs.append(label);
        }
      }
      for (const [key, value] of Object.entries(step.config)) {
        const label = node('label', 'Configure ' + key);
        const input = node('input'); input.value = String(value);
        if (typeof value === 'number') { input.type = 'number'; input.min = '1'; input.max = key === 'k' ? (step.op === 'evidence_bundle' ? '20' : '200') : key === 'per_source' ? '10' : '2048'; }
        else { input.type = 'text'; input.maxLength = key === 'path_prefix' ? 120 : 80; }
        input.addEventListener('change', () => { step.config[key] = typeof value === 'number' ? Number(input.value) : input.value; changed(); });
        label.append(input); inspector.append(label);
      }
      const trace = currentRun?.trace?.find(item => item.node === step.id);
      if (trace) {
        inspector.append(node('p', 'Intermediate result · ' + trace.n_in + ' in → ' + trace.n_out +
          ' out · dropped ' + trace.dropped_count + ' · ' + trace.ms + ' ms'));
        const list = node('ol'); inspector.append(list);
        for (const item of trace.items) list.append(node('li', 'Rank ' + item.rank + ' · chunk ' + item.chunk_id + ' · score ' + item.score));
      }
      if (index > 0 && index < draft.nodes.length - 1) button('Remove selected step', () => remove(index), inspector);
    }
    function draw() {
      palette.replaceChildren(node('h3', 'Operator palette'));
      canvas.replaceChildren(node('h3', 'Connected pipeline'));
      linear.replaceChildren(node('h3', 'Linear builder · keyboard and mobile'));
      for (const op of registry.operators) {
        const label = op.name + ' · ' + op.status + ' · ' + op.input + ' → ' + op.output +
          (op.reason ? ' · ' + op.reason : '');
        const control = button('Add ' + label, () => add(op, draft.nodes.length - 1), palette);
        control.disabled = !op.available || ['query_guard', 'evidence_bundle'].includes(op.name) ||
          compatible(op, draft.nodes.length - 1).length < (op.input === 'CandidateList[]' ? 2 : 1) || draft.nodes.length >= 24;
        if (!op.available) palette.append(node('p', op.name + ' unavailable: ' + op.reason));
      }
      const list = node('div'); list.setAttribute('role', 'tree'); list.setAttribute('aria-label', 'Pipeline typed graph'); canvas.append(list);
      draft.nodes.forEach((step, index) => {
        const op = registry.operators.find(item => item.name === step.op);
        const label = step.id + ' · ' + step.op + ' · ' + op.output + ' ◇ ← ' +
          sources(step).join(', ') + ' · ' + JSON.stringify(step.config);
        const item = button(label, () => { selected = step.id; inspect(step, index); }, list);
        item.setAttribute('role', 'treeitem'); item.setAttribute('aria-selected', String(selected === step.id));
        item.addEventListener('keydown', event => {
          if (event.key === 'ArrowRight' || event.key === 'ArrowDown' || event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
            event.preventDefault(); list.children[Math.max(0, Math.min(list.children.length - 1,
              index + (event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : -1)))]?.focus();
          } else if (event.key.toLowerCase() === 'a') { event.preventDefault(); palette.querySelector('button:not(:disabled)')?.focus(); }
        });
        const row = node('div', undefined, 'quoth-step'); linear.append(row);
        button('Inspect ' + step.id + ' · ' + step.op + ' · ' + sources(step).join(', ') + ' · ' +
          JSON.stringify(step.config), () => { selected = step.id; inspect(step, index); inspector.scrollIntoView({block: 'nearest'}); }, row);
        if (index > 0 && index < draft.nodes.length - 1) {
          button('Move ' + step.id + ' up', () => reorder(index, -1), row).disabled = index === 1;
          button('Move ' + step.id + ' down', () => reorder(index, 1), row).disabled = index === draft.nodes.length - 2;
          button('Remove ' + step.id, () => remove(index), row);
        }
        if (index > 0) {
          const picker = node('select'); picker.setAttribute('aria-label', 'Add step before ' + step.id);
          const prompt = node('option', 'Add compatible step before ' + step.id); prompt.value = ''; picker.append(prompt);
          for (const candidate of registry.operators.filter(candidate => candidate.available &&
            !['query_guard', 'evidence_bundle'].includes(candidate.name) &&
            compatible(candidate, index).length >= (candidate.input === 'CandidateList[]' ? 2 : 1))) {
            const option = node('option', candidate.name + ' · ' + candidate.input); option.value = candidate.name; picker.append(option);
          }
          picker.addEventListener('change', () => { const choice = registry.operators.find(op => op.name === picker.value); if (choice) add(choice, index); });
          row.append(picker);
        }
      });
      const focus = draft.nodes.findIndex(step => step.id === selected);
      inspect(draft.nodes[Math.max(0, focus)], Math.max(0, focus));
    }
    const advanced = node('details'); advanced.append(node('summary', 'Advanced JSON import/export · RAM only')); pane.append(advanced);
    const editLabel = node('label', 'Draft typed DAG JSON');
    const editor = node('textarea'); editor.rows = 12; editor.spellcheck = false;
    editor.value = JSON.stringify(draft, null, 2);
    editLabel.append(editor); advanced.append(editLabel);
    const validation = node('div'); validation.setAttribute('aria-live', 'polite'); pane.append(validation);
    async function validateDraft() {
      validation.replaceChildren(); checked = null;
      try {
        const parsed = JSON.parse(editor.value);
        const result = await api('/lab/pipelines/validate', 'POST', parsed);
        if (result.status !== 'ok') { message(validation, 'Invalid draft: ' + result.errors.join(', ')); return null; }
        draft = parsed; checked = result.hash; selected = draft.nodes[0].id; draw();
        message(validation, 'Validated immutable spec hash: ' + checked, 'ok');
        return parsed;
      } catch (_) { message(validation, 'Invalid JSON or validation API unavailable.'); return null; }
    }
    editor.addEventListener('input', () => { checked = null; lastEval = null; validation.replaceChildren(); });
    button('Validate draft', validateDraft, pane);
    draw();
    const label = node('label', 'Private query (RAM only)');
    const query = node('textarea'); query.maxLength = 2048; query.rows = 3; query.dataset.private = 'true';
    label.append(query); pane.append(label);
    const kLabel = node('label', 'Evidence limit (1–20)');
    const k = node('input'); k.type = 'number'; k.min = '1'; k.max = '20'; k.value = '10';
    kLabel.append(k); pane.append(kLabel);
    const controls = node('div', undefined, 'quoth-controls'); pane.append(controls);
    const output = node('div'); output.setAttribute('aria-live', 'polite'); pane.append(output);
    button('Run trace', async () => {
      output.replaceChildren();
      if (!query.value.trim() || !Number.isInteger(Number(k.value)) || Number(k.value) < 1 || Number(k.value) > 20) {
        message(output, 'Enter a query and a limit from 1 to 20.'); return;
      }
      const submitted = query.value;
      clearSensitive(); const requestGeneration = generation; privateQuery = submitted;
      try {
        const selected = await validateDraft();
        if (!selected) { privateQuery = ''; return; }
        const run = await api('/lab/runs/trace', 'POST', {query: privateQuery, pipeline_hash: checked,
          snapshot_id: spec.snapshot.id, k: Number(k.value), query_class: 'unknown', reason_code: 'unknown', spec: selected});
        if (!dialog || !output.isConnected || requestGeneration !== generation) {
          api('/lab/runs/' + run.run_id, 'DELETE').catch(() => {});
          return;
        }
        query.value = ''; privateQuery = '';
        currentRun = run;
        displayTrace(output, currentRun);
        draw();
      } catch (_) { if (requestGeneration === generation && output.isConnected) { privateQuery = ''; message(output, 'Trace unavailable. No result has been inferred.'); } }
    }, controls);
    button('Discard run', () => { clearSensitive(); output.replaceChildren(); message(output, 'Run discarded.'); }, controls);
    button('Compare k with production k=10', async () => {
      output.replaceChildren();
      if (!query.value.trim() || !Number.isInteger(Number(k.value)) || Number(k.value) < 1 || Number(k.value) > 20) {
        message(output, 'Enter a query and a limit from 1 to 20.'); return;
      }
      const submitted = query.value;
      clearSensitive(); const requestGeneration = generation; privateQuery = submitted;
      try {
        const selected = await validateDraft();
        if (!selected) { privateQuery = ''; return; }
        const result = await api('/lab/runs/compare', 'POST', {query: submitted, pipeline_hash: data.hash,
          snapshot_id: spec.snapshot.id, k_a: 10, k_b: Number(k.value), spec_b: selected});
        if (!dialog || !output.isConnected || requestGeneration !== generation) {
          for (const item of [result.a, result.b]) api('/lab/runs/' + item.run_id, 'DELETE').catch(() => {});
          return;
        }
        query.value = ''; privateQuery = '';
        currentRun = {comparisons: [result.a, result.b]};
        message(output, 'Observed overlap: ' + result.diff.overlap + ' · added: ' + result.diff.added.length +
          ' · removed: ' + result.diff.removed.length + '. No quality verdict.', 'ok');
        for (const [label, value] of [['Production k=10', result.a], ['Draft', result.b]]) {
          const details = node('details'); details.append(node('summary', label)); output.append(details);
          displayTrace(details, value);
        }
      } catch (_) { if (requestGeneration === generation && output.isConnected) { privateQuery = ''; message(output, 'Comparison unavailable; no result inferred.'); } }
    }, controls);
    button('Run fixed four-suite eval', async () => {
      output.replaceChildren(); lastEval = null;
      const selected = await validateDraft(); if (!selected) return;
      const hash = checked, requestGeneration = generation;
      try {
        const job = await api('/lab/evals', 'POST', {spec: selected,
          suites: ['ordinary', 'protected_facets', 'e1_temporal', 'abstention']});
        message(output, 'Evaluation queued; polling on this page. Protected gates cannot be edited.', 'ok');
        const cancel = button('Cancel evaluation', async () => {
          await api('/lab/evals/' + job.eval_id, 'DELETE').catch(() => {});
          output.replaceChildren(); message(output, 'Evaluation cancelled; no gate verdict.');
        }, output);
        for (let attempt = 0; attempt < 120 && requestGeneration === generation && output.isConnected; attempt++) {
          await new Promise(resolve => setTimeout(resolve, 2000));
          if (!output.isConnected || requestGeneration !== generation) break;
          const state = await api('/lab/evals/' + job.eval_id);
          if (state.state === 'done') {
            output.replaceChildren(); lastEval = {id: job.eval_id, hash, result: state.result};
            message(output, state.result.eligible ? 'All fixed gates passed; export may be requested.' :
              'Protected evaluation rejected candidate. No export or serving change.', state.result.eligible ? 'ok' : 'unavailable');
            const dl = node('dl', undefined, 'quoth-fields'); output.append(dl);
            for (const [gate, passed] of Object.entries(state.result.gates)) field(dl, gate, passed ? 'pass' : 'fail');
            break;
          }
          if (state.state === 'failed') { cancel.remove(); message(output, 'Evaluation unavailable; no gate verdict.', 'unavailable'); break; }
        }
      } catch (_) { message(output, 'Evaluation unavailable; no gate verdict.', 'unavailable'); }
    }, controls);
    button('Export candidate (all gates required)', async () => {
      const selected = await validateDraft();
      if (!selected || !lastEval || lastEval.hash !== checked || !lastEval.result.eligible) {
        message(output, 'Export denied: a matching complete four-suite passing eval is required.'); return;
      }
      try {
        const result = await api('/lab/candidates', 'POST', {spec: selected, eval_id: lastEval.id});
        message(output, 'Local candidate exported, awaiting pull: ' + result.candidate_hash, 'ok');
      } catch (_) { message(output, 'Export denied or lab state unavailable. No promotion occurred.'); }
    }, controls);
    const limits = node('p', 'Visual edits validate before execution. Compare runs embeddings separately; latency is not comparable. Export is local-only; no promote control exists.');
    limits.id = 'quoth-limitations'; pane.append(limits);
  }
  function displayTrace(pane, record) {
    const dl = node('dl', undefined, 'quoth-fields'); pane.append(dl);
    field(dl, 'Pipeline', record.pipeline_hash); field(dl, 'Snapshot', record.snapshot_id);
    field(dl, 'Temporal', record.temporal.qualification);
    field(dl, 'Answer use', record.output.answer_use_applied);
    for (const step of record.trace) {
      const details = node('details'); pane.append(details);
      details.append(node('summary', step.node + ' · ' + step.n_in + ' → ' + step.n_out +
        ' · ' + step.ms + ' ms · dropped ' + step.dropped_count));
      const list = node('ol'); details.append(list);
      for (const item of step.items) {
        const li = node('li', 'Rank ' + item.rank + ' · chunk ' + item.chunk_id +
          ' · ' + item.score_kind + ' ' + item.score + ' · ' + (item.selected ? 'selected' : 'not selected'));
        list.append(li);
        const reveal = node('div', undefined, 'quoth-reveal'); li.append(reveal);
        button('Reveal excerpt', async () => {
          reveal.replaceChildren();
          try {
            const result = await api('/lab/runs/' + record.run_id + '/reveal', 'POST',
              {capability: record.reveal_capability, node: step.node, chunk_ids: [item.chunk_id], include_query: false});
            if (!dialog || !reveal.isConnected || (currentRun !== record && !currentRun?.comparisons?.includes(record))) return;
            for (const entry of result.items) reveal.append(node('p', entry.locator + '\n' + entry.text));
            button('Re-hide', () => reveal.replaceChildren(), reveal);
          } catch (_) { message(reveal, 'Reveal unavailable or run expired.'); }
        }, li);
      }
    }
  }
  async function quality(pane) {
    heading(pane, 'Quality & protected gates');
    const quality = await api('/quality');
    message(pane, 'Receipt: ' + quality.state + '. E1 temporal and Q2 diversity operators were rejected as serving; no candidate verdict is inferred.');
    if (quality.data) {
      const dl = node('dl', undefined, 'quoth-fields'); pane.append(dl);
      field(dl, 'Suite', quality.data.suite_id); field(dl, 'Snapshot digest', quality.data.snapshot_sha256);
      for (const [metric, value] of Object.entries(quality.data.metrics)) field(dl, metric, value);
      for (const [gate, value] of Object.entries(quality.data.gate_verdicts)) field(dl, gate, value);
    }
    message(pane, 'Playground evals use fixed suites and protected gates; export does not promote a candidate.');
  }

  // ------------------------------------------------------------ frame

  function navigation(parent, label, items, cls) {
    const nav = node('nav', undefined, 'quoth-nav' + (cls ? ' ' + cls : '')); nav.setAttribute('aria-label', label);
    const list = node('ul'); nav.append(list);
    for (const [view, glyph] of items) {
      const li = node('li'); list.append(li);
      const b = glyph ? iconButton(glyph, view, () => go(view), li) : button(view, () => go(view), li);
      b.dataset.view = view;
    }
    parent.append(nav);
    return nav;
  }
  // Arrow keys move between views in the sidebar; Enter or Space opens one.
  function navKeys(event) {
    const items = [...dialog.querySelectorAll('.quoth-nav button')];
    const index = items.indexOf(document.activeElement);
    if (index < 0) return;
    const next = {ArrowDown: index + 1, ArrowRight: index + 1, ArrowUp: index - 1, ArrowLeft: index - 1, Home: 0, End: items.length - 1}[event.key];
    if (next === undefined) return;
    event.preventDefault();
    items[(next + items.length) % items.length].focus();
  }
  function open({restoreFocus}) {
    if (dialog) { dialog.querySelector('h1')?.focus(); return; }
    restore = restoreFocus;
    dialog = node('div', undefined, 'quoth-overlay');
    dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-labelledby', 'quoth-title');
    const panel = node('div', undefined, 'quoth-panel'); dialog.append(panel);
    const side = node('div', undefined, 'quoth-side'); panel.append(side);
    const top = node('div', undefined, 'quoth-brand'); side.append(top);
    const title = node('h1', undefined); title.id = 'quoth-title'; title.tabIndex = -1;
    title.append(raven(), node('span', 'Quoth', 'quoth-wordmark'), node('span', ' · memory', 'quoth-sr'));
    top.append(title, node('p', 'Memory, quoted with its source.', 'quoth-tagline'));
    const rail = node('div', undefined, 'quoth-rail'); side.append(rail);
    rail.addEventListener('scroll', railEdges, {passive: true});
    navigation(rail, 'Memory views', memoryNav);
    const lab = node('div', undefined, 'quoth-lab'); rail.append(lab);
    const labTitle = node('p', undefined, 'quoth-lab-title'); labTitle.append(icon('flask'), node('span', 'Retrieval lab')); labTitle.setAttribute('aria-hidden', 'true'); lab.append(labTitle);
    navigation(lab, 'Retrieval lab', views.map(v => [v]), 'quoth-nav-lab');
    side.append(node('p', 'Read-only. Decisions are made in your chat with the assistant, in your own words.', 'quoth-colophon'));
    iconButton('close', 'Close', close, panel, 'quoth-close', true);
    const body = node('main', undefined, 'quoth-content'); body.tabIndex = -1; panel.append(body);
    const live = node('p', undefined, 'quoth-live quoth-sr'); live.setAttribute('aria-live', 'polite'); panel.append(live);
    dialog.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); close(); return; }
      navKeys(event);
      if (event.key === 'Tab') {
        const focusable = [...dialog.querySelectorAll('button:not([disabled]),textarea,input,select,summary,[tabindex="0"]')]
          .filter(el => el.getClientRects().length);
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    });
    applyScheme(); document.body.append(dialog); title.focus(); renderFrame(); railEdges();
    window.addEventListener?.('resize', railEdges);
    poll = setInterval(() => { if (!document.hidden && dialog && autoRefresh.has(active) && !memoryState.detail) renderFrame(); }, 60000);
  }
  // The WebUI's mode: `.dark` on :root means dark; --bg defined without it means light. With
  // neither (another host), no data-scheme is set and the CSS falls back to prefers-color-scheme.
  function scheme() {
    const root = document.documentElement;
    if (!root?.classList) return '';
    if (root.classList.contains('dark')) return 'dark';
    return window.getComputedStyle?.(root).getPropertyValue('--bg').trim() ? 'light' : '';
  }
  function applyScheme() {
    const mode = scheme();
    for (const el of [dialog, launcher]) {
      if (!el) continue;
      if (mode) el.dataset.scheme = mode; else delete el.dataset.scheme;
    }
  }
  handle.settings?.registerConfigure?.(({restoreFocus}) => open({restoreFocus}));
  const launcher = node('button', undefined, 'quoth-launcher'); launcher.type = 'button';
  launcher.append(raven(), node('span', 'Quoth'));
  launcher.setAttribute('aria-label', 'Open Quoth memory');
  launcher.addEventListener('click', () => open({restoreFocus: () => launcher.focus()}));
  document.body.append(launcher);
  applyScheme();
  // Follow the WebUI's theme toggle and skins while the page is open.
  if (window.MutationObserver && document.documentElement) {
    new window.MutationObserver(applyScheme).observe(document.documentElement, {attributes: true, attributeFilter: ['class', 'data-skin', 'style']});
  }
})();
