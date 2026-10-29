/* Minimal synthetic DOM harness. No browser, network or storage. Run: node tests/playground-dom.cjs */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
// textContent aggregates descendants like the DOM; strings appended become text nodes.
class Element {
  constructor(tag, text = '') {
    this.tag = tag; this.children = []; this.listeners = {}; this._text = String(text);
    this.dataset = {}; this.attrs = {}; this.style = {}; this.value = ''; this.disabled = false; this.isConnected = true; this.className = '';
  }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...children) {
    for (let child of children) {
      if (typeof child === 'string') child = new Element('#text', child);
      this.children.push(child); child.parent = this;
    }
  }
  replaceChildren(...children) { this.children = []; this._text = ''; this.append(...children); }
  setAttribute(key, value) { this.attrs[key] = value; if (key === 'class') this.className = value; }
  removeAttribute(key) { delete this.attrs[key]; }
  addEventListener(key, fn) { (this.listeners[key] ||= []).push(fn); }
  click() { if (!this.disabled) for (const fn of this.listeners.click || []) fn(); }
  change(value = this.value) { this.value = value; for (const fn of this.listeners.change || []) fn(); }
  key(key) { for (const fn of this.listeners.keydown || []) fn({key, preventDefault() {}}); }
  focus() { this.focused = true; }
  scrollIntoView() {}
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(x => x !== this); }
  getClientRects() { return [1]; }
  matches(simple) {
    const m = /^([a-z0-9#]*)((?:\.[\w-]+)*)((?:\[[\w-]+\])*)(:not\(:disabled\))?$/.exec(simple);
    if (!m) throw Error('harness selector unsupported: ' + simple);
    const classes = this.className.split(' ');
    return (!m[1] || this.tag === m[1]) && m[2].split('.').filter(Boolean).every(c => classes.includes(c)) &&
      (m[3].match(/[\w-]+/g) || []).every(a => a.startsWith('data-') ? this.dataset[a.slice(5)] !== undefined : this.attrs[a] !== undefined) &&
      (!m[4] || !this.disabled);
  }
  querySelectorAll(selector) {
    const descendants = this.children.flatMap(child => [child, ...child.querySelectorAll('*')]);
    if (selector === '*') return descendants;
    const parts = selector.trim().split(/\s+/);
    return descendants.filter(x => {
      if (!x.matches(parts.at(-1))) return false;
      let up = x.parent;
      for (const part of parts.slice(0, -1).reverse()) {
        while (up && up !== this && !up.matches(part)) up = up.parent;
        if (!up || up === this) return false;
        up = up.parent;
      }
      return true;
    });
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0]; }
}
const body = new Element('body');
// The WebUI's root: `.dark` toggles dark mode; --bg is defined in both modes.
const rootClasses = new Set(['dark']); const rootVars = {'--bg': '#0D0D1A'};
const documentElement = {classList: {contains: c => rootClasses.has(c)}};
const document = {body, documentElement, hidden: false, createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag), activeElement: null};
const ops = [
  ['query_guard', 'Query', 'Query', true], ['dense_retrieve', 'Query', 'CandidateList', true],
  ['lexical_bm25', 'Query', 'CandidateList', true], ['filter', 'CandidateList', 'CandidateList', true],
  ['fuse', 'CandidateList[]', 'CandidateList', true], ['evidence_bundle', 'CandidateList', 'EvidenceBundle', true],
  ['llm_answer', 'EvidenceBundle', 'Decision', false],
].map(([name, input, output, available]) => ({name, input, output, available,
  status: available ? 'lab' : 'unavailable', reason: available ? null : 'External LLM disabled'}));
const spec = {schema: 'quoth-pipeline/v1', name: 'production', parent: null,
  registry: 'synthetic', snapshot: {id: 'synthetic', db_sha256: 'synthetic'},
  nodes: [{id:'guard',op:'query_guard',op_version:'1',in:{query:'$query'},config:{max_chars:2048}},
    {id:'dense',op:'dense_retrieve',op_version:'1',in:{query:'guard'},config:{model:'bge-m3',k:10}},
    {id:'out',op:'evidence_bundle',op_version:'1',in:{in:'dense'},config:{k:10}}],
  output:'out',budget:{trace_ms:5000,max_candidates_per_edge:200}};
