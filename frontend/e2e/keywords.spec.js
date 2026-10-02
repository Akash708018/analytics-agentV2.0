// F6 and F10: keyword groups proposed, approved, and an approval withdrawn.
const { test, expect } = require('@playwright/test');
const h = require('./helpers');

test('groups are proposed from the chosen column, approved, and one approval withdrawn', async ({ page }) => {
  test.slow();
  const { sid, ds } = await h.keywordsReady();
  await h.open(page, 'keywords', sid);
  const column = h.keyed(page, `ui.kw.${ds}.column`).getByRole('combobox');
  await expect(column).toHaveValue('', h.T);                            // no column chosen for you
  await h.choose(page, column, 'query');
  await h.keyed(page, `ui.kw.${ds}.run`).getByRole('button').click();
  await expect(page.getByText(/^\d+ group\(s\) · \d+ keyword/)).toBeVisible(h.T);

  let groups = (await h.get(`/datasets/${ds}/keyword-groups`)).groups;
  expect(groups.some((g) => g.approved)).toBe(false);
  expect(await h.text(page, /^\d+ group\(s\) · \d+ keyword/)).toMatch(new RegExp(`^${groups.length} group\\(s\\)`));
  expect(await page.locator(`[class*="st-key-ui-kw-${ds}-tick-"] input:checked`).count()).toBe(0);

  const [a, b] = groups;
  await h.tick(page, `ui.kw.${ds}.tick.${a.group_id}`);
  await h.tick(page, `ui.kw.${ds}.tick.${b.group_id}`);
  await h.keyed(page, `ui.kw.${ds}.approve`).getByRole('button').click();
  await expect(page.getByText('Approved 2 group(s).')).toBeVisible(h.T);
  groups = (await h.get(`/datasets/${ds}/keyword-groups`)).groups;
  expect(groups.filter((g) => g.approved).map((g) => g.group_id).sort()).toEqual([a.group_id, b.group_id].sort());

  await page.waitForTimeout(1500);
  await h.tick(page, `ui.kw.${ds}.tick.${a.group_id}`);
  const withdraw = h.keyed(page, `ui.kw.${ds}.withdraw`).getByRole('button');
  await expect(withdraw).toHaveText('Withdraw approval of the 1 ticked');
  await withdraw.click();
  await expect(page.getByText('Withdrew the approval of 1 group(s); they are proposals again.')).toBeVisible(h.T);
  groups = (await h.get(`/datasets/${ds}/keyword-groups`)).groups;
  expect(groups.filter((g) => g.approved).map((g) => g.group_id)).toEqual([b.group_id]);
});
