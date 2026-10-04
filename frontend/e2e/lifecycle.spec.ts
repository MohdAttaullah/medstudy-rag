import { test, expect, type APIRequestContext, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { randomUUID } from 'node:crypto';

/**
 * The document lifecycle against the running stack.
 *
 * Two journeys. The first reads an existing document that stopped for review and checks the page
 * explains it and offers only what can work — it changes nothing. The second uploads a small,
 * clearly named disposable fixture, follows it to Ready for Ask, then deletes it permanently and
 * proves it is gone from the library, the API and retrieval. Only that disposable document is
 * ever deleted.
 *
 * Environment: MEDRAG_E2E_LIVE=1, MEDRAG_DEV_PRINCIPALS (never printed), optional
 * MEDRAG_E2E_TENANT to choose the workspace, and MEDRAG_E2E_REVIEW_DOCUMENT for the first journey.
 */

type Principal = { token: string; role: string; tenant_id: string };

function principals() {
  const all = JSON.parse(process.env.MEDRAG_DEV_PRINCIPALS ?? '[]') as Principal[];
  const tenant = process.env.MEDRAG_E2E_TENANT;
  const scoped = tenant ? all.filter(item => item.tenant_id === tenant) : all;
  const admin = scoped.find(item => item.role === 'admin');
  const reader = scoped.find(item => item.role === 'reader' && item.tenant_id === admin?.tenant_id);
  if (!admin) throw new Error('Provision development credentials and pass the environment to Playwright.');
  return { admin, reader };
}

async function signIn(page: Page, token: string, path: string) {
  await page.goto(path);
  await page.getByLabel('Access key').fill(token);
  await page.getByRole('button', { name: 'Open workspace' }).click();
  await expect(page.getByRole('button', { name: 'Account and workspace' })).toBeVisible();
}

async function noHorizontalScroll(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
}

async function search(request: APIRequestContext, token: string, documentId: string) {
  return request.post('/api/v1/retrieval/search', {
    headers: { Authorization: 'Bearer ' + token },
    data: { query: 'Parameter Group Alpha Units', mode: 'BM25_ONLY', top_k: 20, filters: { document_ids: [documentId] } },
  });
}

test('a document stopped for review explains why and offers only what can work', async ({ page }, testInfo) => {
  test.skip(process.env.MEDRAG_E2E_LIVE !== '1', 'Requires the running stack.');
  const documentId = process.env.MEDRAG_E2E_REVIEW_DOCUMENT;
  test.skip(!documentId, 'Set MEDRAG_E2E_REVIEW_DOCUMENT to a document awaiting review.');
  const { admin, reader } = principals();

  await signIn(page, admin.token, `/documents/${documentId}`);
  const panel = page.locator('section.review-panel');
  await expect(panel.getByRole('heading', { name: 'Review required' })).toBeVisible({ timeout: 30000 });
  await expect(page.getByRole('heading', { name: 'Paused for review' })).toBeVisible();
  await expect(panel).toContainText(/\d+ blocking issues?/);
  await expect(panel).toContainText('cannot appear in Ask answers');
  await expect(panel.getByRole('button', { name: 'Re-embed' })).toHaveCount(0);
  await expect(panel.getByRole('button', { name: /accept/i })).toHaveCount(0);
  await expect(panel.locator('.review-warnings')).not.toHaveAttribute('open', '');
  await page.screenshot({ path: testInfo.outputPath('review-required.png'), fullPage: true });
  await panel.locator('.issue-blocking').first().screenshot({ path: testInfo.outputPath('blocking-issue.png') });

  const inspect = panel.getByRole('link', { name: /Inspect (this issue|the first occurrence)/ }).first();
  await expect(inspect).toHaveAttribute('href', /\/chunk-runs\/.+\?chunk=|\/parse\//);

  await page.setViewportSize({ width: 390, height: 844 });
  await noHorizontalScroll(page);
  await page.screenshot({ path: testInfo.outputPath('review-required-mobile.png'), fullPage: true });

  if (reader) {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.getByRole('button', { name: 'Account and workspace' }).click();
    await page.getByRole('menuitem', { name: 'Sign out' }).click();
    await signIn(page, reader.token, `/documents/${documentId}`);
    await expect(panel.getByRole('heading', { name: 'Review required' })).toBeVisible({ timeout: 30000 });
    await expect(panel.getByRole('button')).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Delete permanently' })).toHaveCount(0);
  }
});

test('a disposable upload is followed to Ready, then permanently deleted', async ({ page, request }, testInfo) => {
  test.setTimeout(1500000);
  test.skip(process.env.MEDRAG_E2E_LIVE !== '1', 'Requires the running stack.');
  const { admin } = principals();
  const title = 'DISPOSABLE delete test ' + randomUUID().slice(0, 8);
  const fixture = resolve('../backend/tests/fixtures/parsing/table.pdf');
  const buffer = Buffer.concat([readFileSync(fixture), Buffer.from('\n%% disposable ' + randomUUID() + '\n')]);

  await signIn(page, admin.token, '/library');
  await page.getByLabel('Title', { exact: true }).fill(title);
  await page.getByRole('combobox', { name: 'Source type', exact: true }).selectOption('TEXTBOOK');
  await page.getByRole('combobox', { name: 'Authority level', exact: true }).selectOption('UNREVIEWED');
  await page.getByLabel('Choose PDF files or drag them here').setInputFiles({
    name: 'disposable-delete-test.pdf', mimeType: 'application/pdf', buffer,
  });
  await page.getByRole('button', { name: 'Upload documents' }).click();
  await expect(page.getByRole('status')).toContainText('Upload recorded', { timeout: 60000 });
  await page.getByRole('link', { name: 'Open uploaded document' }).click();
  const documentId = page.url().split('/documents/')[1].split(/[?#]/)[0];
  testInfo.annotations.push({ type: 'disposable-document', description: `${title} (${documentId})` });

  // Processing: real stages, an elapsed clock, never a percentage.
  const lifecycle = page.locator('section.lifecycle');
  await expect(lifecycle.getByRole('heading', { name: 'Preparing document for Ask' })).toBeVisible({ timeout: 30000 });
  await expect(lifecycle.locator('.lc-running')).toHaveCount(1, { timeout: 120000 });
  await expect(lifecycle).not.toContainText('%');
  await expect(lifecycle.getByRole('progressbar')).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('processing-active.png'), fullPage: true });

  await expect(page.getByRole('heading', { name: 'Ready for Ask', level: 2 })).toBeVisible({ timeout: 1200000 });
  await expect(lifecycle.locator('.lc-completed')).toHaveCount(7);
  await page.screenshot({ path: testInfo.outputPath('ready.png'), fullPage: true });
  const before = await search(request, admin.token, documentId);
  expect(before.ok()).toBe(true);
  expect(((await before.json()) as { candidates: unknown[] }).candidates.length).toBeGreaterThan(0);

  // Delete from the library: ⋯ → Delete permanently → type DELETE.
  await page.getByRole('link', { name: 'Library', exact: true }).click();
  const row = page.locator('tr', { has: page.getByRole('link', { name: title, exact: true }) });
  await expect(row).toContainText('Ready');
  await row.getByRole('button', { name: `More actions for ${title}` }).click();
  await page.getByRole('menuitem', { name: 'Delete permanently' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('This cannot be undone.');
  await expect(dialog).toContainText(/searchable passages? and \d+ search vectors?/);
  const confirm = dialog.getByRole('button', { name: 'Delete permanently' });
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel(/Type DELETE to confirm/).fill('DELETE');
  await expect(confirm).toBeEnabled();
  await page.waitForTimeout(300); // let the button's colour transition finish before the picture
  await page.screenshot({ path: testInfo.outputPath('delete-confirmation.png'), fullPage: true });
  await confirm.click();
  await expect(dialog).toHaveCount(0, { timeout: 120000 });
  await expect(page.getByText(`“${title}” was permanently deleted.`)).toBeVisible();
  await expect(page.getByRole('link', { name: title, exact: true })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('post-delete-library.png'), fullPage: true });
  // Visually hidden labels inside the table once widened the whole page at phone width.
  await page.setViewportSize({ width: 390, height: 844 });
  await noHorizontalScroll(page);

  // Gone from the API and from retrieval, not merely hidden.
  const gone = await request.get(`/api/v1/documents/${documentId}`, { headers: { Authorization: 'Bearer ' + admin.token } });
  expect(gone.status()).toBe(404);
  const after = await search(request, admin.token, documentId);
  if (after.ok()) expect(((await after.json()) as { candidates: unknown[] }).candidates).toHaveLength(0);
  else expect(after.status()).toBeLessThan(500);
});
