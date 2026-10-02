// F5: a question's answer, how it was reached, and the results behind it; a reload never resends.
const { test, expect } = require('@playwright/test');
const h = require('./helpers');

test('an answer shows its plan and evidence as the turn holds them, and a reload does not resend', async ({ page }) => {
  test.slow();
  const { sid } = await h.adsReady('ask');
  await h.open(page, 'ask', sid);
  const question = page.getByLabel('Your question', { exact: true });
  await question.fill('Where is spend wasted?');
  await page.getByRole('button', { name: 'Ask', exact: true }).click();
  await expect(page.getByText(/^Here is what the data shows/).first()).toBeVisible(h.T);
  await page.waitForTimeout(1500);

  const turns = (await h.get(`/sessions/${sid}/turns`)).turns;
  expect(turns.length).toBe(1);
  const turn = turns[0];
  expect(turn.status).toBe('done');
  expect(await h.text(page, /^Here is what the data shows/)).toBe(turn.answer.text);

  await page.getByText('How this was answered').first().click();
  const how = page.locator('[data-testid="stExpander"]').filter({ hasText: 'How this was answered' }).first();
  await expect(how.getByText(new RegExp(`Playbook ${turn.answer.playbook}`))).toBeVisible();
  for (const call of turn.events.filter((e) => e.type === 'tool_call')) {
    await expect(how.getByText(call.data.tool_id).first()).toBeVisible();
  }

  const result = turn.answer.results[0];
  await page.getByText(`Evidence: ${result.summary}`).first().click();
  const evidence = page.locator('[data-testid="stExpander"]').filter({ hasText: `Evidence: ${result.summary}` }).first();
  await expect(evidence.getByText(`Every figure (${result.figures.length}), with its source`)).toBeVisible(h.T);

  await page.reload();
  await expect(page.getByText(/^Here is what the data shows/).first()).toBeVisible(h.T);
  await page.waitForTimeout(2000);
  expect((await h.get(`/sessions/${sid}/turns`)).turns.length).toBe(1);
});
