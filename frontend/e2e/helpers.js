// Shared steps for the e2e suite. Setup goes through the API (each step was first proved in
// the browser by an earlier milestone, and has its own test here); every check compares the
// page with the API's own reply. Synthetic data only.
const fs = require('node:fs');
const path = require('node:path');
const { expect } = require('@playwright/test');

const ROOT = path.resolve(__dirname, '..', '..');
const FIXTURES = {
  ads: path.join(__dirname, 'fixtures', 'ads_messy.csv'),          // F3: 11 rows, 1 duplicate
  keywords: path.join(__dirname, 'fixtures', 'gsc_kw.csv'),        // F6: the gold keyword set
  logistics: path.join(ROOT, 'backend', 'tests', 'fixtures', 'logistics_sla.csv'),
  multiheader: path.join(ROOT, 'backend', 'tests', 'fixtures', 'multiheader.csv'),
};
const T = { timeout: 90_000 };

const app = (p) => process.env.E2E_APP + p;

async function call(method, p, body) {
  const init = { method };
  if (body !== undefined) {
    init.headers = { 'content-type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  const response = await fetch(process.env.E2E_API + p, init);
  return { status: response.status, body: await response.json().catch(() => null) };
}

async function get(p) {
  for (let attempt = 1; ; attempt += 1) {
    const r = await call('GET', p);
    if (r.status < 400) return r.body;
    // Two requests on one workspace at once can answer 500 (issue #24): a GET is safe to
    // retry, twice; a 500 that persists still fails the test. POSTs are never retried.
    if (r.status >= 500 && attempt < 3) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      continue;
    }
    throw new Error(`GET ${p} → ${r.status} ${JSON.stringify(r.body)}`);
  }
}

async function post(p, body = {}) {
  const r = await call('POST', p, body);
  if (r.status >= 400) throw new Error(`POST ${p} → ${r.status} ${JSON.stringify(r.body)}`);
  return r.body;
}

async function upload(ws, file, name = path.basename(file)) {
  const form = new FormData();
  form.append('file', new Blob([fs.readFileSync(file)]), name);
  const response = await fetch(`${process.env.E2E_API}/workspaces/${ws}/uploads`, { method: 'POST', body: form });
  const body = await response.json();
  if (!response.ok) throw new Error(`upload ${name} → ${response.status} ${JSON.stringify(body)}`);
  return body.dataset_id;
}

async function session() {
  const { sid } = await post('/sessions', {});
  const s = await get(`/sessions/${sid}`);
  return { sid, ws: s.workspace_id };
}

// What a person's tab would have saved: the page, a name and the active dataset.
async function remember(sid, page, label, datasetId) {
  const { version } = await get(`/sessions/${sid}`);
  const r = await call('PUT', `/sessions/${sid}/ui-state`, { version, ui_state: {
    schema: 2, page, label, dataset_id: datasetId ?? null,
    datasets: datasetId ? [datasetId] : [], drafts: {} } });
  if (r.status >= 400) throw new Error(`PUT ui-state → ${r.status} ${JSON.stringify(r.body)}`);
}

const answerForks = (proposal) => Object.fromEntries(
  proposal.forks.map((f) => [f.fork_id, f.suggested || f.options[0].id]));

// The ads file cleaned, marketing confirmed, a contract in force (F3 did this in the browser).
async function adsReady(page = 'tools') {
  const { sid, ws } = await session();
  const ds = await upload(ws, FIXTURES.ads);
  const plan = (await get(`/datasets/${ds}/cleaning/proposals`)).proposals;
  await post(`/datasets/${ds}/cleaning/approve`, { approve: plan.map((p) => p.action_id), reject: [] });
  await post(`/datasets/${ds}/domains/confirm`, { domains: ['marketing'] });
  const proposal = await get(`/datasets/${ds}/contract/proposal`);
  const sums = ['cost', 'clicks', 'impressions', 'conversions', 'revenue'];
  await post(`/datasets/${ds}/contract/confirm`, { contract: {
    grain: 'one row per campaign per day', primary_key: ['date', 'campaign'], date_column: 'date',
    measures: [...sums, 'ctr'], dimensions: ['campaign', 'channel', 'utm_source'],
    aggregations: { ...Object.fromEntries(sums.map((m) => [m, 'sum'])), ctr: 'none' },
    measure_definitions: Object.fromEntries([...sums, 'ctr'].map((m) => [m, `${m} as the platform reports it`])),
    analysis_window_start: '2026-08-01', analysis_window_end: '2026-09-30', caveats: [] },
    fork_choices: answerForks(proposal) });
  await remember(sid, page, 'e2e ads', ds);
  return { sid, ws, ds };
}

// The logistics fixture cleaned, logistics confirmed, a contract, metrics approved (F8).
async function logisticsReady(metrics = ['sla_breach'], page = 'explore') {
  const { sid, ws } = await session();
  const ds = await upload(ws, FIXTURES.logistics);
  const plan = (await get(`/datasets/${ds}/cleaning/proposals`)).proposals;
  await post(`/datasets/${ds}/cleaning/approve`, { approve: plan.map((p) => p.action_id), reject: [] });
  await post(`/datasets/${ds}/domains/confirm`, { domains: ['logistics'] });
  const proposal = await get(`/datasets/${ds}/contract/proposal`);
  const measures = ['order_value_inr', 'distance_km', 'package_weight_kg', 'recorded_delivery_minutes',
                    'delivery_cost_inr'];
  const dims = ['hub', 'zone', 'rider_id', 'payment_mode', 'delivery_status', 'attempt_count',
                'address_quality', 'weather', 'source_system', 'promised_minutes'];
  await post(`/datasets/${ds}/contract/confirm`, { contract: {
    grain: 'one row = one order', primary_key: [], date_column: 'order_date', measures, dimensions: dims,
    aggregations: Object.fromEntries(measures.map((m) => [m, m.endsWith('_inr') ? 'sum' : 'mean'])),
    measure_definitions: Object.fromEntries(measures.map((m) => [m, m.replaceAll('_', ' ')])),
    analysis_window_start: '2026-08-01', analysis_window_end: '2026-08-31',
    caveats: ['August only; the courier export was late'] },
    fork_choices: Object.fromEntries(proposal.forks.map((f) => [f.fork_id, f.options[0].id])) });
  for (const t of metrics) await post(`/datasets/${ds}/metrics/approve`, { template_id: t, bindings: {}, fork_choices: {} });
  await remember(sid, page, 'e2e logistics', ds);
  return { sid, ws, ds };
}

// The keyword file with marketing and a contract (F6).
async function keywordsReady() {
  const { sid, ws } = await session();
  const ds = await upload(ws, FIXTURES.keywords);
  await post(`/datasets/${ds}/domains/confirm`, { domains: ['marketing'] });
  const proposal = await get(`/datasets/${ds}/contract/proposal`);
  await post(`/datasets/${ds}/contract/confirm`, { contract: {
    grain: 'one row per query', primary_key: ['row_id'], date_column: 'date',
    measures: ['clicks', 'impressions', 'position'], dimensions: ['query', 'page'],
    aggregations: { clicks: 'sum', impressions: 'sum', position: 'mean' },
    measure_definitions: { clicks: 'clicks as reported', impressions: 'impressions as reported',
                           position: 'average position' },
    analysis_window_start: '2026-09-01', analysis_window_end: '2026-09-30', caveats: [] },
    fork_choices: answerForks(proposal) });
  await remember(sid, 'keywords', 'e2e keywords', ds);
  return { sid, ws, ds };
}

// --- the page ----------------------------------------------------------------------------

const sidebar = (page, name) => page.locator('[data-testid="stSidebar"]').getByRole('link', { name, exact: true });
// A keyed Streamlit element carries the class st-key-<key> (dots become dashes).
const keyed = (page, k) => page.locator(`.st-key-${k.replaceAll('.', '-')}`);

// Bind to the DOM node first: an open selectbox leaves the role/name match, so a positional
// locator would move to the next box mid-action (F3).
async function choose(page, combobox, option) {
  const box = typeof combobox === 'string'
    ? page.getByRole('combobox', { name: combobox, exact: true }) : combobox;
  for (let attempt = 1; ; attempt += 1) {
    await box.waitFor(T);
    const el = await box.elementHandle();
    try {
      await el.click();
      await el.fill(option);
      await page.getByRole('option', { name: option, exact: true }).first().waitFor({ timeout: 10_000 });
      await el.press('Enter');
      await page.waitForTimeout(1200);
      return;
    } catch (error) {
      // A rerun from the previous answer can redraw the box and close its list mid-choice
      // (1 in 51 runs, F11). Like a person: close it and choose again, twice at most. The
      // tests check every choice against the API afterwards.
      if (attempt === 3) throw error;
      await page.keyboard.press('Escape');
      await page.waitForTimeout(1500);
    }
  }
}

// Type, then Enter: Streamlit saves a text field on Enter or blur (F2).
async function type(page, locator, value) {
  await locator.fill(value);
  await locator.press('Enter');
  await page.waitForTimeout(900);
}

async function open(page, route, sid) {
  await page.goto(app(`/${route}?sid=${sid}`));
  await expect(page.locator('h1').first()).toBeVisible(T);
}

// A Streamlit tick box: click its label, as a person does (a role click can be reverted, F10).
async function tick(page, k) {
  await keyed(page, k).locator('label').first().click();
  await page.waitForTimeout(1200);
}

// Streamlit 1.64's multiselect (react-aria): find it by its box; each chip is a [data-tag]
// labelled with its value.
const multiselect = (page, name) => page.locator('[data-testid="stMultiSelect"]')
  .filter({ has: page.getByRole('combobox', { name, exact: true }) });
const chips = (field) => field.locator('[data-tag]').evaluateAll((els) => els.map((e) => e.getAttribute('aria-label')));

const text = async (page, re) => (await page.getByText(re).first().innerText()).trim();

// Poll the API until `check` holds (a save or an action lands just after the page shows it).
async function eventually(fn, check, seconds = 30) {
  const deadline = Date.now() + seconds * 1000;
  let last;
  while (Date.now() < deadline) {
    last = await fn();
    if (check(last)) return last;
    await new Promise((r) => setTimeout(r, 500));
  }
  return last;
}

module.exports = {
  FIXTURES, T, app, call, get, post, upload, session, remember, adsReady, logisticsReady,
  keywordsReady, sidebar, keyed, choose, type, open, tick, multiselect, chips, text, eventually,
};
