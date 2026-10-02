// F2: sessions, saved work, links and conflicts, in the browser.
const crypto = require('node:crypto');
const { test, expect } = require('@playwright/test');
const h = require('./helpers');

const PAGES = ['Session', 'Data', 'Clean', 'Domain', 'Contract', 'Metrics', 'Keyword groups', 'Tools',
               'Explore', 'Results', 'Ask'];
const nameBox = (page) => page.getByLabel('Name this analysis (optional)', { exact: true });

test('a session starts only on the click, keeps its name after a reload, and every link keeps it', async ({ page }) => {
  await page.goto(h.app('/'));
  await expect(page.getByRole('button', { name: 'Start a new session' })).toBeVisible(h.T);
  await expect(nameBox(page)).toHaveCount(0);
  await page.getByRole('button', { name: 'Start a new session' }).click();
  await expect(nameBox(page)).toBeVisible(h.T);
  const sid = new URL(page.url()).searchParams.get('sid');
  expect((await h.get(`/sessions/${sid}`)).version).toBe(1);

  await h.type(page, nameBox(page), 'Q3 paid search review');
  const saved = await h.eventually(() => h.get(`/sessions/${sid}`), (s) => s.ui_state.label);
  expect(saved.ui_state.label).toBe('Q3 paid search review');
  await expect(page.getByText(`Saved · version ${saved.version}`)).toBeVisible();

  await page.reload();
  await expect(nameBox(page)).toHaveValue('Q3 paid search review', h.T);
  const links = page.locator('[data-testid="stSidebar"]').getByRole('link');
  await expect(links).toHaveText(PAGES);
  for (const href of await links.evaluateAll((as) => as.map((a) => a.getAttribute('href')))) {
    expect(href).toContain(`sid=${sid}`);
  }
});

test('a malformed link and an unknown session load nothing', async ({ page }) => {
  await page.goto(h.app('/?sid=not-a-uuid'));
  await expect(page.getByText("This link's session id is not valid")).toBeVisible(h.T);
  const unknown = crypto.randomUUID();
  await page.goto(h.app(`/?sid=${unknown}`));
  await expect(page.getByText('not found or has expired')).toBeVisible(h.T);
  expect((await h.call('GET', `/sessions/${unknown}`)).status).toBe(404);   // none was made
});

test('two tabs: a stale edit waits for a choice, and Load latest shows the other tab', async ({ browser }) => {
  const { sid } = await h.session();
  const context = await browser.newContext();
  const [a, b] = [await context.newPage(), await context.newPage()];
  for (const tab of [a, b]) {
    await tab.goto(h.app(`/?sid=${sid}`));
    await expect(nameBox(tab)).toBeVisible(h.T);
  }
  await h.type(a, nameBox(a), 'Tab A label');
  await h.eventually(() => h.get(`/sessions/${sid}`), (s) => s.ui_state.label === 'Tab A label');
  await h.type(b, nameBox(b), 'Tab B label');
  await expect(b.getByText('changed in another tab')).toBeVisible(h.T);
  await expect(b.getByText('Not saved: this session changed elsewhere.')).toBeVisible();
  expect((await h.get(`/sessions/${sid}`)).ui_state.label).toBe('Tab A label');   // nothing overwritten
  await b.getByRole('button', { name: 'Load latest' }).click();
  await expect(nameBox(b)).toHaveValue('Tab A label', h.T);
  await expect(b.getByText('changed in another tab')).toHaveCount(0);
  await context.close();
});
