// F8: a result's lineage, and every row of it on Results, as the API stores them.
const { test, expect } = require('@playwright/test');
const h = require('./helpers');

const lineage = (r) => [`computed on ${r.snapshot.rows} rows (snapshot ${r.snapshot.hash})`,
  `contract v${r.contract_version}`, `grain: ${r.grain}`,
  `metrics: ${Object.entries(r.metrics_used).map(([t, m]) => `${t} → ${m.measure}`).join(', ')}`,
  `result ${r.result_id}`].join(' · ');

test('a tool result shows its lineage, and its rows on Results are the inspect reply', async ({ page }) => {
  test.slow();
  const { sid, ds } = await h.logisticsReady(['sla_breach'], 'tools');
  await h.open(page, 'tools', sid);
  const tool = (await h.get('/packs/logistics')).pack.tools.find((t) => t.id === 'logistics.sla_compliance');
  await h.choose(page, 'Tool', tool.ui_label);
  await h.choose(page, 'By (optional)', 'hub');
  await page.getByRole('button', { name: 'Run', exact: true }).click();
  await expect(page.getByText(/^computed on /).first()).toBeVisible(h.T);
  const shown = await h.text(page, /^computed on /);
  const listed = (await h.get(`/datasets/${ds}/results`)).results;
  const stored = await h.get(`/results/${listed[listed.length - 1].result_id}`);
  expect(shown).toBe(lineage(stored));

  await page.getByRole('link', { name: 'Page through every row on Results' }).first().click();
  await expect(page.getByText('Every row', { exact: true })).toBeVisible(h.T);
  await expect(page.getByText(/^Rows \d+–\d+ of \d+/)).toBeVisible(h.T);
  const rows = await h.get(`/results/${stored.result_id}/inspect`);
  expect(await h.text(page, /^Rows \d+–\d+ of \d+/)).toContain(`of ${rows.total_rows}`);
  expect(await h.text(page, /^Rows \d+–\d+ of \d+/))
    .toContain(`${rows.rows_kept} kept with the result of the engine's ${rows.rows_in_engine_output}`);
  const results = (await h.get(`/datasets/${ds}/results`)).results;
  await expect(page.getByText(`${results.length} stored result(s) · ${results.filter((r) => r.stale).length} out of date.`)).toBeVisible();
});
