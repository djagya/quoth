/* Mocked Hermes extension host + Quoth sidecar for the preview page. Every text here is invented. */
(() => {
  'use strict';
  const params = new URLSearchParams(location.search);
  const scenario = params.get('scenario') || 'rich';
  // The WebUI's own theme switch: `.dark` on :root, its variables defined in both modes.
  if (params.get('host') !== 'bare') {
    const dark = params.get('theme') ? params.get('theme') === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    document.documentElement.classList.add('webui');
    document.documentElement.classList.toggle('dark', dark);
  }
  let seed = 7;
  const rand = () => { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };
  const pick = list => list[Math.floor(rand() * list.length)];
  const NOW = Date.now();
  const iso = hoursAgo => new Date(NOW - hoursAgo * 3600e3).toISOString().replace(/\.\d+Z$/, '.000Z');

  const TOPICS = {
    'Food & health': ['prefers mint tea after lunch', 'avoids coffee after 3 pm', 'cooks lentil soup on Sundays',
      'takes the stairs instead of the lift', 'likes rye bread with salted butter', 'drinks sparkling water at dinner',
      'is mildly allergic to kiwi', 'stretches for ten minutes each morning'],
    'Work': ['writes design notes before any code', 'keeps Fridays free of meetings', 'reviews pull requests in the morning',
      'prefers short written updates over calls', 'names branches after the change, not the ticket', 'keeps a decision log per project'],
    'Family': ['calls their sister on Sunday evenings', 'has a niece who is learning the cello', 'visits their parents each December',
      'has a brother who lives by the lake', 'plans a family dinner every spring'],
    'Home': ['keeps the kayak in the blue shed', 'waters the balcony plants on Tuesdays', 'the spare key hangs in the hallway cabinet',
      'repainted the study in a pale green', 'stores winter tyres in the cellar', 'the paddles hang on the left rack'],
    'Travel': ['prefers night trains for trips under ten hours', 'packs a paper map on hikes', 'likes window seats',
      'books lodgings near a river when possible', 'keeps a travel journal in a grey notebook'],
    'Music': ['listens to piano trios while writing', 'is learning a nocturne on the upright piano', 'keeps vinyl for weekend listening',
      'goes to one chamber concert a month'],
    'Garden': ['grows tomatoes in the south bed', 'composts kitchen scraps in two bins', 'prunes the apple tree in February',
      'saves bean seeds in paper envelopes'],
    'Reading': ['reads one poem before sleep', 'keeps a commonplace book of quotations', 'prefers paper books for fiction',
      'is rereading a collection of Gothic tales', 'underlines in pencil, never ink'],
    'Money': ['reviews the budget on the first of the month', 'pays bills by standing order', 'keeps an emergency fund in a separate account'],
    'Other': ['mentioned a dentist appointment next week', 'asked about the weather on Thursday', 'was tired after the long drive'],
  };
  const LOCATORS = ['PROJECTS/Garden.md:12-14', 'NOTES/Kitchen.md:3-5', 'JOURNAL/2026-09.md:41-44', 'PEOPLE/Family.md:8-9',
    'HOME/Shed inventory.md:1-6', 'WORK/Practices.md:20-22', 'READING/Commonplace.md:30-31'];
  const NOTES = ['same fact, shorter wording on the kept one', 'one-off remark, not durable', 'moved it last month',
    'already in the notes', 'only said in passing', null, null, null];
  const KINDS = {'Food & health': 'preference', Work: 'preference', Family: 'person', Home: 'environment', Travel: 'preference',
    Music: 'taste', Garden: 'environment', Reading: 'taste', Money: 'project_state', Other: 'episode'};
  const ORIGINS = ['assistant_proposal', 'assistant_proposal', 'owner_statement', 'imported'];
  const AUTH = {owner_statement: 'owner', owner_note: 'owner', assistant_proposal: 'assistant', imported: 'imported'};
  const ACTIONS = [['accept', 140], ['keep', 12], ['none', 16], ['duplicate', 61], ['covered', 40], ['stale', 22], ['reject', 32]];
  const ORDER = ['accept', 'keep', 'none', 'duplicate', 'covered', 'stale', 'reject'];

  // Accepted memories first (ids 1..), then pending (oldest first), then the nevermore ones.
  const claims = [];
  let id = 1;
  const topicNames = Object.keys(TOPICS);
  const make = (topic, status, hoursAgo, extra = {}) => {
    const origin = pick(ORIGINS);
    const fact = pick(TOPICS[topic]);
    const c = {id: id++, text: 'The owner ' + fact + '.', kind: KINDS[topic], origin, authority: AUTH[origin], proposer: origin === 'imported' ? 'builtin_memory' : 'assistant',
      review_status: status, current: status === 'accepted' || status === 'pending', recorded_at: iso(hoursAgo), quote_verified: origin === 'owner_statement' ? 1 : 0,
      supersedes: null, superseded_by: null, valid_to: null, use_count: status === 'accepted' ? Math.floor(rand() * 30) : 0, auto_accepted: 0,
      topic, ...extra};
    claims.push(c);
    return c;
  };
  for (let i = 0; i < 412; i++) make(pick(topicNames.slice(0, 9)), 'accepted', 2000 + i * 6);
  const pendingActions = ACTIONS.flatMap(([a, n]) => Array(n).fill(a)).sort(() => rand() - .5);
  const accepted = claims.slice();
  pendingActions.forEach((action, i) => {
    const topic = action === 'reject' && rand() < .5 ? 'Other' : pick(topicNames);
    const c = make(topic, 'pending', 19 * 24 - i * 1.3);
    const s = {action, topic, suggested_by: 'assistant', suggested_at: iso(30), suggested_kind: null, rewrite: null, note: null,
      duplicate_of: null, evidence_locator: null};
    if (action === 'accept' && rand() < .35) s.rewrite = c.text.replace(/\.$/, '') + ', most weeks.';
    if (action === 'duplicate') { const t = pick(accepted); s.duplicate_of = t.id; c.text = t.text.replace(/^The owner /, 'The owner mentioned again: '); }
    if (action === 'covered') { s.evidence_locator = pick(LOCATORS); s.note = 'the note says the same'; }
    if (action === 'stale') s.note = 'moved it last month';
    if (action === 'reject') s.note = pick(NOTES);
    if (action === 'keep') s.note = 'unclear whether this still holds';
    c.suggestion = s;
  });
  for (let i = 0; i < 180; i++) make(pick(topicNames), 'rejected', 3000 + i * 5, {current: false, reviewed_at: iso(2900 + i * 5), review_note: 'dropped via hermes:tg1:' + (900 + i)});
  for (let i = 0; i < 32; i++) make(pick(topicNames), 'expired', 4000 + i * 7, {current: false, valid_to: iso(100 + i)});
  // A supersede chain: #1 replaced by a later accepted claim.
  const replacement = make('Home', 'accepted', 50, {supersedes: 1, text: 'The owner keeps the kayak in the green shed.'});
  Object.assign(claims[0], {superseded_by: replacement.id, current: false, valid_to: iso(50), text: 'The owner keeps the kayak in the blue shed.', topic: 'Home', kind: 'environment'});
  const byId = Object.fromEntries(claims.map(c => [c.id, c]));
  const pending = claims.filter(c => c.review_status === 'pending');

  // Positions as `digest` numbers them: per topic, accept/keep/none first, oldest first.
  const positions = {};
  for (const t of topicNames) {
    pending.filter(c => c.suggestion.topic === t).sort((a, b) => ORDER.indexOf(a.suggestion.action) - ORDER.indexOf(b.suggestion.action))
      .forEach((c, i) => { positions[c.id] = i + 1; });
  }
  const row = c => ({id: c.id, text: c.text, kind: c.kind, origin: c.origin, authority: c.authority, review_status: c.review_status,
    current: c.current, recorded_at: c.recorded_at, quote_verified: c.quote_verified, supersedes: c.supersedes, superseded_by: c.superseded_by,
    valid_to: c.valid_to, use_count: c.use_count, auto_accepted: c.auto_accepted});
  const counts = Object.fromEntries(ORDER.map(a => [a, pending.filter(c => c.suggestion.action === a).length]));
  const cleanup = counts.duplicate + counts.covered + counts.stale + counts.reject;
  const toRead = pending.length - cleanup;
  const queue = () => {
    const listed = pending.slice(0, 300);
    const groups = Object.fromEntries(ORDER.map(a => [a, []]));
    for (const c of listed) {
      const item = {...row(c), suggestion: c.suggestion, position: positions[c.id]};
      if (c.suggestion.duplicate_of) {
        const t = byId[c.suggestion.duplicate_of];
        item.duplicate_target = {id: t.id, text: t.text, review_status: t.review_status, kept: !t.superseded_by, kept_id: t.superseded_by ? null : t.id};
      }
      groups[c.suggestion.action].push(item);
    }
    const topics = topicNames.map(t => {
      const members = pending.filter(c => c.suggestion.topic === t);
      const by = {};
      for (const c of members) by[c.suggestion.action] = (by[c.suggestion.action] || 0) + 1;
      return {topic: t, total: members.length, to_read: members.filter(c => ['accept', 'keep', 'none'].includes(c.suggestion.action)).length, by_suggestion: by};
    }).filter(t => t.total).sort((a, b) => (a.topic === 'Other') - (b.topic === 'Other') || a.topic.localeCompare(b.topic));
    return {pending: pending.length, oldest_pending_age_days: 19.4, status: 'red', reasons: ['pending_queue_large', 'pending_queue_old'],
      counts, groups, truncated: pending.length > 300, topics,
      plan: {cleanup, to_read: toRead, unsorted: counts.none, round_size: 10, rounds: Math.ceil(toRead / 10)},
      buckets: {duplicate: counts.duplicate, covered: counts.covered, stale: counts.stale, reject: counts.reject}};
  };
  const totals = {};
  for (const c of claims) totals[c.review_status] = (totals[c.review_status] || 0) + 1;
  const claimsView = body => {
    let rows = claims.slice().reverse();
    if (body.status === 'current') rows = rows.filter(c => c.current);
    else if (body.status) rows = rows.filter(c => c.review_status === body.status);
    if (body.kind) rows = rows.filter(c => c.kind === body.kind);
    if (body.authority) rows = rows.filter(c => c.authority === body.authority);
    if (body.q) rows = rows.filter(c => c.text.toLowerCase().includes(body.q.toLowerCase()));
    const offset = body.offset || 0, limit = body.limit || 50;
    return {total: rows.length, offset, limit, ledger_totals: totals,
      filters: {status: ['pending', 'accepted', 'rejected', 'expired', 'current'],
        kind: ['identity', 'preference', 'consent', 'correction', 'environment', 'person', 'taste', 'project_state', 'episode', 'pointer', 'unclassified'],
        authority: ['owner', 'assistant', 'imported']},
      items: rows.slice(offset, offset + limit).map(c => ({...row(c), suggestion: c.suggestion?.action || null}))};
  };
  const detail = n => {
    const c = byId[n];
    if (!c) return null;
    const chain = c.supersedes || c.superseded_by ? [byId[1], byId[replacement.id]].map(x => ({id: x.id, text: x.text, kind: x.kind,
      review_status: x.review_status, current: x.current, ...(x.id === c.id ? {self: true} : {})})) : [];
    return {claim: {...c, evidence_locator: c.origin === 'owner_statement' ? 'hermes:tg1:' + (4000 + c.id) : null,
        evidence_quote: c.origin === 'owner_statement' ? 'I ' + c.text.replace(/^The owner /, '').replace(/s\b/, '') : null,
        reason: c.origin === 'assistant_proposal' ? 'came up twice in conversation' : null, confidence: c.origin === 'assistant_proposal' ? 0.82 : null,
        subject_key: null, valid_from: c.review_status === 'accepted' ? c.recorded_at : null, reviewed_at: c.reviewed_at || null,
        review_note: c.review_note || (c.review_status === 'accepted' ? 'approved via hermes:tg1:' + (5000 + c.id) : null), forget_reason: null, last_used_at: c.use_count ? iso(5) : null},
      suggestion: c.suggestion || null,
      events: [{at: c.recorded_at, action: 'proposed', actor: c.proposer, detail: {kind: c.kind}},
        ...(c.suggestion ? [{at: iso(30), action: 'suggested', actor: 'assistant', detail: {action: c.suggestion.action, topic: c.suggestion.topic}}] : []),
        ...(c.review_status === 'accepted' ? [{at: c.recorded_at, action: 'accepted', actor: 'owner', detail: {batch: 'hermes:tg1:' + (5000 + c.id)}}] : []),
        ...(c.superseded_by ? [{at: iso(50), action: 'superseded', actor: 'owner', detail: {by: c.superseded_by}}] : [])],
      chain};
  };
  const coreText = ['# Core memory', '', '## Preferences', ...accepted.filter(c => c.kind === 'preference').slice(0, 9).map(c => '- ' + c.text),
    '', '## People', ...accepted.filter(c => c.kind === 'person').slice(0, 6).map(c => '- ' + c.text),
    '', '## Environment', ...accepted.filter(c => c.kind === 'environment').slice(0, 8).map(c => '- ' + c.text),
    '', '## Taste', ...accepted.filter(c => c.kind === 'taste').slice(0, 6).map(c => '- ' + c.text)].join('\n');
  const core = {source: 'published', file: 'core-3f9a1c2e7b10.md', text: coreText, live_differs: true,
    live: {claim_ids: Array.from({length: 29}, (_, i) => i + 2), dropped_ids: [41, 52], chars: coreText.length, limit: 3200, budget: 4000},
    versions: Array.from({length: 6}, (_, i) => ({id: 6 - i, sha256: (i + 'f3a9c1e7b2d4').repeat(6).slice(0, 64), chars: coreText.length - i * 40,
      claims: 29 - i, budget: 4000, compiled_at: iso(30 + i * 170)}))};
  const recall = {text: '<quoth-recall>\n- #' + replacement.id + ' environment: ' + replacement.text + '\n- #57 environment: The owner keeps the paddles on the left rack.\n</quoth-recall>\n<quoth-evidence>\nHOME/Shed inventory.md:1-6 (2026-09-14)\n</quoth-evidence>',
    evidence_status: 'ok', episodes_status: 'ok',
    claims: [{id: replacement.id, kind: 'environment', authority: 'owner', text: replacement.text, cosine: 0.81, match: 'cosine'},
      {id: 57, kind: 'environment', authority: 'assistant', text: 'The owner keeps the paddles on the left rack.', cosine: 0.74, match: 'cosine'},
      {id: 112, kind: 'preference', authority: 'assistant', text: 'The owner prefers paddling on calm mornings.', cosine: 0.58, match: 'cosine'},
      {id: 9, kind: 'environment', authority: 'imported', text: 'The owner stores the life vests in the shed loft.', cosine: null, match: 'words'}],
    evidence: [{locator: 'HOME/Shed inventory.md:1-6', note_date: '2026-09-14'}, {locator: 'JOURNAL/2026-09.md:41-44', note_date: '2026-09-21'}],
    episodes: [{locator: 'hermes:tg1:20231', timestamp: iso(70), score: 0.66}, {locator: 'hermes:tg1:19876', timestamp: iso(300), score: 0.61}]};
  const episodes = {answer_use: 'assistant-origin', hits: [
    {locator: 'hermes:tg1:20231', timestamp: iso(70), score: 0.71, text: 'The paddles are on the left rack of the shed, next to the life vests. The kayak itself is in the green shed since the move.'},
    {locator: 'hermes:tg1:19876', timestamp: iso(300), score: 0.64, text: 'You mentioned you prefer calm mornings on the lake; Thursday looks still until ten.'},
    {locator: 'hermes:tg1:18102', timestamp: iso(900), score: 0.52, text: 'I noted the spare key is in the hallway cabinet, top drawer.'}]};
  const outcomes = ['SWITCH', 'SWITCH', 'SWITCH', 'SWITCH', 'SWITCH', 'SKIP', 'SWITCH', 'HOLD', 'SWITCH', 'SWITCH'];
  const receipts = Array.from({length: 34}, (_, i) => {
    const outcome = i === 11 ? 'FAIL' : i === 4 ? 'HOLD' : outcomes[(i * 7) % outcomes.length];
    const seconds = {ingest: 40 + rand() * 30, embed: 300 + rand() * 200, gate: 60 + rand() * 20, freeze: 5 + rand() * 4};
    return {receipt_id: 'r' + i, recorded_at: iso(19 + i * 24), outcome, gate_mode: i % 6 === 0 ? 'code' : 'content', seconds,
      total_seconds: Math.round(Object.values(seconds).reduce((a, b) => a + b, 0) * 10) / 10,
      reasons: outcome === 'HOLD' ? ['private_hit@10_regressed'] : outcome === 'FAIL' ? ['embed_timeout'] : outcome === 'SKIP' ? ['no_vault_change'] : [],
      gold_drift: i % 5 === 0 ? {renamed: 2, stale: 1} : null, rollback: null};
  });
  const refresh = {receipts_state: 'ok', receipts, heartbeat: {state: 'ok', status: 'green', reasons: [], checked_at: iso(0.2),
    fresh_as_of: iso(19), fresh_age_hours: 19, max_fresh_age_hours: 36, max_run_age_hours: 26,
    serving: {frozen_at: iso(19.1), age_hours: 19.1}, last_run: {outcome: 'SWITCH', reasons: [], recorded_at: iso(19), receipt_id: 'r0'}}};
  const sidecar = {version: '0.14.0', status: 'green', quote_verification: true,
    ledger: {status: 'red', reasons: ['pending_queue_large', 'pending_queue_old'], pending: pending.length, accepted_current: 411, oldest_pending_age_days: 19.4},
    evidence: {status: 'ok', snapshot_id: 'snapshot-20261028T020500Z'},
    episodes: {status: 'green', reasons: [], messages: 18234, embedded: 18230, pending: 4, newest: iso(0.5), last_tick_at: iso(0.03)},
    owner_episodes: {status: 'green', reasons: [], messages: 6120, embedded: 6120, pending: 0, newest: iso(0.6), last_tick_at: iso(0.05)}};

  // Empty scenario: a fresh install with nothing yet.
  const emptyState = {
    '/memory/queue': {pending: 0, oldest_pending_age_days: null, status: 'green', reasons: [], counts: Object.fromEntries(ORDER.map(a => [a, 0])),
      groups: Object.fromEntries(ORDER.map(a => [a, []])), truncated: false, topics: [], plan: {cleanup: 0, to_read: 0, unsorted: 0, round_size: 10, rounds: 0},
      buckets: {duplicate: 0, covered: 0, stale: 0, reject: 0}},
    '/memory/claims': {total: 0, offset: 0, limit: 50, ledger_totals: {}, items: [], filters: claimsView({}).filters},
    '/memory/core': {source: 'live', file: null, text: '# Core memory\n', live_differs: null, live: {claim_ids: [], dropped_ids: [], chars: 14, limit: 3200, budget: 4000}, versions: []},
    '/memory/refresh': {receipts_state: 'ok', receipts: [], heartbeat: null},
    '/memory/sidecar': {...sidecar, ledger: {status: 'green', reasons: [], pending: 0, accepted_current: 0, oldest_pending_age_days: null},
      episodes: {status: 'disabled'}, owner_episodes: {status: 'disabled'}, evidence: {status: 'disabled'}},
    '/memory/recall': {text: '', claims: [], evidence: [], episodes: [], evidence_status: 'disabled', episodes_status: 'disabled'},
    '/memory/episodes': {answer_use: 'assistant-origin', hits: []},
  };
  const ERRORS = {'/memory/queue': 'ledger_unavailable', '/memory/claims': 'ledger_unavailable', '/memory/core': 'core_unavailable',
    '/memory/refresh': 'receipts_unconfigured', '/memory/sidecar': 'sidecar_unavailable', '/memory/recall': 'sidecar_unavailable',
    '/memory/episodes': 'episodes_unconfigured'};

  const reply = (status, data) => new Response(JSON.stringify(data), {status, headers: {'Content-Type': 'application/json'}});
  window.fetch = async (url, args = {}) => {
    const path = String(url).split('/sidecar/api/memory/v1')[1] || '';
    const body = args.body ? JSON.parse(args.body) : {};
    if (scenario === 'slow') await new Promise(resolve => setTimeout(resolve, 60000));
    else await new Promise(resolve => setTimeout(resolve, 40));
    if (path.startsWith('/lab/') || ['/status', '/freshness', '/quality'].includes(path)) return reply(503, {error: {code: 'lab_unavailable'}});
    if (scenario === 'error') {
      const base = path.replace(/\/\d+$/, '').replace(/\/versions$/, '');
      return reply(503, {error: {code: ERRORS[base] || 'sidecar_unavailable', retryable: true}});
    }
    if (scenario === 'empty' && emptyState[path]) return reply(200, emptyState[path]);
    if (path === '/memory/queue') return reply(200, queue());
    if (path === '/memory/claims') return reply(200, claimsView(body));
    const m = /^\/memory\/claims\/(\d+)$/.exec(path);
    if (m) { const d = detail(Number(m[1])); return d ? reply(200, d) : reply(404, {error: {code: 'claim_unknown'}}); }
    if (path === '/memory/core') return reply(200, core);
    if (/^\/memory\/core\/versions\/\d+$/.test(path)) return reply(200, {id: 1, text: coreText.split('\n').slice(0, 12).join('\n')});
    if (path === '/memory/recall') return reply(200, recall);
    if (path === '/memory/episodes') return reply(200, episodes);
    if (path === '/memory/refresh') return reply(200, refresh);
    if (path === '/memory/sidecar') return reply(200, sidecar);
    return reply(404, {error: {code: 'not_found'}});
  };
  const handle = {settings: {registerConfigure(fn) { this.open = fn; }}};
  window.hermesExt = {register: () => handle};
  window.__quothPreviewOpen = () => handle.settings.open?.({restoreFocus() {}});
})();
