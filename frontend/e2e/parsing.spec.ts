import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { randomUUID } from 'node:crypto';

/**
 * End-to-end M2 path against the running stack: upload a synthetic structural fixture, wait for
 * the worker to reach READY_FOR_CHUNKING, then inspect the parsed page, elements, table, figure
 * and formula through the real UI.
 */
test('a real upload is parsed and its structure is inspectable in the UI', async ({ page }, testInfo) => {
  test.setTimeout(600000);
  test.skip(process.env.MEDRAG_E2E_LIVE !== '1', 'Requires the running API, parsing worker and dispatcher.');
  const credentials = JSON.parse(process.env.MEDRAG_DEV_PRINCIPALS ?? '[]') as { token: string; role: string }[];
  const token = credentials.find(item => item.role === 'admin')?.token;
  if (!token) throw new Error('Provision development credentials and pass the environment to Playwright.');

  const title = 'Parse fixture ' + randomUUID().slice(0, 8);
  const fixture = resolve('../backend/tests/fixtures/parsing/table.pdf');
  const buffer = Buffer.concat([readFileSync(fixture), Buffer.from('\n%% unique ' + randomUUID() + '\n')]);

  await page.goto('/library');
  await page.getByLabel('Access key').fill(token);
  await page.getByRole('button', { name: 'Open workspace' }).click();
  await page.getByLabel('Title', { exact: true }).fill(title);
  await page.getByRole('combobox', { name: 'Source type', exact: true }).selectOption('TEXTBOOK');
  await page.getByRole('combobox', { name: 'Authority level', exact: true }).selectOption('UNREVIEWED');
  await page.getByLabel('Choose PDF files or drag them here').setInputFiles({
    name: 'parse-fixture.pdf', mimeType: 'application/pdf', buffer,
  });
  await page.getByRole('button', { name: 'Upload documents' }).click();
  await expect(page.getByRole('status')).toContainText('Upload recorded', { timeout: 60000 });
  await page.getByRole('link', { name: 'Open uploaded document' }).click();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();

  // The worker loads parser weights on its first document, so allow a generous wait. The
  // lifecycle reports the stage done from the server's own state; M3 chunks straight after.
  await expect(page.locator('.lc-step', { hasText: 'Reading the document' })).toContainText('Completed', { timeout: 600000 });
  await page.locator('.version-card summary', { hasText: 'Findings and technical details' }).first().click();
  const parsing = page.locator('.parse-summary');
  await expect(parsing.getByText(/^docling /)).toBeVisible();
  await expect(parsing.getByText('parsing-m2-v1', { exact: false })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('parse-summary.png'), fullPage: true });

  await parsing.getByRole('link', { name: 'Open parse inspector' }).click();
  await expect(page.getByRole('heading', { name: 'Parse inspector' })).toBeVisible();
  await expect(page.getByText('1 (1-based, printed order)')).toBeVisible();
  await expect(page.getByAltText(/Rendered preview of page 1/)).toBeVisible({ timeout: 30000 });

  const elements = page.locator('.element-list').first();
  await expect(elements.getByText('Heading', { exact: true }).first()).toBeVisible();
  await expect(elements.getByText('Table', { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/\(TOPLEFT, pt\)/).first()).toBeVisible();

  // Cell contents and grid geometry are stable; whether the model marks row 0 as a header row
  // is a layout judgement that differs between CPU environments, so the cells are asserted by
  // text rather than by header role.
  const grid = page.locator('table.parse-table').first();
  for (const cell of ['Parameter', 'Group A', 'Group B', 'Units', 'Alpha', 'Gamma']) {
    await expect(grid.getByText(cell, { exact: true })).toBeVisible();
  }
  await expect(page.getByText('4 × 4', { exact: true })).toBeVisible();
  await expect(page.getByText(/Page 1 · \d+ header row\(s\) · 16 cells/)).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('parse-inspector.png'), fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('parse-inspector-mobile.png'), fullPage: true });

  // Parsed material is provenance, not answerable evidence.
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.getByRole('link', { name: 'Ask', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Ask with evidence' })).toBeDisabled();
});

test('operations exposes the real parse stages', async ({ page }) => {
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
  for (const stage of ['PARSING', 'NORMALIZING', 'ENRICHING', 'READY_FOR_CHUNKING']) {
    expect(options).toContain(stage);
  }
  // M3 made the chunk stages real and M4 the embedding and index stages. READY stays
  // unreachable: it is reserved for a version that can actually be answered from.
  expect(options).not.toContain('READY');
});
