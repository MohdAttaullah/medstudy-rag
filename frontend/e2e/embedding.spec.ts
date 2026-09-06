import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { randomUUID } from 'node:crypto';

/**
 * End-to-end M4 path against the running stack: upload a synthetic fixture, wait for the real
 * worker to parse, chunk, embed and index it to READY_FOR_RETRIEVAL, then inspect the index run,
 * a point, its source chunk and the original source page — all through the real UI.
 */
test('a real upload is embedded and indexed, and every point resolves to its source', async ({ page }, testInfo) => {
  test.setTimeout(1200000);
  test.skip(process.env.MEDRAG_E2E_LIVE !== '1', 'Requires the running API, worker and dispatcher.');
  const credentials = JSON.parse(process.env.MEDRAG_DEV_PRINCIPALS ?? '[]') as { token: string; role: string }[];
  const token = credentials.find(item => item.role === 'admin')?.token;
  if (!token) throw new Error('Provision development credentials and pass the environment to Playwright.');

  const title = 'Index fixture ' + randomUUID().slice(0, 8);
  const fixture = resolve('../backend/tests/fixtures/parsing/table.pdf');
  const buffer = Buffer.concat([readFileSync(fixture), Buffer.from('\n%% unique ' + randomUUID() + '\n')]);

  await page.goto('/library');
  await page.getByLabel('Access key').fill(token);
  await page.getByRole('button', { name: 'Open workspace' }).click();
  await page.getByLabel('Title', { exact: true }).fill(title);
  await page.getByRole('combobox', { name: 'Source type', exact: true }).selectOption('TEXTBOOK');
  await page.getByRole('combobox', { name: 'Authority level', exact: true }).selectOption('UNREVIEWED');
  await page.getByLabel('Choose PDF files or drag them here').setInputFiles({
    name: 'index-fixture.pdf', mimeType: 'application/pdf', buffer,
  });
  await page.getByRole('button', { name: 'Upload documents' }).click();
  await expect(page.getByRole('status')).toContainText('Upload recorded', { timeout: 60000 });
  await page.getByRole('link', { name: 'Open uploaded document' }).click();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();

  // Parsing loads model weights on a cold worker, so allow a generous wait for the whole path.
  await expect(page.locator('.version-card').getByText('ready for retrieval').first()).toBeVisible({
    timeout: 1080000,
  });
  const embedding = page.locator('.panel', { hasText: 'Embedding and index' }).first();
  await expect(embedding.getByText(/ncbi\/MedCPT-Article-Encoder @ d05a736da4bb/)).toBeVisible();
  await expect(embedding.getByText(/768-dimensional · CLS pooling · unnormalized · DOT similarity/)).toBeVisible();
  await expect(embedding.getByText(/Index verified/)).toBeVisible();
  await expect(embedding.getByText(/Active vectors/)).toBeVisible();
  // The claim on the page is exactly what was verified, and no more.
  await expect(embedding.getByText(/It does not mean this document can be answered from/)).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('embedding-summary.png'), fullPage: true });

  await embedding.getByRole('link', { name: 'Open index inspector' }).click();
  await expect(page.getByRole('heading', { name: 'Index inspector' })).toBeVisible();
  await expect(page.getByText('Active vector index')).toBeVisible();
  await expect(page.getByText('medcpt_dense')).toBeVisible();
  await expect(page.getByText('768')).toBeVisible();
  await expect(page.getByText('DOT')).toBeVisible();
  await expect(page.getByText('d05a736da4bb84ee4057b7f7999485be6ed85465')).toBeVisible();
  await expect(page.getByText(/point\(s\) present for this run/)).toBeVisible({ timeout: 30000 });
  await page.screenshot({ path: testInfo.outputPath('index-inspector.png'), fullPage: true });

  // A point carries metadata, never a dense array.
  const point = page.locator('.version-card', { hasText: 'dimensions' }).first();
  await expect(point).toBeVisible({ timeout: 30000 });
  await expect(point.getByText(/768 dimensions · \d+ input tokens · norm/)).toBeVisible();
  await expect(point.getByText(/^Vector SHA-256: [0-9a-f]{64}$/)).toBeVisible();

  // Point -> chunk -> chunk inspector -> original source page.
  await point.getByRole('button', { name: 'Inspect source chunk' }).click();
  const detail = page.getByLabel('Source chunk');
  await expect(detail.getByRole('heading', { name: 'Embedded retrieval representation' })).toBeVisible();
  await expect(detail.getByText(/^Chunk SHA-256: [0-9a-f]{64}$/)).toBeVisible();
  await detail.getByRole('link', { name: 'Open chunk inspector' }).click();
  await expect(page.getByRole('heading', { name: 'Chunk inspector' })).toBeVisible();
  await page.getByRole('button', { name: /^Inspect chunk \d+$/ }).first().click();
  await page.getByRole('link', { name: 'Open source page' }).first().click();
  await expect(page.getByRole('heading', { name: 'Parse inspector' })).toBeVisible();
  await expect(page.getByText('1 (1-based, printed order)')).toBeVisible();

  // An indexed corpus is still not an answerable one.
  await page.getByRole('link', { name: 'Ask', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Ask with evidence' })).toBeDisabled();
});

test('operations exposes the real index stages and not the answerable state', async ({ page }) => {
  test.skip(process.env.MEDRAG_E2E_LIVE !== '1', 'Requires the running API.');
  const credentials = JSON.parse(process.env.MEDRAG_DEV_PRINCIPALS ?? '[]') as { token: string; role: string }[];
  const token = credentials.find(item => item.role === 'admin')?.token;
  if (!token) throw new Error('Provision development credentials and pass the environment to Playwright.');
  await page.goto('/operations');
  await page.getByLabel('Access key').fill(token);
  await page.getByRole('button', { name: 'Open workspace' }).click();
  const filter = page.getByLabel('Status filter');
  await expect(filter.locator('option').first()).toBeAttached({ timeout: 30000 });
  const options = await filter.locator('option').allTextContents();
  for (const stage of ['EMBEDDING', 'INDEXING', 'VERIFYING_INDEX', 'READY_FOR_RETRIEVAL']) {
    expect(options).toContain(stage);
  }
  // READY is reserved for a version that can actually be answered from.
  expect(options).not.toContain('READY');
});
