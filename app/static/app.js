/* cronohelper -- preview state machine.
 *
 * The parse result is a draft that this file owns and mutates. /api/parse is
 * called exactly once per paste; every edit after that (moving a portion to
 * another day, deleting it, toggling a breakfast, adding a day column) happens
 * here, and /api/import is handed the edited draft. The server never re-parses
 * the original text.
 *
 * The unit of everything is one portion. A `2 adag` line arrives as two rows,
 * and each moves independently -- that is the whole point of the preview.
 */

'use strict';

const $ = (sel) => document.querySelector(sel);

const HU_DOW = ['Vas', 'Hét', 'Ked', 'Sze', 'Csü', 'Pén', 'Szo'];

const state = {
  rows: [],              // one portion each
  days: [],              // ordered dates rendered as columns
  breakfast: {},         // date -> bool (on by default)
  importedDays: new Set(),
  config: { breakfast: [], macro_tolerance: 0.02 },
  parsed: false,
  busy: false,
  // normalized dish name -> resolution from /api/resolve
  resolutions: {},
  // normalized dish name -> "use_existing" | "create_new_version"
  decisions: {},
  resolveError: '',
};

/* --- helpers ------------------------------------------------------------ */

// Parse an ISO date without letting the local timezone shift it a day.
function dowOf(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return HU_DOW[new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
}

function addDays(iso, n) {
  const [y, m, d] = iso.split('-').map(Number);
  const t = new Date(Date.UTC(y, m - 1, d + n));
  return t.toISOString().slice(0, 10);
}

const fmt = (n) => (Math.round(n * 10) / 10).toLocaleString('en-US');
const fmt0 = (n) => Math.round(n).toLocaleString('en-US');

function rowsOn(date) {
  return state.rows.filter((r) => r.date === date);
}

// A day is written if it has at least one lunch portion OR its breakfast is
// on. A day with no rows and breakfast switched off is skipped entirely.
function daysToImport() {
  return state.days.filter((d) => rowsOn(d).length > 0 || breakfastOn(d));
}

// Breakfast is on for every day column, on by default and independently
// toggleable, whether or not that day has any lunch rows. A day whose
// breakfast is on is written even with no lunch on it.
function breakfastOn(date) {
  return state.breakfast[date] !== false;
}

function breakfastTotals() {
  return state.config.breakfast.reduce(
    (a, f) => ({
      kcal: a.kcal + f.kcal,
      carbs_g: a.carbs_g + f.carbs_g,
      protein_g: a.protein_g + f.protein_g,
      fat_g: a.fat_g + f.fat_g,
    }),
    { kcal: 0, carbs_g: 0, protein_g: 0, fat_g: 0 }
  );
}

function totalsFor(date) {
  const t = rowsOn(date).reduce(
    (a, r) => ({
      kcal: a.kcal + r.kcal,
      carbs_g: a.carbs_g + r.carbs_g,
      protein_g: a.protein_g + r.protein_g,
      fat_g: a.fat_g + r.fat_g,
    }),
    { kcal: 0, carbs_g: 0, protein_g: 0, fat_g: 0 }
  );
  if (breakfastOn(date)) {
    const b = breakfastTotals();
    t.kcal += b.kcal;
    t.carbs_g += b.carbs_g;
    t.protein_g += b.protein_g;
    t.fat_g += b.fat_g;
  }
  return t;
}

function setState(msg, kind) {
  const el = $('#state');
  el.textContent = msg || '';
  el.className = 'state' + (kind ? ' ' + kind : '');
}

/* --- parse -------------------------------------------------------------- */

async function doParse() {
  const text = $('#paste').value;
  if (!text.trim()) {
    setState('Nothing to parse.', 'warn');
    return;
  }
  let res;
  try {
    res = await fetch('/api/parse', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text }),
    });
  } catch (e) {
    setState('Could not reach the server.', 'err');
    return;
  }
  const data = await res.json();

  $('#results-pane').hidden = true;

  if (!data.ok) {
    renderIssues(data.issues);
    $('#preview-pane').hidden = true;
    $('#confirm-bar').hidden = true;
    state.parsed = false;
    return;
  }

  $('#issues').hidden = true;
  state.rows = data.rows;
  // The two days after the pasted range are offered empty and ready to
  // receive a drop -- the Saturday case is first-class, not an edge case.
  state.days = data.days.concat(data.suggested_extra_days);
  state.breakfast = {};
  data.days.forEach((d) => (state.breakfast[d] = true));
  data.suggested_extra_days.forEach((d) => (state.breakfast[d] = true));
  state.parsed = true;
  state.resolutions = {};
  state.decisions = {};
  state.resolveError = '';

  await loadHistory();
  render();
  setState('');
  // Resolution reads from Cronometer but writes nothing to it, so it is safe
  // to run on paste. It is what lets a macro conflict surface in the preview
  // instead of halfway through an import.
  doResolve();
}

