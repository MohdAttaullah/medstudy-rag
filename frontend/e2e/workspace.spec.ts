import { test, expect } from '@playwright/test';
test('workspace navigation and safe empty state', async ({ page }, testInfo) => {
  await page.goto('/');
  // The marketing headline this opened with was removed when Ask became a conversation
  // (73eb049); the shell is what a signed-out visitor actually lands on, so that is what is
  // asserted. The invariant underneath is unchanged and is the one that matters: an
  // unauthenticated visitor gets the access gate, not a question box, and certainly not an answer.
  await expect(page.getByRole('heading', { name: 'Development workspace access' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Ask with evidence' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Answer', exact: true })).toHaveCount(0);
  // Signed out, the header offers no account control: there is no principal to offer one for.
  await expect(page.getByRole('button', { name: 'Account and workspace' })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('desktop.png'), fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('heading', { name: 'Development workspace access' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('mobile.png'), fullPage: true });

  // At phone width the rail is a drawer, so navigation is reached by opening it first.
  await page.getByRole('button', { name: 'Navigation' }).click();
  await page.getByRole('link', { name: 'Library', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Your source documents' })).toBeVisible();
  await page.goto('/documents/bootstrap');
  await expect(page.getByRole('heading', { name: 'Document details' })).toBeVisible();
});
