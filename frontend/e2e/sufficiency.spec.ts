import { test, expect } from '@playwright/test';

test('M7 evidence gate is reached, explained, and never presented as an answer', async ({ page }, testInfo) => {
  test.setTimeout(180000);
  test.skip(process.env.MEDRAG_E2E_LIVE !== '1', 'Requires the real stack and the M6 smoke corpus.');
  const credentials = JSON.parse(process.env.MEDRAG_DEV_PRINCIPALS ?? '[]') as {token: string; role: string}[];
  const token = credentials.find(c => c.role === 'admin')?.token;
  if (!token) throw new Error('Development credentials are required.');

  await page.goto('/retrieval');
  await page.getByLabel('Access key').fill(token);
  await page.getByRole('button', {name: 'Open workspace'}).click();
  await page.getByLabel('Retrieval mode').selectOption('GROUNDED_DRAFT');
  await page.getByLabel('Query text').fill('Parameter Group Alpha Units');
  await page.getByRole('button', {name: 'Retrieve candidates'}).click();

  // No provider key is configured in the development stack, so the corpus reaches the gate and the
  // gate decides. Either it abstains, or it permits generation and the provider is then reported
  // unavailable. Both are correct; what must never happen is an answer.
  const decided = page.getByRole('button', {name: 'Sufficiency'});
  const failed = page.getByRole('alert');
  await expect(decided.or(failed).first()).toBeVisible({timeout: 120000});

  if (await decided.isVisible()) {
    await decided.click();
    await expect(page.getByRole('heading', {name: 'Sufficiency decision'})).toBeVisible();
    await expect(page.getByText(/No retrieval, fusion or reranker score takes part/)).toBeVisible();
    await expect(page.getByRole('table')).toBeVisible();
    await page.screenshot({path: testInfo.outputPath('m7-sufficiency.png'), fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  } else {
    await expect(failed.first()).toContainText(/GENERATION_/);
  }

  // Whatever the outcome, nothing on this page is a delivered medical answer.
  await expect(page.getByRole('heading', {name: 'Answer', exact: true})).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'Ask', exact: true})).toHaveCount(0);
  await expect(page.getByText(/medical confidence/i)).toHaveCount(0);
});