const calls = [];
// Synthetic memory fixtures: invented text only.
const claim = (id, extra = {}) => ({id, text: 'Synthetic memory ' + id + ' about the blue shed', kind: 'environment',
  origin: 'assistant_proposal', authority: 'assistant', review_status: 'accepted', current: true, recorded_at: '2026-10-01T10:00:00.000Z',
  quote_verified: 0, supersedes: null, superseded_by: null, valid_to: null, use_count: 0, auto_accepted: 0, suggestion: null, ...extra});
const memory = {
  claims: {total: 3, offset: 0, limit: 50, ledger_totals: {accepted: 2, pending: 1},
    filters: {status: ['pending', 'accepted', 'rejected', 'expired', 'current'], kind: ['environment', 'preference'], authority: ['owner', 'assistant', 'imported']},
    items: [claim(3, {review_status: 'pending', current: true, suggestion: 'accept', kind: 'preference', authority: 'imported'}),
            claim(2, {supersedes: 1}), claim(1, {superseded_by: 2, current: false, valid_to: '2026-10-02T00:00:00.000Z'})]},
  detail: id => ({claim: {...claim(id), proposer: 'assistant', evidence_locator: 'hermes:tg1:1', evidence_quote: null, reason: 'synthetic',
      valid_from: '2026-10-01T10:00:00.000Z', reviewed_at: '2026-10-01T11:00:00.000Z', review_note: 'approved via hermes:tg1:9',
      ...(id === 1 ? {superseded_by: 2, current: false} : {}), ...(id === 3 ? {review_status: 'pending'} : {})},
    suggestion: id === 3 ? {action: 'duplicate', duplicate_of: 2, suggested_kind: null, rewrite: null, note: 'same shed', suggested_by: 'assistant', suggested_at: '2026-10-03'} : null,
    events: [{at: '2026-10-01T10:00:00.000Z', action: 'proposed', actor: 'assistant', detail: {kind: 'environment'}}],
    chain: id === 3 ? [] : [{id: 1, text: 'Synthetic memory 1', review_status: 'accepted', current: false, ...(id === 1 ? {self: true} : {})},
                            {id: 2, text: 'Synthetic memory 2', review_status: 'accepted', current: true, ...(id === 2 ? {self: true} : {})}]}),
  // No `plan`/`topics`: an older sidecar's queue, so the page derives them itself.
  queue: {pending: 3, oldest_pending_age_days: 2.5, status: 'green', reasons: [], truncated: false,
    counts: {accept: 2, duplicate: 1, covered: 0, reject: 0, stale: 0, keep: 0, none: 0},
    groups: {accept: [claim(4, {review_status: 'pending', position: 1, suggestion: {action: 'accept', topic: 'Home', rewrite: 'Synthetic rewrite 4'}}),
                      claim(5, {review_status: 'pending', position: 1, suggestion: {action: 'accept', topic: 'Work'}})],
      duplicate: [claim(3, {review_status: 'pending', position: 2, suggestion: {action: 'duplicate', duplicate_of: 2, note: 'same shed', topic: 'Home'},
        duplicate_target: {id: 2, text: 'Synthetic memory 2', review_status: 'accepted', kept: true, kept_id: 2}})],
      covered: [], reject: [], stale: [], keep: [], none: []}},
  // The sidecar's own status is red because of the queue alone; the Health card must not say so.
  sidecar: {version: '0.14.0', status: 'red', quote_verification: true, ledger: {status: 'red', reasons: ['pending_queue_large'], pending: 2, accepted_current: 2},
    evidence: {status: 'disabled'}, episodes: {status: 'green', reasons: [], messages: 3, pending: 0, last_tick_at: '2026-10-01T10:00:00+00:00'},
    owner_episodes: {status: 'disabled'}},
  core: {source: 'published', file: 'core-abc.md', text: 'Core memory\n## Environment\n- Synthetic memory 2', live_differs: true,
    live: {claim_ids: [2], dropped_ids: [], chars: 40, limit: 3200, budget: 4000},
    versions: [{id: 1, sha256: 'a'.repeat(64), chars: 40, claims: 1, budget: 4000, compiled_at: '2026-10-01T12:00:00.000Z'}]},
  recall: {text: '<quoth-recall>\n- #2 environment: Synthetic memory 2\n</quoth-recall>', evidence_status: 'ok', episodes_status: 'ok',
    claims: [{id: 2, kind: 'environment', authority: 'assistant', text: 'Synthetic memory 2', cosine: 0.71, match: 'cosine'},
             {id: 1, kind: 'environment', authority: 'owner', text: 'Synthetic memory 1', cosine: null, match: 'words'}],
    evidence: [{locator: 'PROJECTS/Garden.md:1-3', note_date: '2026-01-02'}],
    episodes: [{locator: 'hermes:tg1:2', timestamp: '2026-10-01T10:00:00+00:00', score: 0.66}]},
  episodes: {answer_use: 'assistant-origin', hits: [{locator: 'hermes:tg1:2', timestamp: '2026-10-01T10:00:00+00:00', score: 0.66, text: 'Synthetic reply about paddles'}]},
  refresh: {receipts_state: 'ok', heartbeat: {state: 'ok', status: 'red', reasons: ['last_run_hold'], checked_at: '2026-10-27T02:06:00Z',
      fresh_as_of: '2026-10-26T02:05:00Z', fresh_age_hours: 24, max_fresh_age_hours: 36, max_run_age_hours: 26,
      serving: {frozen_at: '2026-10-26T02:00:00Z', age_hours: 24.1}, last_run: {outcome: 'HOLD', reasons: ['x'], recorded_at: '2026-10-27T02:05:00Z'}},
    receipts: [{receipt_id: 'r2', recorded_at: '2026-10-27T02:05:00Z', outcome: 'HOLD', gate_mode: 'code', seconds: {build: 10}, total_seconds: 10,
                reasons: ['private_hit@10_regressed'], gold_drift: {renamed: 2, stale: 1}, rollback: null}]},
};
let recallFails = true;
const handle = {settings: {registerConfigure(fn) { this.open = fn; }}};
let themeObserver;
const window = {hermesExt: {register: () => handle}, getComputedStyle: () => ({getPropertyValue: name => rootVars[name] || ''}),
  MutationObserver: class { constructor(fn) { themeObserver = fn; } observe() {} }};