async function doResolve() {
  if (!state.parsed) return;
  setState('Checking your Cronometer foods…');
  try {
    const res = await fetch('/api/resolve', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(buildPayload()),
    });
    const data = await res.json();
    if (!res.ok) {
      state.resolveError = data.detail || `HTTP ${res.status}`;
      setState(state.resolveError, 'err');
      render();
      return;
    }
    state.resolutions = {};
    for (const d of data.dishes || []) state.resolutions[d.normalized_name] = d;
    state.resolveError = '';
  } catch (e) {
    state.resolveError = 'Could not reach the server to check foods.';
    setState(state.resolveError, 'err');
    render();
    return;
  }

  render();
  const n = conflicts().length;
  setState(
    n
      ? `${n} macro ${n === 1 ? 'conflict needs' : 'conflicts need'} a decision.`
      : '',
    n ? 'warn' : ''
  );
}

function conflicts() {
  return Object.values(state.resolutions).filter(
    (r) => r.status === 'conflict' && !state.decisions[r.normalized_name]
  );
}

function renderIssues(issues) {
  const box = $('#issue-list');
  box.innerHTML = '';
  for (const i of issues) {
    const el = document.createElement('div');
    el.className = 'issue';
    el.innerHTML =
      `<span class="lineno">line ${i.line_no}</span>` +
      `<span class="src"></span>` +
      `<span class="exp">expected <b></b></span>`;
    el.querySelector('.src').textContent = i.line || '(empty line)';
    el.querySelector('.exp b').textContent = i.expected;
    box.appendChild(el);
  }
  $('#issues').hidden = false;
  setState(`${issues.length} parse ${issues.length === 1 ? 'error' : 'errors'}.`, 'err');
}

async function loadHistory() {
  try {
    const res = await fetch('/api/history');
    const h = await res.json();
    state.importedDays = new Set(h.imported_days || []);
  } catch (e) {
    state.importedDays = new Set();
  }
}

/* --- render ------------------------------------------------------------- */

function render() {
  renderConflicts();
  renderGrid();
  renderSummary();
  renderConfirm();
  $('#preview-pane').hidden = false;
  $('#confirm-bar').hidden = false;
}

