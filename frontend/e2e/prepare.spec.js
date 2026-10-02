// F3 (with F10's additions): upload → clean → domain → contract, all in the browser.
const { test, expect } = require('@playwright/test');
const h = require('./helpers');

const SUMS = ['cost', 'clicks', 'impressions', 'conversions', 'revenue'];

test('a file is uploaded, cleaned, its domain and contract confirmed, as the API records', async ({ page }) => {
  test.slow();
  const { sid } = await h.session();
  await h.open(page, 'data', sid);

  // Data: upload in the browser; the record shown is the API's.
  await page.locator('input[type="file"]').setInputFiles(h.FIXTURES.ads);
  await page.getByRole('button', { name: 'Upload', exact: true }).click();
  await expect(page.getByText('Loaded ads_messy.csv.')).toBeVisible(h.T);
  await expect(page.getByText('What each column holds')).toBeVisible(h.T);
  // The run saves after it draws, so the page can show the upload before the save lands.
  const ds = (await h.eventually(() => h.get(`/sessions/${sid}`), (s) => s.ui_state.dataset_id))
    .ui_state.dataset_id;
  const record = await h.get(`/datasets/${ds}`);
  await expect(page.locator('[data-testid="stMetricValue"]'))
    .toHaveText([String(record.rows), String(record.columns)]);

  // Clean: what each step loses is the API's; nothing is ticked until the person ticks.
  const plan = (await h.get(`/datasets/${ds}/cleaning/proposals`)).proposals;   // before the page reads it
  await h.sidebar(page, 'Clean').click();
  await expect(page.getByText('Tick the steps you approve')).toBeVisible(h.T);
  // Streamlit draws a page in steps: assertions on lists retry until the whole list is there.
  await expect(page.getByText(/^Loses \d+ /))
    .toHaveText(plan.filter((p) => p.values_lost).map((p) => `Loses ${p.values_lost} ${p.loss_unit}(s).`));
  expect(await page.locator('input[type="checkbox"]:checked').count()).toBe(0);
  const suggested = plan.filter((p) => p.suggested);
  const dupes = plan.find((p) => p.kind === 'DROP_DUPLICATE_ROWS');
  await page.getByRole('button', { name: `Tick the ${suggested.length} suggested` }).click();
  await page.waitForTimeout(1000);
  await page.getByText(dupes.description, { exact: true }).click();
  await page.waitForTimeout(1200);
  const applied = [dupes, ...suggested].map((p) => p.action_id).sort();
  await page.getByRole('button', { name: new RegExp(`^Apply ${applied.length} step`) }).click();
  await expect(page.getByText(new RegExp(`^Applied ${applied.join(', ')}`))).toBeVisible(h.T);
  const after = (await h.get(`/datasets/${ds}/cleaning/proposals`)).proposals;
  expect(after.some((p) => p.kind === 'DROP_DUPLICATE_ROWS')).toBe(false);
  expect((await h.get(`/datasets/${ds}`)).rows).toBe(record.rows - dupes.values_lost);

  // Domain: confirm what the person ticks.
  await h.sidebar(page, 'Domain').click();
  await expect(page.getByText('Confirmed now:')).toBeVisible(h.T);
  await page.getByText('Marketing', { exact: true }).click();
  await page.waitForTimeout(800);
  await page.getByRole('button', { name: 'Confirm domains' }).click();
  await expect(page.getByText('Confirmed now: marketing')).toBeVisible(h.T);
  expect((await h.get(`/datasets/${ds}/domains/detect`)).confirmed).toEqual(['marketing']);

  // Contract: none in force yet; fill the form; confirm; the contract in force is the API's.
  await h.sidebar(page, 'Contract').click();
  await expect(page.getByText('What each column is')).toBeVisible(h.T);
  await expect(page.getByText('No contract confirmed yet.')).toBeVisible();
  await h.type(page, page.getByLabel('One row of this table is…'), 'one row per campaign per day');
  for (const m of SUMS) await h.choose(page, m, 'Measure (a number)');
  const keyName = 'Which columns together identify one row?';
  const keyField = h.multiselect(page, keyName);
  const proposedKey = (await h.get(`/datasets/${ds}/contract/proposal`)).key;
  await expect.poll(() => h.chips(keyField)).toEqual(proposedKey);   // the engine's, shown as is
  if (proposedKey.length) {
    await keyField.getByRole('button', { name: 'Clear all' }).click();
    await page.waitForTimeout(1200);
  }
  for (const c of ['date', 'campaign']) await h.choose(page, keyName, c);
  await page.keyboard.press('Escape');
  await expect.poll(() => h.chips(keyField)).toEqual(['date', 'campaign']);
  for (const m of [...SUMS, 'ctr']) {
    const card = h.keyed(page, `sun-card-measure-${m}`);
    await h.choose(page, card.getByRole('combobox', { name: 'How it combines across rows' }), m === 'ctr' ? 'none' : 'sum');
    await h.type(page, card.getByRole('textbox', { name: 'What it means (required)' }), `${m} as the platform reports it`);
  }
  const dates = page.locator('[data-testid="stDateInput"] input');
  await h.type(page, dates.nth(0), '2026-08-01');
  await h.type(page, dates.nth(1), '2026-09-30');
  const useSuggested = page.getByRole('button', { name: /suggested answer/ });
  if (await useSuggested.count()) { await useSuggested.click(); await page.waitForTimeout(1200); }
  for (const group of await page.getByRole('radiogroup').all()) {
    if (await group.locator('input:checked').count() === 0) {
      await group.locator('label').first().click();
      await page.waitForTimeout(800);
    }
  }
  await page.getByRole('button', { name: 'Confirm the contract' }).click();
  await expect(page.getByText('Contract confirmed (version 1)')).toBeVisible(h.T);
  const inForce = await h.get(`/datasets/${ds}/contract`);
  expect(inForce.primary_key).toEqual(['date', 'campaign']);
  expect(Object.fromEntries(inForce.measures.map((m) => [m.column, m.agg])))
    .toEqual({ ...Object.fromEntries(SUMS.map((m) => [m, 'sum'])), ctr: 'none' });
  await expect(page.getByText(`The contract in force: version ${inForce.version}, confirmed ${inForce.confirmed_at}`)).toBeVisible(h.T);
  const tools = (await h.get(`/datasets/${ds}/tools`)).tools;
  expect(tools.some((t) => t.status === 'active' && t.tool_id.startsWith('marketing.'))).toBe(true);
});
