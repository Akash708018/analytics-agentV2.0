// F10: API 0.8.0/0.9.0 on the screens: upload answers, the contract in force, cleaning losses,
// declared optional params, Explore and reports.
const { test, expect } = require('@playwright/test');
const h = require('./helpers');

test('a refused upload is answered from a blank form with the reader\'s guess, and loads', async ({ page }) => {
  test.slow();
  const { sid } = await h.session();
  const probe = await h.session();                    // the same file through the API, elsewhere
  const form = new FormData();
  form.append('file', new Blob([require('node:fs').readFileSync(h.FIXTURES.multiheader)]), 'multiheader.csv');
  const refusal = await (await fetch(`${process.env.E2E_API}/workspaces/${probe.ws}/uploads`, { method: 'POST', body: form })).json();
  expect(refusal.error.code).toBe('ingest_needs_answers');
  const { guess } = refusal.preview;

  await h.open(page, 'data', sid);
  await page.locator('input[type="file"]').setInputFiles(h.FIXTURES.multiheader);
  await page.getByRole('button', { name: 'Upload', exact: true }).click();
  await expect(page.getByText('multiheader.csv is not loaded yet: the reader needs your answers about its layout.')).toBeVisible(h.T);
  for (const q of refusal.questions) await expect(page.getByText(q, { exact: true })).toBeVisible();
  await expect(page.getByText(`The first ${refusal.preview.rows.length} rows as the file holds them, numbered as in the file.`)).toBeVisible();
  const headerRows = h.multiselect(page, 'Rows that form the header');
  const firstRow = page.getByRole('spinbutton', { name: 'First data row' });
  const name = page.getByRole('textbox', { name: 'Dataset name' });
  await expect.poll(() => h.chips(headerRows)).toEqual([]);           // nothing assumed
  await expect(firstRow).toHaveValue('');
  await expect(name).toHaveValue('');

  await page.getByRole('button', { name: "Use the reader's guess", exact: true }).click();
  await expect(firstRow).toHaveValue(String(guess.data_start_row), h.T);
  await expect.poll(() => h.chips(headerRows)).toEqual(guess.header_rows.map(String));
  await expect(name).toHaveValue(guess.name);

  await page.getByRole('button', { name: 'Load with these answers', exact: true }).click();
  await expect(page.getByText('Loaded multiheader.csv with your answers.')).toBeVisible(h.T);
  const ds = (await h.eventually(() => h.get(`/sessions/${sid}`), (s) => s.ui_state.dataset_id)).ui_state.dataset_id;
  const record = await h.get(`/datasets/${ds}`);
  expect(record.name).toBe(guess.name);
  await expect(page.locator('[data-testid="stMetricValue"]'))
    .toHaveText([String(record.rows), String(record.columns)]);
});

test('Clean shows what each step loses, its examples and its SQL, as proposed', async ({ page }) => {
  const { sid, ws } = await h.session();
  const ds = await h.upload(ws, h.FIXTURES.logistics);
  await h.remember(sid, 'clean', 'e2e losses', ds);
  const plan = (await h.get(`/datasets/${ds}/cleaning/proposals`)).proposals;
  await h.open(page, 'clean', sid);
  await expect(page.getByText(/^Loses \d+ /))
    .toHaveText(plan.filter((p) => p.values_lost).map((p) => `Loses ${p.values_lost} ${p.loss_unit}(s).`), h.T);
  const values = plan.find((p) => (p.samples || []).some((s) => 'value' in s));
  await expect(page.getByText(`Values it changes (examples): ${values.samples.map((s) => s.value).join(', ')}`).first()).toBeVisible();
  await page.getByText('SQL', { exact: true }).first().click();
  await expect(page.locator('[data-testid="stCode"]').first()).toHaveText(plan[0].sql, h.T);
});

test('the contract in force is shown as the API holds it, declared and counted caveats apart', async ({ page }) => {
  const { sid, ds } = await h.logisticsReady(['sla_breach'], 'contract');
  const inForce = await h.get(`/datasets/${ds}/contract`);
  await h.open(page, 'contract', sid);
  const label = `The contract in force: version ${inForce.version}, confirmed ${inForce.confirmed_at}`;
  await page.getByText(label).click();
  const panel = page.locator('[data-testid="stExpander"]').filter({ hasText: label });
  await expect(panel.getByText(`One row: ${inForce.grain}`)).toBeVisible(h.T);
  for (const c of inForce.caveats) await expect(panel.getByText(`• ${c}`, { exact: true })).toBeVisible();
  await expect(panel.getByText(/^• /)).toHaveCount(inForce.caveats.length + inForce.measured_caveats.length);
  const metrics = Object.entries(inForce.metrics).map(([t, m]) => `${t} → ${m}`).join(', ');
  await expect(panel.getByText(`Approved metrics: ${metrics}`)).toBeVisible();
});