function renderConflicts() {
  const box = $('#conflict-list');
  const items = Object.values(state.resolutions).filter(
    (r) => r.status === 'conflict'
  );
  if (!items.length) {
    $('#conflicts').hidden = true;
    return;
  }
  box.innerHTML = '';

  for (const r of items) {
    const c = r.conflict;
    const decided = state.decisions[r.normalized_name];

    const el = document.createElement('div');
    el.className = 'conflict-item';

    const dish = document.createElement('div');
    dish.className = 'dish';
    dish.textContent = r.name;
    el.appendChild(dish);

    const why = document.createElement('div');
    why.className = 'why';
    why.textContent =
      `“${c.name}” (id ${c.food_id}) already exists on your account, but its ` +
      `macros differ by more than ${c.tolerance_pct}%. Nothing has been ` +
      `written for this dish.`;
    el.appendChild(why);

    // Side-by-side numbers, so the disagreement is readable as data.
    const keys = [
      ['kcal', 'kcal'],
      ['carbs_g', 'carbs'],
      ['protein_g', 'protein'],
      ['fat_g', 'fat'],
    ];
    const t = document.createElement('table');
    t.className = 'cmp';
    t.innerHTML =
      '<thead><tr><th></th>' +
      keys.map(([, l]) => `<th>${l}</th>`).join('') +
      '</tr></thead><tbody>' +
      `<tr><td>on Cronometer</td>${keys
        .map(([k]) => `<td>${fmt(c.existing[k])}</td>`)
        .join('')}</tr>` +
      `<tr><td>pasted</td>${keys
        .map(([k]) => `<td>${fmt(c.pasted[k])}</td>`)
        .join('')}</tr>` +
      `<tr><td>difference</td>${keys
        .map(
          ([k]) =>
            `<td class="${
              c.delta_pct[k] > c.tolerance_pct ? 'over' : ''
            }">${fmt(c.delta_pct[k])}%</td>`
        )
        .join('')}</tr>` +
      '</tbody>';
    el.appendChild(t);

    const choices = document.createElement('div');
    choices.className = 'choices';

    const useBtn = document.createElement('button');
    useBtn.type = 'button';
    useBtn.textContent = 'Use existing';
    useBtn.title = `Log against food ${c.food_id} and remember it`;
    if (decided === 'use_existing') useBtn.classList.add('chosen');
    useBtn.addEventListener('click', () => {
      state.decisions[r.normalized_name] = 'use_existing';
      doResolve();
    });

    const newBtn = document.createElement('button');
    newBtn.type = 'button';
    newBtn.textContent = 'Create new version';
    newBtn.title = c.new_version_name;
    if (decided === 'create_new_version') newBtn.classList.add('chosen');
    newBtn.addEventListener('click', () => {
      state.decisions[r.normalized_name] = 'create_new_version';
      doResolve();
    });

    choices.appendChild(useBtn);
    choices.appendChild(newBtn);

    // Manual override: pin this dish to a specific Cronometer food id. Not
    // needed for normal operation, but the escape hatch when a dish's
    // Cronometer name differs from the delivery name.
    const idInput = document.createElement('input');
    idInput.className = 'link-id';
    idInput.placeholder = 'or food id…';
    idInput.setAttribute('aria-label', `Link ${r.name} to a Cronometer food id`);

    const linkBtn = document.createElement('button');
    linkBtn.type = 'button';
    linkBtn.textContent = 'Link';
    linkBtn.addEventListener('click', () => linkFood(r.name, idInput.value));

    choices.appendChild(idInput);
    choices.appendChild(linkBtn);
    el.appendChild(choices);

    box.appendChild(el);
  }

  $('#conflicts').hidden = false;
}

async function linkFood(name, rawId) {
  const foodId = parseInt(rawId, 10);
  if (!foodId) {
    setState('Enter a numeric Cronometer food id to link.', 'warn');
    return;
  }
  try {
    const res = await fetch('/api/foods/link', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ name, food_id: foodId }),
    });
    const data = await res.json();
    if (!res.ok) {
      setState(data.detail || `Link failed (HTTP ${res.status}).`, 'err');
      return;
    }
    setState(`Linked “${name}” to ${data.food.name} (${foodId}).`, 'ok');
    doResolve();
  } catch (e) {
    setState('Could not reach the server.', 'err');
  }
}

function renderGrid() {
  const grid = $('#grid');
  grid.innerHTML = '';
  state.days.sort();

  for (const date of state.days) {
    grid.appendChild(renderDay(date));
  }

  $('#grid-hint').textContent =
    'Drag a row onto another day, or use its date field. ' +
    'Every day gets breakfast unless you switch it off — ' +
    'a day is skipped only when it has no rows and no breakfast.';
}

function renderDay(date) {
  const rows = rowsOn(date);
  const imported = state.importedDays.has(date);
  const isExtra = rows.length === 0;

  const day = document.createElement('article');
  day.className =
    'day' + (imported ? ' imported' : '') + (isExtra ? ' extra' : '');
  day.dataset.date = date;

  // --- header
  const head = document.createElement('header');
  head.innerHTML =
    `<span class="dow"></span><span class="iso"></span>` +
    `<span class="hspacer"></span>`;
  head.querySelector('.dow').textContent = dowOf(date);
  head.querySelector('.iso').textContent = date;
  // The badge is shown whenever the day will actually be touched, so an
  // otherwise-empty day that is still getting a breakfast reads as "new"
  // rather than looking inert.
  const willWrite = rows.length > 0 || breakfastOn(date);
  const badge = document.createElement('span');
  badge.className = 'badge ' + (imported ? 'imported' : 'new');
  badge.textContent = imported ? 'imported' : 'new';
  if (imported || willWrite) head.appendChild(badge);
  day.appendChild(head);

  // --- lunch rows
  const ul = document.createElement('ul');
  ul.className = 'rows';
  if (!rows.length) {
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = 'drop a portion here';
    ul.appendChild(li);
  } else {
    rows.forEach((r) => ul.appendChild(renderRow(r)));
  }
  day.appendChild(ul);

  // --- breakfast block
  day.appendChild(renderBreakfast(date));

  // --- day totals
  const t = totalsFor(date);
  const foot = document.createElement('footer');
  foot.innerHTML = [
    ['kcal', fmt0(t.kcal), 'kcal'],
    ['carbs', fmt(t.carbs_g) + ' g', ''],
    ['protein', fmt(t.protein_g) + ' g', ''],
    ['fat', fmt(t.fat_g) + ' g', ''],
  ]
    .map(
      ([lbl, v, cls]) =>
        `<div><span class="lbl">${lbl}</span><span class="v ${cls}">${v}</span></div>`
    )
    .join('');
  day.appendChild(foot);

  wireDropTarget(day, date);
  return day;
}

function renderRow(r) {
  const li = document.createElement('li');
  li.className = 'row';
  li.draggable = true;
  li.dataset.id = r.id;
  li.tabIndex = 0;

  // Mark every portion of a dish whose macros are still in dispute.
  const res = state.resolutions[r.normalized_name];
  if (res && res.status === 'conflict' && !state.decisions[r.normalized_name]) {
    li.classList.add('conflict');
    li.title = 'Macro conflict — see the panel above';
  }

  li.innerHTML =
    `<div class="top"><span class="code"></span><span class="name"></span></div>` +
    `<div class="bot">` +
    `<span class="macros">` +
    `<span class="kcal"></span>` +
    `<span><i>C</i> <span class="c"></span></span>` +
    `<span><i>P</i> <span class="p"></span></span>` +
    `<span><i>F</i> <span class="f"></span></span>` +
    `</span>` +
    `<span class="ctl">` +
    `<input type="date" class="move" aria-label="Move this portion to another date">` +
    `<button class="del" title="Remove this portion" aria-label="Remove this portion">&times;</button>` +
    `</span></div>`;

  li.querySelector('.code').textContent = r.code;
  li.querySelector('.name').textContent = r.name;
  li.querySelector('.name').title = r.name;
  li.querySelector('.kcal').textContent = fmt0(r.kcal);
  li.querySelector('.c').textContent = fmt(r.carbs_g);
  li.querySelector('.p').textContent = fmt(r.protein_g);
  li.querySelector('.f').textContent = fmt(r.fat_g);

  // Keyboard / mobile fallback for drag-and-drop.
  const dateInput = li.querySelector('.move');
  dateInput.value = r.date;
  dateInput.addEventListener('change', (e) => {
    const to = e.target.value;
    if (to) moveRow(r.id, to);
    else e.target.value = r.date;
  });

  li.querySelector('.del').addEventListener('click', () => {
    state.rows = state.rows.filter((x) => x.id !== r.id);
    render();
  });

  li.addEventListener('dragstart', (e) => {
    e.dataTransfer.setData('text/plain', r.id);
    e.dataTransfer.effectAllowed = 'move';
    li.classList.add('dragging');
  });
  li.addEventListener('dragend', () => li.classList.remove('dragging'));

  return li;
}

function renderBreakfast(date) {
  const on = breakfastOn(date);

  const box = document.createElement('div');
  box.className = 'breakfast' + (on ? '' : ' off');

  const label = document.createElement('label');
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = on;
  cb.addEventListener('change', () => {
    state.breakfast[date] = cb.checked;
    render();
  });
  label.appendChild(cb);
  label.appendChild(document.createTextNode('Breakfast'));
  box.appendChild(label);

  const ul = document.createElement('ul');
  ul.className = 'items';
  for (const f of state.config.breakfast) {
    const li = document.createElement('li');
    const t = document.createElement('span');
    t.className = 't';
    t.textContent = f.serving + ' ' + f.name;
    t.title = f.name;
    const v = document.createElement('span');
    v.textContent = fmt0(f.kcal);
    li.appendChild(t);
    li.appendChild(v);
    ul.appendChild(li);
  }
  box.appendChild(ul);
  return box;
}

function renderSummary() {
  const table = $('#summary');
  const head =
    '<tr><th>Day</th><th>Items</th><th>kcal</th><th>Carbs</th>' +
    '<th>Protein</th><th>Fat</th></tr>';

  const week = { kcal: 0, carbs_g: 0, protein_g: 0, fat_g: 0, items: 0 };
  let body = '';

  for (const date of state.days) {
    const rows = rowsOn(date);
    const t = totalsFor(date);
    const items = rows.length + (breakfastOn(date) ? state.config.breakfast.length : 0);
    week.kcal += t.kcal;
    week.carbs_g += t.carbs_g;
    week.protein_g += t.protein_g;
    week.fat_g += t.fat_g;
    week.items += items;

    const z = rows.length || breakfastOn(date) ? '' : ' class="zero"';
    body +=
      `<tr class="${state.importedDays.has(date) ? 'is-imported' : ''}">` +
      `<td${z}>${dowOf(date)} ${date}</td>` +
      `<td${z}>${items || '&mdash;'}</td>` +
      `<td${z}>${t.kcal ? fmt0(t.kcal) : '&mdash;'}</td>` +
      `<td${z}>${t.kcal ? fmt(t.carbs_g) : '&mdash;'}</td>` +
      `<td${z}>${t.kcal ? fmt(t.protein_g) : '&mdash;'}</td>` +
      `<td${z}>${t.kcal ? fmt(t.fat_g) : '&mdash;'}</td>` +
      `</tr>`;
  }

  const n = daysToImport().length;
  body +=
    `<tr class="total"><td>Week &middot; ${n} ${n === 1 ? 'day' : 'days'}</td>` +
    `<td>${week.items}</td><td>${fmt0(week.kcal)}</td>` +
    `<td>${fmt(week.carbs_g)}</td><td>${fmt(week.protein_g)}</td>` +
    `<td>${fmt(week.fat_g)}</td></tr>`;

  table.innerHTML = `<thead>${head}</thead><tbody>${body}</tbody>`;
}

function renderConfirm() {
  const n = daysToImport().length;
  const open = conflicts().length;
  const btn = $('#import-btn');
  btn.disabled = n === 0 || state.busy;
  // The button says what it will do, including what it will not do.
  btn.textContent = state.busy
    ? 'Importing…'
    : n === 0
    ? 'Nothing to import'
    : open
    ? `Import ${n} ${n === 1 ? 'day' : 'days'} · skip ${open} unresolved`
    : `Import ${n} ${n === 1 ? 'day' : 'days'}`;
}

/* --- editing ------------------------------------------------------------ */

function moveRow(id, toDate) {
  const row = state.rows.find((r) => r.id === id);
  if (!row || row.date === toDate) return;
  const from = row.date;
  row.date = toDate;
  if (!state.days.includes(toDate)) {
    state.days.push(toDate);
    state.breakfast[toDate] = true;
  }
  render();

  if (state.importedDays.has(toDate)) {
    setState(
      `Moved ${from} → ${toDate}. That day already has imported entries; ` +
        `duplicates will be reported as skipped.`,
      'warn'
    );
  } else {
    setState(`Moved ${from} → ${toDate}.`);
  }
}

function wireDropTarget(el, date) {
  el.addEventListener('dragover', (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    el.classList.add('dropping');
    // Warn before the drop lands, not after.
    if (state.importedDays.has(date)) el.classList.add('warn-drop');
  });
  el.addEventListener('dragleave', () => {
    el.classList.remove('dropping', 'warn-drop');
  });
  el.addEventListener('drop', (e) => {
    e.preventDefault();
    el.classList.remove('dropping', 'warn-drop');
    const id = e.dataTransfer.getData('text/plain');
    if (id) moveRow(id, date);
  });
}

function addDayColumn() {
  const last = state.days.length ? state.days[state.days.length - 1] : null;
  const next = last ? addDays(last, 1) : new Date().toISOString().slice(0, 10);
  if (state.days.includes(next)) return;
  state.days.push(next);
  state.breakfast[next] = true;
  render();
  setState(`Added ${next}.`);
}

/* --- import ------------------------------------------------------------- */

function buildPayload() {
  // Exactly what the server will write. No original text is sent.
  return {
    decisions: state.decisions,
    days: daysToImport().map((date) => ({
      date,
      breakfast: breakfastOn(date),
      rows: rowsOn(date).map((r) => ({
        name: r.name,
        normalized_name: r.normalized_name,
        code: r.code,
        kcal: r.kcal,
        carbs_g: r.carbs_g,
        protein_g: r.protein_g,
        fat_g: r.fat_g,
      })),
    })),
  };
}

async function doImport() {
  if (state.busy) return;
  const payload = buildPayload();
  if (!payload.days.length) return;

  state.busy = true;
  renderConfirm();
  setState('Writing to Cronometer…');

  let data;
  try {
    const res = await fetch('/api/import', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    });
    data = await res.json();
    if (!res.ok) {
      setState(data.detail || `Import failed (HTTP ${res.status}).`, 'err');
      state.busy = false;
      renderConfirm();
      return;
    }
  } catch (e) {
    setState('Could not reach the server.', 'err');
    state.busy = false;
    renderConfirm();
    return;
  }

  state.busy = false;
  renderResults(data);
  // Re-read history so the days just written immediately show as imported,
  // and a second Ctrl+Enter is visibly a no-op rather than a silent duplicate.
  await loadHistory();
  if (data.resolutions) {
    state.resolutions = {};
    for (const r of data.resolutions) state.resolutions[r.normalized_name] = r;
  }
  render();
}

function renderResults(data) {
  const box = $('#results');
  box.innerHTML = '';
  for (const e of data.entries || []) {
    const el = document.createElement('div');
    el.className = 'r ' + e.status;
    el.innerHTML = `<span></span><span class="st"></span><span class="msg"></span>`;
    el.children[0].textContent = e.date;
    el.children[1].textContent = e.status;
    el.children[2].textContent = e.detail ? `${e.name} — ${e.detail}` : e.name;
    box.appendChild(el);
  }
  $('#results-pane').hidden = false;

  // The result state says what happened.
  const parts = [`Imported ${data.days_written} ${data.days_written === 1 ? 'day' : 'days'}`];
  if (data.skipped) parts.push(`${data.skipped} skipped`);
  if (data.failed) parts.push(`${data.failed} failed`);
  setState(parts.join(' · '), data.failed ? 'err' : data.skipped ? 'warn' : 'ok');
}

/* --- wiring ------------------------------------------------------------- */

const SAMPLE = `2026-08-31 Hétfő
ED1 1 adag Könnyű tejfölös zöldbableves*: 1095 FT (1095 FT)
(174kcal, 19.9g szénh., 5.2g fehérje, 6.8g zsír)
ED10 1 adag Óvári sertésborda (sonka, gomba, sajt), tepsis burgonyával, bébirépával: 1940 FT (1940 FT)
(697kcal, 62.4g szénh., 45.2g fehérje, 24.7g zsír)
--------------------------------------------------------------
2026-09-01 Kedd
ED1 1 adag Marhahúsleves zöldségekkel gazdagon: 1395 FT (1395 FT)
(193kcal, 10.6g szénh., 21.2g fehérje, 6.4g zsír)
--------------------------------------------------------------
2026-09-02 Szerda
ED3 1 adag Csirkemell rizzsel, párolt zöldségekkel: 1 640 FT (1 640 FT)
(612kcal, 71.2g szénh., 38.9g fehérje, 18.4g zsír)
--------------------------------------------------------------
2026-09-03 Csütörtök
ED1 1 adag Gulyásleves*: 1295 FT (1295 FT)
(286kcal, 22.4g szénh., 18.1g fehérje, 13.5g zsír)
ED7 1 adag Rakott krumpli (tojás, kolbász, tejföl): 1790 FT (1790 FT)
(534kcal, 41.3g szénh., 22.7g fehérje, 30.2g zsír)
--------------------------------------------------------------
2026-09-04 Péntek
ED1 2 adag Húsleves sovány pulykamellből: 1355 FT (2710 FT)
(191kcal, 15g szénh., 26.6g fehérje, 1.3g zsír)
`;

async function boot() {
  try {
    const res = await fetch('/api/config');
    state.config = await res.json();
  } catch (e) {
    /* keep defaults */
  }

  $('#parse-btn').addEventListener('click', doParse);
  $('#import-btn').addEventListener('click', doImport);
  $('#add-day-btn').addEventListener('click', addDayColumn);
  $('#sample-btn').addEventListener('click', () => {
    $('#paste').value = SAMPLE;
    doParse();
  });
  $('#clear-btn').addEventListener('click', () => {
    $('#paste').value = '';
    $('#preview-pane').hidden = true;
    $('#results-pane').hidden = true;
    $('#confirm-bar').hidden = true;
    $('#issues').hidden = true;
    state.parsed = false;
    $('#paste').focus();
  });

  // Paste then Enter should parse; Cmd/Ctrl+Enter confirms the import.
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      if (state.parsed) doImport();
      else doParse();
    }
  });

  $('#paste').addEventListener('paste', () => {
    // Let the value settle, then parse: paste is the whole interaction.
    setTimeout(doParse, 0);
  });

  $('#paste').focus();
}

boot();
