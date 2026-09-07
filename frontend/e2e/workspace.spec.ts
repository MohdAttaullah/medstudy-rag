import { test, expect } from '@playwright/test';
test('workspace navigation and safe empty state', async ({ page }, testInfo) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Evidence comes first.' })).toBeVisible();
  // Until M9 this asserted the Ask control was disabled. Answering now exists, so the invariant
  // that replaces it is the one that still matters: an unauthenticated visitor gets the access
  // gate, not a question box, and certainly not an answer.
  await expect(page.getByRole('heading', { name: 'Development workspace access' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Ask with evidence' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Answer', exact: true })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('heading', { name: 'Evidence comes first.' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('mobile.png'), fullPage: true });
  await page.getByRole('link', { name: 'Library', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Your source documents' })).toBeVisible();
  await page.goto('/documents/bootstrap');
  await expect(page.getByRole('heading', { name: 'Document details' })).toBeVisible();
});