const context = {window, document, structuredClone, console, setInterval: () => 1, clearInterval() {},
  fetch: async (url, args) => {
    const path = url.split('/sidecar/api/memory/v1')[1];
    calls.push([path,args]);
    let result;
    if (path === '/memory/claims') result = memory.claims;
    else if (/^\/memory\/claims\/\d+$/.test(path)) result = memory.detail(Number(path.split('/').at(-1)));
    else if (path === '/memory/queue') result = memory.queue;
    else if (path === '/memory/core') result = memory.core;
    else if (path === '/memory/core/versions/1') result = {id: 1, text: 'Synthetic old core version'};
    else if (path === '/memory/recall') {
      if (recallFails) { recallFails = false; return {ok: false, status: 503, json: async () => ({error: {code: 'sidecar_unconfigured', retryable: true}})}; }
      result = memory.recall;
    }
    else if (path === '/memory/episodes') result = memory.episodes;
    else if (path === '/memory/refresh') result = memory.refresh;
    else if (path === '/memory/sidecar') result = memory.sidecar;
    else if (path === '/lab/pipelines/production') result = {spec,hash:'sha256:synthetic',fidelity:{state:'unavailable',reason:'test'}};
    else if (path === '/lab/operators') result = {operators:ops};
    else if (path === '/lab/pipelines/validate') {
      const draft = JSON.parse(args.body);
      const ids = {'$query':'Query'}; const errors = [];
      for (const step of draft.nodes) {
        const op = ops.find(x => x.name === step.op);
        const sources = Object.values(step.in).flat();
        if (!op?.available || sources.some(source => ids[source] !== (op.input === 'CandidateList[]' ? 'CandidateList' : op.input))) errors.push('input_type_or_order');
        if (step.op === 'filter' && step.config.path_prefix.includes('%')) errors.push('config');
        ids[step.id] = op?.output;
      }
      result = {status:errors.length ? 'invalid' : 'ok', errors, hash:errors.length ? null : 'sha256:valid'};
    } else throw Error('unexpected API '+path);
    return {ok:true,status:200,json:async()=>result};
  }};

