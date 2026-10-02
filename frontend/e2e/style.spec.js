// F9: every screen renders without an error and carries the stylesheet once; with reduced
// motion nothing animates and nothing is left hidden.
const { test, expect } = require('@playwright/test');
const h = require('./helpers');

const ROUTES = ['', 'data', 'clean', 'domain', 'contract', 'metrics', 'keywords', 'tools', 'explore',
                'results', 'ask'];
const sheets = (page) => page.evaluate(() => [...document.querySelectorAll('style')]
  .filter((s) => s.textContent.includes('--sun-ember')).length);

test('every page of a prepared dataset renders with no error and one stylesheet', async ({ page }) => {
  test.slow();
  const { sid } = await h.adsReady('tools');
  for (const route of ROUTES) {
    await page.goto(h.app(`/${route}?sid=${sid}`));
    await expect(page.locator('h1').first()).toBeVisible(h.T);
    await page.waitForTimeout(1500);
    expect(await page.locator('[data-testid="stException"]').count(), route).toBe(0);
    expect(await sheets(page), route).toBe(1);
  }
});

test('the landing animates, and with reduced motion nothing animates and the scene stays visible', async ({ browser }) => {
  const moving = await (await browser.newContext()).newPage();
  await moving.goto(h.app('/'));
  await expect(moving.locator('.sun-hero')).toBeVisible(h.T);
  await moving.waitForTimeout(1000);
  expect(await moving.evaluate(() => document.getAnimations().length)).toBeGreaterThan(0);

  const still = await (await browser.newContext({ reducedMotion: 'reduce' })).newPage();
  await still.goto(h.app('/'));
  await expect(still.locator('.sun-hero')).toBeVisible(h.T);
  await still.waitForTimeout(1000);
  expect(await still.evaluate(() => document.getAnimations().length)).toBe(0);
  expect(await still.locator('.sun-hero').evaluate((el) => getComputedStyle(el).opacity)).toBe('1');
  expect(await sheets(still)).toBe(1);
});
