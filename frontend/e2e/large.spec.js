// A large file through the browser: generated ad rows (E2E_LARGE_ROWS, default 300,000),
// uploaded, read and profiled. The page's figures are the API's; each step's time is recorded
// as an annotation (shown in the report) and printed.
const fs = require('node:fs');
const { test, expect } = require('@playwright/test');
const h = require('./helpers');

const ROWS = Number(process.env.E2E_LARGE_ROWS || 300_000);

function write(file, rows) {
  const out = fs.openSync(file, 'w');
  const channels = ['search', 'social', 'video', 'display'];
  let seed = 7;
  const next = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed; };
  fs.writeSync(out, 'date,campaign,channel,cost,clicks,impressions\n');
  const day0 = Date.UTC(2025, 0, 1);
  let chunk = [];
  for (let i = 0; i < rows; i += 1) {
    const day = new Date(day0 + (i % 640) * 86_400_000).toISOString().slice(0, 10);
    const impressions = 500 + (next() % 5000);
    const clicks = next() % 200;
    chunk.push(`${day},campaign_${i % 50},${channels[i % 4]},${(next() % 90_000) / 100},${clicks},${impressions}`);
    if (chunk.length === 10_000) { fs.writeSync(out, chunk.join('\n') + '\n'); chunk = []; }
  }
  if (chunk.length) fs.writeSync(out, chunk.join('\n') + '\n');
  fs.closeSync(out);
  return fs.statSync(file).size;
}

test(`a ${ROWS.toLocaleString('en')}-row file uploads, reads and profiles as the API reports`, async ({ page }, info) => {
  test.setTimeout(600_000);
  const file = info.outputPath('large_ads.csv');
  const bytes = write(file, ROWS);
  const timing = (what, ms) => {
    info.annotations.push({ type: 'timing', description: `${what}: ${(ms / 1000).toFixed(1)} s` });
    console.log(`large file (${ROWS} rows, ${(bytes / 1e6).toFixed(1)} MB): ${what} ${(ms / 1000).toFixed(1)} s`);
  };
  const { sid } = await h.session();
  await h.open(page, 'data', sid);
  await expect(page.getByText('200MB per file', { exact: false })).toBeVisible(h.T);   // Streamlit's cap

  let t = Date.now();
  await page.locator('input[type="file"]').setInputFiles(file);
  await page.getByRole('button', { name: 'Upload', exact: true }).click();
  await expect(page.getByText('Loaded large_ads.csv.')).toBeVisible({ timeout: 480_000 });
  timing('upload, read and load', Date.now() - t);

  t = Date.now();
  await expect(page.getByText(`${ROWS} rows. Values as the service reports them.`)).toBeVisible({ timeout: 300_000 });
  timing('profile shown', Date.now() - t);
  const ds = (await h.eventually(() => h.get(`/sessions/${sid}`), (s) => s.ui_state.dataset_id)).ui_state.dataset_id;
  const record = await h.get(`/datasets/${ds}`);
  expect(record.rows).toBe(ROWS);
  await expect(page.locator('[data-testid="stMetricValue"]')).toHaveText([String(ROWS), '6']);

  t = Date.now();
  await h.sidebar(page, 'Clean').click();
  await expect(page.getByText(/Tick the steps you approve|The engine proposes no cleaning/)).toBeVisible({ timeout: 300_000 });
  timing('cleaning proposals shown', Date.now() - t);
  expect(await page.locator('[data-testid="stException"]').count()).toBe(0);
});