vm.runInNewContext(fs.readFileSync('web/control-surface/quoth/boot.js','utf8'),context);
const find = (part, tag = 'button') => body.querySelectorAll('*').find(x => x.tag === tag && x.textContent.includes(part));
const flush = async () => { for (let i=0;i<8;i++) await new Promise(resolve => setImmediate(resolve)); };
const text = () => body.querySelectorAll('*').map(x => x._text).filter(Boolean).join(' | ');
(async () => {
  handle.settings.open({restoreFocus() {}}); await flush();
  // Accessible frame: a labelled modal dialog, two labelled navs, one main, the title focused.
  const dialog = body.querySelector('.quoth-overlay');
  assert.equal(dialog.attrs.role, 'dialog'); assert.equal(dialog.attrs['aria-modal'], 'true');
  assert.deepEqual(body.querySelectorAll('nav').map(x => x.attrs['aria-label']).slice(0, 2), ['Memory views', 'Retrieval lab']);
  assert.equal(body.querySelectorAll('main').length, 1);
  assert(find('Quoth', 'h1') && body.querySelector('h1').focused, 'dialog title not focused on open');
  // The overview leads: what needs you, what the assistant knows, health; never a decision button.
  assert(find('Overview', 'h2'), 'overview did not render: ' + text());
  assert(find('Needs you', 'h3') && find('What the assistant knows', 'h3') && find('Health', 'h3'));
  // Theme follows the WebUI: `.dark` on :root, light when only --bg is set, the media query when neither.
  assert.equal(dialog.dataset.scheme, 'dark');
  rootClasses.delete('dark'); themeObserver(); assert.equal(dialog.dataset.scheme, 'light');
  delete rootVars['--bg']; themeObserver(); assert.equal(dialog.dataset.scheme, undefined);
  rootVars['--bg'] = '#0D0D1A'; rootClasses.add('dark'); themeObserver(); assert.equal(dialog.dataset.scheme, 'dark');
  // The sidecar row reports the sidecar, not the queue's red.
  const sidecarRow = body.querySelectorAll('.quoth-checks li').find(li => li.textContent.startsWith('Sidecar'));
  assert.equal(sidecarRow.dataset.status, 'green'); assert(sidecarRow.textContent.includes('reachable · v0.14.0') && !sidecarRow.textContent.includes('red'));
  assert(find('memories wait for your word', 'p') && find('review memory', 'q'), 'no plain call to action: ' + text());
  assert.equal(body.querySelector('.quoth-nav button').attrs['aria-current'], 'page');
  const noDecisions = () => assert(!body.querySelectorAll('button').some(x => /^(accept|reject|approve|apply|forget|delete|edit|undo)\b/i.test(x.textContent.trim())),
    'a decision button appeared: ' + text());
  noDecisions();
  // Keyboard: arrows move between views in the sidebar.
  const navButtons = body.querySelectorAll('.quoth-nav button');
  document.activeElement = navButtons[0]; dialog.key('ArrowDown'); assert(navButtons[1].focused);
  // Memory table, filters, detail, supersede chain, back.
  find('Memories').click(); await flush();
  assert(find('Memories', 'h2'), 'memories did not render: ' + text());
  assert(body.querySelectorAll('*').some(x => x.tag === 'table' && x.className.split(' ').includes('quoth-table')));
  assert(body.querySelectorAll('*').some(x => x.tag === 'td' && x.textContent === 'imported'));
  assert(body.querySelectorAll('*').some(x => x.tag === 'td' && x.textContent.includes('replaced by #2')));
  const status = body.querySelectorAll('*').find(x => x.tag === 'select' && x.parent?._text === 'Status');
  status.change('pending'); await flush();
  assert.deepEqual(JSON.parse(calls.filter(([p]) => p === '/memory/claims').at(-1)[1].body), {status: 'pending', offset: 0, limit: 50});
  const search = body.querySelectorAll('*').find(x => x.tag === 'input' && x.type === 'search');
  search.value = 'blue shed'; search.key('Enter'); await flush();
  assert.deepEqual(JSON.parse(calls.filter(([p]) => p === '/memory/claims').at(-1)[1].body), {status: 'pending', q: 'blue shed', offset: 0, limit: 50});
  find('#1').click(); await flush();
  assert(find('Memory #1', 'h2') && find('approved via', 'dd') && find('hermes:tg1:1', 'dd') && find('proposed by the assistant', 'li'));
  assert(find('#1 (this memory)', 'strong'));
  assert(find('Nevermore.', 'p') && find('Replaced by #2', 'p'), 'a replaced memory must say so plainly');
  find('#2 · accepted').click(); await flush(); assert(find('Memory #2', 'h2'));
  assert(!find('Nevermore.', 'p'), 'a current memory is not nevermore');
  find('Back to memories').click(); await flush(); assert(find('Memories', 'h2'));
  // Review queue: plan, the clean-up bucket with its provenance, a numbered round with a drafted reply.
  find('Review queue').click(); await flush();
  assert(find('Review queue', 'h2') && find('How a review goes', 'h3'));
  assert(find('Duplicates', 'summary') && find('Same as #2', 'p') && find('Synthetic memory 2', 'q'));
  assert(find('First, the clean-up', 'h3') && find('Then, topic by topic', 'h3') && find('Home', 'h4'));
  assert(find('Synthetic rewrite 4', 'p') && find('Originally: Synthetic memory 4', 'p'));
  const reply = () => body.querySelector('.quoth-reply q').textContent;
  assert.equal(reply(), 'ok');
  const drop = body.querySelectorAll('*').find(x => x.tag === 'input' && x.type === 'checkbox');
  assert.equal(drop.attrs['aria-label'], 'Drop 1 in your drafted answer');
  drop.checked = true; drop.change(); assert.equal(reply(), 'ok, drop 1');
  // Only the round the assistant shows next is numbered and drafted; a later topic says so instead.
  assert.equal(body.querySelectorAll('.quoth-reply').length, 1, 'a later topic got a drafted reply');
  assert.equal(body.querySelectorAll('input').filter(x => x.type === 'checkbox').length, 1);
  assert(find('The assistant reaches this topic after “Home”', 'p') && find('Work', 'h4'));
  // Copy re-reads the queue first; an unchanged round copies (here: the no-clipboard fallback).
  const queueReads = () => calls.filter(([p]) => p === '/memory/queue').length;
  const copyDraft = () => body.querySelector('.quoth-reply .quoth-copy').click();
  let reads = queueReads();
  copyDraft(); await flush();
  assert.equal(queueReads(), reads + 1, 'copy did not re-check the queue');
  assert(body.querySelector('.quoth-live').textContent.includes('Copy unavailable'), 'copy fallback not announced');
  // The assistant moved on (Home answered in chat): the stale draft is refused, never copied.
  const saved = memory.queue;
  memory.queue = {...saved, groups: {...saved.groups, accept: [saved.groups.accept[1]], duplicate: []}};
  copyDraft(); await flush();
  assert(find('The assistant has moved on to “Work”', 'span') && body.querySelector('.quoth-live').textContent.includes('Nothing copied'));
  // Same topic, different memory in a numbered place: refused too.
  memory.queue = {...saved, groups: {...saved.groups, accept: [{...saved.groups.accept[0], id: 9}, saved.groups.accept[1]]}};
  copyDraft(); await flush();
  assert(find('This round changed since the page loaded', 'span'));
  memory.queue = saved;
  find('Refresh the queue').click(); await flush();
  assert(!find('Nothing copied', 'span') && body.querySelector('.quoth-reply q').textContent === 'ok, drop 1');
  noDecisions();
  find('By suggestion').click(); await flush();
  assert(find('Remember', 'summary') && find('Duplicates', 'summary'));
  find('In review order').click(); await flush();
  find('#3').click(); await flush(); assert(find('Memory #3', 'h2') && find('Suggests', 'dt'));
  find('Duplicate of #2').click(); await flush(); assert(find('Memory #2', 'h2'));
  find('Back to review queue').click(); await flush(); assert(find('Review queue', 'h2'));
  find('Core').click(); await flush();
  assert(find('Synthetic memory 2', 'pre') && find('Synthetic memory 2', 'li') && find('ledger changed since', 'p'));
  find('Show version #1').click(); await flush(); assert(find('Synthetic old core version', 'pre'));
  // Recall: a configuration error is named, then a real dry run renders cosine/word marks.
  find('Recall').click(); await flush();
  const recallText = body.querySelectorAll('*').find(x => x.tag === 'textarea' && x.dataset.private);
  recallText.value = 'where is the kayak';
  find('Show what the assistant would recall').click(); await flush();
  assert(find('--quoth-socket', 'p'), 'unconfigured socket not named: ' + text());
  find('Show what the assistant would recall').click(); await flush();
  assert(find('cosine 0.71', 'span') && find('word match', 'span') && find('PROJECTS/Garden.md:1-3', 'li') && find('hermes:tg1:2 · 2026-10-01', 'li'));
  assert(find('Exact block injected', 'summary') && find('<quoth-recall>', 'pre'));
  assert.equal(JSON.parse(calls.filter(([p]) => p === '/memory/recall').at(-1)[1].body).text, 'where is the kayak');
  find('What the assistant said').click(); await flush();
  assert.equal(recallText.value, '', 'private recall text survived a tab switch');
  const episodeQuery = body.querySelectorAll('*').find(x => x.tag === 'input' && x.dataset.private);
  episodeQuery.value = 'paddles'; find('Search').click(); await flush();
  assert(find('Synthetic reply about paddles', 'blockquote'));
  find('Refresh history').click(); await flush();
  assert(find('Heartbeat RED: last_run_hold', 'p') && body.querySelectorAll('*').some(x => x.tag === 'td' && x.textContent === 'HOLD'));
  assert(body.querySelectorAll('*').some(x => x.tag === 'td' && x.textContent === 'renamed 2 · stale 1'));
  assert(body.querySelectorAll('*').some(x => x.attrs['aria-label'] === 'Last 1 runs: 1 HOLD'), 'night strip has no text summary');
  noDecisions();
  assert(!calls.some(([p, args]) => p.startsWith('/memory/') && args.method !== 'GET' &&
    !['/memory/claims', '/memory/recall', '/memory/episodes'].includes(p)), 'unexpected memory write route');
  find('Playground').click(); await flush();
  assert(find('llm_answer', 'p'), 'palette did not render: ' + text());
  assert(find('llm_answer', 'p').textContent.includes('disabled'));
  assert(find('Add llm_answer').disabled);
  assert(find('Linear builder', 'h3'));
  find('Add filter').click();
  assert(find('Inspect filter'));
  assert(find('filter · lab · Executable', 'p'));
  const tree = body.querySelectorAll('*').filter(x => x.attrs.role === 'treeitem');
  tree[1].key('ArrowDown'); assert(tree[2].focused);
  const port = body.querySelectorAll('*').find(x => x.tag === 'select' && x.parent?._text.includes('CandidateList'));
  port.change('guard');
  find('Validate draft').click(); await flush();
  assert(find('Invalid draft', 'p'));
  port.change('dense');
  const path = body.querySelectorAll('*').find(x => x.tag === 'input' && x.parent?._text.includes('path_prefix'));
  path.change('%'); find('Validate draft').click(); await flush(); assert(find('Invalid draft', 'p'));
  path.change('ME/'); find('Validate draft').click(); await flush(); assert(find('Validated immutable', 'p'));
  assert(!find('Move filter up').disabled);
  find('Move filter up').click(); find('Validate draft').click(); await flush(); assert(find('Invalid draft', 'p'));
  find('Move filter down').click(); find('Validate draft').click(); await flush(); assert(find('Validated immutable', 'p'));
  tree[1].key('a'); assert(body.querySelectorAll('*').some(x => x.focused && x.textContent.includes('Add')));
  find('Remove filter').click(); assert(!find('Inspect filter'));
  const second = find('Add lexical_bm25'); second.click();
  assert(find('Inspect lexical_bm25'));
  find('Add fuse').click(); assert(find('Inspect fuse'));
  find('Move fuse up').click(); assert(find('Inspect fuse'));
  const insert = body.querySelectorAll('*').find(x => x.tag === 'select' && x.attrs['aria-label'] === 'Add step before out');
  assert(insert.children.some(x => x.value === 'filter'));
  insert.change('filter'); assert(find('Inspect filter'));
  assert.equal(body.querySelectorAll('*').filter(x => x.tag === 'textarea' && x.dataset.private).length,1);
  const source = fs.readFileSync('web/control-surface/quoth/boot.js','utf8');
  assert(!/localStorage|sessionStorage|indexedDB|innerHTML|console\./.test(source));
  console.log('Synthetic DOM: dialog landmarks, overview, nav keys, memory table/filters/detail/chain/nevermore, review queue plan/buckets/round reply draft, no decision buttons, core, recall, episodes, refresh history; playground palette, typed ports, config, graph keyboard, linear add/remove/reorder, no storage or logging PASS');
})().catch(error => {console.error(error);process.exitCode=1});
