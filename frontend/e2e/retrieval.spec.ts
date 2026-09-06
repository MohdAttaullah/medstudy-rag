import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { randomUUID } from 'node:crypto';

/**
 * End-to-end M5 path against the running stack: upload a synthetic fixture, wait for the real
 * worker to carry it all the way to RETRIEVAL_READY, then retrieve it through the real UI and
 * follow one candidate back to the original source page.
 *
 * The negative assertions matter as much as the positive ones. The page must never present an
 * answer, a confidence or a summary, because none of those exist at this milestone.
 */
test('a real upload becomes retrievable and a candidate resolves to its source page', async ({ page }, testInfo) => {
  test.setTimeout(1200000);
  test.skip(process.env.MEDRAG_E2E_LIVE !== '1', 'Requires the running API, worker, dispatcher and retrieval service.');
  const credentials = JSON.parse(process.env.MEDRAG_DEV_PRINCIPALS ?? '[]') as { token: string; role: string }[];
  const token = credentials.find(item => item.role === 'admin')?.token;
  if (!token) throw new Error('Provision development credentials and pass the environment to Playwright.');

  const title = 'Retrieval fixture ' + randomUUID().slice(0, 8);
  const fixture = resolve('../backend/tests/fixtures/parsing/table.pdf');
  const buffer = Buffer.concat([readFileSync(fixture), Buffer.from('\n%% unique ' + randomUUID() + '\n')]);

  await page.goto('/library');
  await page.getByLabel('Access key').fill(token);
  await page.getByRole('button', { name: 'Open workspace' }).click();
  await page.getByLabel('Title', { exact: true }).fill(title);
  await page.getByRole('combobox', { name: 'Source type', exact: true }).selectOption('TEXTBOOK');
  await page.getByRole('combobox', { name: 'Authority level', exact: true }).selectOption('UNREVIEWED');
  await page.getByLabel('Choose PDF files or drag them here').setInputFiles({
    name: 'retrieval-fixture.pdf', mimeType: 'application/pdf', buffer,
  });
  await page.getByRole('button', { name: 'Upload documents' }).click();
  await expect(page.getByRole('status')).toContainText('Upload recorded', { timeout: 60000 });
  await page.getByRole('link', { name: 'Open uploaded document' }).click();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();

  // The whole pipeline runs for real: parse, chunk, embed, index, then build the lexical index.
  await expect(page.locator('.version-card').getByText('retrieval ready').first()).toBeVisible({
    timeout: 1080000,
  });
  await page.screenshot({ path: testInfo.outputPath('retrieval-ready.png'), fullPage: true });

  await page.getByRole('link', { name: 'Retrieval inspector' }).click();
  await expect(page.getByRole('heading', { name: 'Retrieval inspector' })).toBeVisible();
  // The page states what it is before it states anything else.
  await expect(page.getByRole('heading', { name: 'Retrieved evidence candidates' })).toBeVisible();
  await expect(page.getByText(/It does not answer medical questions/)).toBeVisible();
  await expect(page.getByText('Corpus available to this account')).toBeVisible({ timeout: 30000 });

  await page.getByLabel('Retrieval mode').selectOption('HYBRID_RRF');
  await page.getByLabel('Query text').fill('Parameter Group Alpha Units');
  await page.getByRole('button', { name: 'Retrieve candidates' }).click();

  const fused = page.locator('.panel', { hasText: 'Fused candidates' });
  await expect(fused).toBeVisible({ timeout: 120000 });
  const candidate = fused.locator('.version-card').first();
  await expect(candidate).toBeVisible();
  // Lane diagnostics are shown as diagnostics, with both lanes named.
  await expect(candidate.getByText(/fused 0\./)).toBeVisible();
  await expect(candidate.getByText(/lanes /)).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('retrieval-hybrid.png'), fullPage: true });

  // Each lane can be inspected on its own, which is what makes a comparison possible at all.
  await page.getByRole('button', { name: /^Dense \(/ }).click();
  await expect(page.getByRole('heading', { name: 'Dense lane' })).toBeVisible();
  await page.getByRole('button', { name: /^BM25 \(/ }).click();
  await expect(page.getByRole('heading', { name: 'BM25 lane' })).toBeVisible();
  await expect(page.getByText(/must not be compared with each other/)).toBeVisible();
  await page.getByRole('button', { name: /^Hybrid \(/ }).click();

  // The trace carries a hash of the query, never the query.
  const trace = page.locator('.panel', { hasText: 'Execution trace' });
  await expect(trace.getByText('HYBRID_RRF')).toBeVisible();
  await expect(trace.getByText('Query hash')).toBeVisible();
  await expect(trace.getByText('Parameter Group Alpha Units')).toHaveCount(0);

  await candidate.getByRole('button', { name: 'Inspect provenance' }).click();
  const provenance = page.getByLabel('Candidate provenance');
  await expect(provenance).toBeVisible();
  await expect(provenance.getByText('Chunk type')).toBeVisible();
  await expect(provenance.getByText('Pages')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('retrieval-provenance.png'), fullPage: true });

  // Candidate -> chunk -> original source page, through the real UI.
  await provenance.getByRole('link', { name: 'Open chunk inspector' }).click();
  await expect(page.getByRole('heading', { name: 'Chunk inspector' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Selected chunk' })).toBeVisible({ timeout: 30000 });
  await page.getByRole('link', { name: 'Open source page' }).first().click();
  await expect(page.getByRole('heading', { name: 'Parse inspector' })).toBeVisible();
  await expect(page.getByRole('img', { name: /Rendered preview of page/ }).first()).toBeVisible({ timeout: 60000 });
  await page.screenshot({ path: testInfo.outputPath('retrieval-source-page.png'), fullPage: true });
});

test('the retrieval inspector never presents an answer, and Ask stays disabled', async ({ page }) => {
  test.skip(process.env.MEDRAG_E2E_LIVE !== '1', 'Requires the running API.');
  const credentials = JSON.parse(process.env.MEDRAG_DEV_PRINCIPALS ?? '[]') as { token: string; role: string }[];
  const token = credentials.find(item => item.role === 'admin')?.token;
  if (!token) throw new Error('Provision development credentials and pass the environment to Playwright.');

  await page.goto('/retrieval');
  await page.getByLabel('Access key').fill(token);
  await page.getByRole('button', { name: 'Open workspace' }).click();
  await expect(page.getByRole('heading', { name: 'Retrieval inspector' })).toBeVisible();

  // No answering affordance of any kind exists on the retrieval page.
  await expect(page.getByRole('button', { name: /^Ask/ })).toHaveCount(0);
  await expect(page.getByText(/confidence/i)).toHaveCount(0);
  await expect(page.getByText(/hallucination/i)).toHaveCount(0);
  await expect(page.getByRole('heading', { name: /^Answer/ })).toHaveCount(0);

  // And the Ask page is still closed, with the reason stated rather than implied.
  await page.getByRole('link', { name: 'Ask', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Answering is not available' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Ask with evidence' })).toBeDisabled();
  await expect(page.getByPlaceholder('Ask a question about your source material…')).toBeDisabled();
});
