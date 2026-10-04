import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { randomUUID } from 'node:crypto';

/**
 * End-to-end M3 path against the running stack: upload a synthetic structural fixture, wait for
 * the real worker to parse and then chunk it to READY_FOR_EMBEDDING, and inspect a chunk, its
 * retrieval representation, a table part and the route back to the original source page.
 */
test('a real upload is chunked and every chunk resolves to its source page', async ({ page }, testInfo) => {
  test.setTimeout(900000);
  test.skip(process.env.MEDRAG_E2E_LIVE !== '1', 'Requires the running API, worker and dispatcher.');
  const credentials = JSON.parse(process.env.MEDRAG_DEV_PRINCIPALS ?? '[]') as { token: string; role: string }[];
  const token = credentials.find(item => item.role === 'admin')?.token;
  if (!token) throw new Error('Provision development credentials and pass the environment to Playwright.');

  const title = 'Chunk fixture ' + randomUUID().slice(0, 8);
  const fixture = resolve('../backend/tests/fixtures/parsing/table.pdf');
  const buffer = Buffer.concat([readFileSync(fixture), Buffer.from('\n%% unique ' + randomUUID() + '\n')]);

  await page.goto('/library');
  await page.getByLabel('Access key').fill(token);
  await page.getByRole('button', { name: 'Open workspace' }).click();
  await page.getByLabel('Title', { exact: true }).fill(title);
  await page.getByRole('combobox', { name: 'Source type', exact: true }).selectOption('TEXTBOOK');
  await page.getByRole('combobox', { name: 'Authority level', exact: true }).selectOption('UNREVIEWED');
  await page.getByLabel('Choose PDF files or drag them here').setInputFiles({
    name: 'chunk-fixture.pdf', mimeType: 'application/pdf', buffer,
  });
  await page.getByRole('button', { name: 'Upload documents' }).click();
  await expect(page.getByRole('status')).toContainText('Upload recorded', { timeout: 60000 });
  await page.getByRole('link', { name: 'Open uploaded document' }).click();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();

  // Parsing loads model weights on a cold worker, so allow a generous wait. M4 embeds straight
  // after chunking, so the version may already have advanced past ready for embedding.
  await expect(page.locator('.lc-step', { hasText: 'Preparing searchable passages' })).toContainText('Completed', { timeout: 1080000 });
  await page.locator('.version-card summary', { hasText: 'Findings and technical details' }).first().click();
  const chunking = page.locator('.panel', { hasText: 'Chunking' }).first();
  await expect(chunking.getByText(/medical-structure/)).toBeVisible();
  await expect(chunking.getByText(/chunking-m3-v1/)).toBeVisible();
  await expect(chunking.getByText(/Active dataset/)).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('chunk-summary.png'), fullPage: true });

  await chunking.getByRole('link', { name: 'Open chunk inspector' }).click();
  await expect(page.getByRole('heading', { name: 'Chunk inspector' })).toBeVisible();
  await expect(page.getByText('Active chunk dataset')).toBeVisible();
  await expect(page.getByText(/No embeddings, index, or answers exist/)).toBeVisible();

  // Inspect a text chunk: source text, retrieval representation, hash and ordered provenance.
  await page.getByRole('button', { name: /^Inspect chunk \d+$/ }).first().click();
  const detail = page.getByLabel('Selected chunk');
  await expect(detail.getByRole('heading', { name: 'Source representation' })).toBeVisible();
  await expect(detail.getByRole('heading', { name: 'Retrieval representation' })).toBeVisible();
  await expect(detail.getByText(/^SHA-256: [0-9a-f]{64}$/)).toBeVisible();
  await expect(detail.getByText(/reading order \d+ \/ offsets \d+–\d+/).first()).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('chunk-detail.png'), fullPage: true });

  // A table chunk keeps its canonical cells and its source artifact relationship.
  await page.getByLabel('Chunk type').selectOption('TABLE');
  const tableCard = page.locator('.version-card', { hasText: 'TABLE' }).first();
  await expect(tableCard).toBeVisible({ timeout: 30000 });
  await tableCard.getByRole('button', { name: /^Inspect chunk \d+$/ }).click();
  await expect(detail.getByRole('heading', { name: 'Source table' })).toBeVisible({ timeout: 30000 });
  await expect(detail.locator('table.parse-table').getByText('Parameter').first()).toBeVisible();
  await expect(detail.getByText(/Table artifact [0-9a-f-]{36}/)).toBeVisible();

  // The chunk resolves back to the exact page of the original document.
  await detail.getByRole('link', { name: 'Open source page' }).first().click();
  await expect(page.getByRole('heading', { name: 'Parse inspector' })).toBeVisible();
  await expect(page.getByText('1 (1-based, printed order)')).toBeVisible();

  // Chunked material is still not answerable evidence.
  await page.getByRole('link', { name: 'Ask', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Ask with evidence' })).toBeDisabled();
});

test('operations exposes the real chunk stages and not the embedding stages', async ({ page }) => {
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
  for (const stage of ['CHUNKING', 'VALIDATING_CHUNKS', 'READY_FOR_EMBEDDING']) {
    expect(options).toContain(stage);
  }
  // M4 made the embedding and index stages real; READY remains unreachable.
  expect(options).not.toContain('READY');
});
