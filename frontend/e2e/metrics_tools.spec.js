// F4 and F7: approve a metric and validity rules, run a tool, download its figures.
const fs = require('node:fs');
const { test, expect } = require('@playwright/test');
const h = require('./helpers');

// RFC 4180, as Python's csv module writes it.
function parseCsv(textIn) {
  const rows = [];
  let row = [], cell = '', quoted = false;
  for (let i = 0; i < textIn.length; i += 1) {
    const c = textIn[i];
    if (quoted) {
      if (c === '"' && textIn[i + 1] === '"') { cell += '"'; i += 1; }
      else if (c === '"') quoted = false;
      else cell += c;
    } else if (c === '"') quoted = true;
    else if (c === ',') { row.push(cell); cell = ''; }
    else if (c === '\n') { row.push(cell); rows.push(row); row = []; cell = ''; }
    else cell += c;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  return rows;
}

test('a metric and the suggested rules are approved; a tool run and its CSV are the API\'s figures', async ({ page }) => {
  test.slow();
  const { sid, ds } = await h.adsReady('metrics');
  await h.open(page, 'metrics', sid);
  await expect(page.getByText('Validity rules')).toBeVisible(h.T);
  expect((await h.get(`/datasets/${ds}/metrics/templates`)).templates.some((t) => t.approved)).toBe(false);

  await h.keyed(page, `metrics.${ds}.ctr.approve`).getByRole('button').click();
  await expect(page.getByText(/^Approved: measure/).first()).toBeVisible(h.T);
  const ctr = (await h.get(`/datasets/${ds}/metrics/templates`)).templates.find((t) => t.template_id === 'ctr');
  expect(ctr.approved).toBe(true);

  const rules = (await h.get(`/datasets/${ds}/validity-rules`)).rules;
  const suggested = rules.filter((r) => r.suggested).map((r) => r.rule_id).sort();
  await page.getByRole('button', { name: new RegExp(`^Tick the ${suggested.length} suggested`) }).click();
  await page.waitForTimeout(1200);
  await page.getByRole('button', { name: /^Apply \(/ }).click();
  await expect(page.getByText(/^Saved\. Approved now:/)).toBeVisible(h.T);
  const approved = (await h.get(`/datasets/${ds}/validity-rules`)).rules.filter((r) => r.approved);
  expect(approved.map((r) => r.rule_id).sort()).toEqual(suggested);

  await h.sidebar(page, 'Tools').click();
  const tool = (await h.get('/packs/marketing')).pack.tools.find((t) => t.id === 'marketing.channel_efficiency');
  await h.choose(page, 'Tool', tool.ui_label);
  await page.getByRole('button', { name: 'Run', exact: true }).click();
  await expect(page.getByText(/^computed on /).first()).toBeVisible(h.T);
  const rid = (await h.text(page, /^computed on /)).match(/result (r_[0-9a-f]+)/)[1];
  const stored = await h.get(`/results/${rid}`);
  const direct = await h.post(`/tools/${tool.id}/run`, { dataset_id: ds, params: {} });
  expect(stored.figures).toEqual(direct.figures);                      // the same run, twice
  await expect(page.getByText(`Every figure (${stored.figures.length}), with its source`)).toBeVisible();
  expect(await h.text(page, /^Validity rules applied:/))
    .toBe(`Validity rules applied: ${stored.validity_filters_applied.join(', ') || 'none approved'}`);

  await page.getByText(`Every figure (${stored.figures.length}), with its source`).click();
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: 'Download every figure (CSV)' }).click(),
  ]);
  expect(download.suggestedFilename()).toBe(`${tool.id}-figures.csv`);
  const rows = parseCsv(fs.readFileSync(await download.path(), 'utf8'));
  expect(rows[0]).toEqual(['figure', 'value', 'unit', 'source']);
  expect(rows.length - 1).toBe(stored.figures.length);
  stored.figures.forEach((f, i) => {
    const [name, value, unit, source] = rows[i + 1];
    expect([name, unit, source]).toEqual([f.name, f.unit || '', f.provenance]);
    if (f.value === null) expect(value).toBe('suppressed');
    else expect(Number(value)).toBe(f.value);                           // the value, not reformatted
  });
});