test('a declared optional param shows the pack\'s help, starts blank, and reaches the engine', async ({ page }) => {
  test.slow();
  const { sid, ds } = await h.logisticsReady(['sla_breach'], 'tools');
  const stuck = (await h.get('/packs/logistics')).pack.tools.find((t) => t.id === 'logistics.stuck_shipments');
  const days = stuck.params_optional.find((p) => p.name === 'days');
  await h.open(page, 'tools', sid);
  await h.choose(page, 'Tool', stuck.ui_label);
  const box = page.getByRole('spinbutton', { name: 'Days (optional)' });
  await expect(box).toHaveValue('', h.T);
  await page.locator('[data-testid="stNumberInput"]').filter({ hasText: 'Days (optional)' })
    .locator('[data-testid="stTooltipIcon"]').hover();
  await expect(page.locator('[data-testid="stTooltipContent"]').first())
    .toHaveText(`${days.help}. Blank: the tool uses ${days.default}.`, h.T);
  await h.type(page, page.getByRole('textbox', { name: 'As of' }), '2026-08-31');
  await h.type(page, box, '5');
  await page.getByRole('button', { name: 'Run', exact: true }).click();
  await expect(page.getByText(/^computed on /).first()).toBeVisible(h.T);
  const rid = (await h.text(page, /^computed on /)).match(/result (r_[0-9a-f]+)/)[1];
  const stored = await h.get(`/results/${rid}`);
  expect(stored.tool_id).toBe(stuck.id);
  expect(stored.caveats).toContain('Open 5+ days before 2026-08-31 (the as-of date you entered).');
  await expect(page.getByText('• Open 5+ days before 2026-08-31 (the as-of date you entered).')).toBeVisible();
  expect(ds).toBeTruthy();
});

test('an Explore run and a report are the API\'s own', async ({ page }) => {
  test.slow();
  const { sid, ds } = await h.logisticsReady(['sla_breach'], 'explore');
  // Read before the page loads: two requests on one workspace at once can answer 500 (#24).
  const listed = (await h.get(`/datasets/${ds}/analyses`)).analyses;
  const playbook = (await h.get('/packs/logistics')).pack.playbooks.find((b) => b.id === 'sla_where_and_why');
  await h.open(page, 'explore', sid);
  await expect(page.getByText(`${listed.length} analyses, grouped by what they answer.`, { exact: false })).toBeVisible(h.T);
  await expect(page.getByRole('combobox', { name: 'Analysis', exact: true })).toHaveValue('');
  await h.choose(page, 'Analysis', 'Comparative · group compare');
  await h.choose(page, 'Dimension', 'hub');
  await h.choose(page, 'Measure', 'order_value_inr');
  await page.getByRole('button', { name: 'Run the analysis', exact: true }).click();
  await expect(page.getByText(/^computed on /).first()).toBeVisible(h.T);
  const rid = (await h.text(page, /^computed on /)).match(/result (r_[0-9a-f]+)/)[1];
  const shown = await h.get(`/results/${rid}`);
  const direct = await h.post('/tools/core.group_compare/run',
                              { dataset_id: ds, params: { dimension: 'hub', measure: 'order_value_inr' } });
  expect(shown.figures).toEqual(direct.figures);
  await expect(page.getByText(`Every figure (${shown.figures.length}), with its source`)).toBeVisible();

  await h.choose(page, 'Playbook', `${playbook.description} (logistics)`);
  await page.getByRole('button', { name: 'Build the report', exact: true }).click();
  await expect(page.getByText(/^sla_where_and_why · report rep_/)).toBeVisible(h.T);
  const report = await h.post(`/datasets/${ds}/reports`, { playbook: playbook.id, slots: {} });
  await expect(page.getByText(new RegExp(`^logistics\\.\\w+ · ${ds}$`)))
    .toHaveText(report.results.map((r) => `${r.tool_id} · ${ds}`));
  await expect(page.getByText(/^Skipped /))
    .toHaveText(report.skipped.map((s) => `Skipped ${s.tool_id}: ${s.reason} (${s.code})`));
  await expect(page.getByText(`Rules this playbook holds its reading to: ${report.rules.join(', ')}`)).toBeVisible();
});

test('without a contract or a confirmed domain, Explore says what comes first', async ({ page }) => {
  const { sid, ws } = await h.session();
  const ds = await h.upload(ws, h.FIXTURES.logistics);
  await h.remember(sid, 'explore', 'e2e nothing yet', ds);
  await h.open(page, 'explore', sid);
  await expect(page.getByText("Analyses run under the dataset's contract: confirm it first.")).toBeVisible(h.T);
  await expect(page.getByText('Reports come from the playbooks of a confirmed domain: confirm one on Domain.')).toBeVisible();
  expect((await h.call('GET', `/datasets/${ds}/analyses`)).status).toBe(409);
});
