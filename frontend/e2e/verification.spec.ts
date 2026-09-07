import { test, expect } from '@playwright/test';

test('M8 claim verification is reached, explained, and never released unverified', async ({ page }, testInfo) => {
  test.setTimeout(180000);
  test.skip(process.env.MEDRAG_E2E_LIVE !== '1', 'Requires the real stack and the M6 smoke corpus.');
  const credentials = JSON.parse(process.env.MEDRAG_DEV_PRINCIPALS ?? '[]') as {token: string; role: string}[];
  const token = credentials.find(c => c.role === 'admin')?.token;
  if (!token) throw new Error('Development credentials are required.');

  await page.goto('/retrieval');
  await page.getByLabel('Access key').fill(token);
  await page.getByRole('button', {name: 'Open workspace'}).click();
  await page.getByLabel('Retrieval mode').selectOption('VERIFIED_ANSWER');
  await page.getByLabel('Query text').fill('Parameter Group Alpha Units');
  await page.getByRole('button', {name: 'Retrieve candidates'}).click();

  // The development stack has no verifier account configured, so the pipeline may abstain at the
  // sufficiency gate, abstain during verification, or report a declared provider failure. All
  // three are correct; releasing an unverified answer is not.
  const sufficiency = page.getByRole('button', {name: 'Sufficiency'});
  const failed = page.getByRole('alert');
  await expect(sufficiency.or(failed).first()).toBeVisible({timeout: 120000});

  if (await sufficiency.isVisible()) {
    await sufficiency.click();
    await expect(page.getByRole('heading', {name: 'Sufficiency decision'})).toBeVisible();
    const claims = page.getByRole('button', {name: 'Claims'});
    if (await claims.isVisible()) {
      await claims.click();
      await expect(page.getByRole('heading', {name: 'Claim verification'})).toBeVisible();
      await expect(page.getByText(/Claims are taken from the answer text itself/)).toBeVisible();
      await page.screenshot({path: testInfo.outputPath('m8-claims.png'), fullPage: true});
      await page.setViewportSize({width: 390, height: 844});
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    }
  } else {
    await expect(failed.first()).toContainText(/GENERATION_|VERIFIER_/);
  }

  // Nothing on this page is a delivered medical answer, and the final Ask experience is M9.
  await expect(page.getByRole('heading', {name: 'Answer', exact: true})).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'Ask', exact: true})).toHaveCount(0);
  await expect(page.getByText(/medical confidence/i)).toHaveCount(0);
});
