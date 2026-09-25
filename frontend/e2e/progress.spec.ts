import { test, expect } from '@playwright/test';

/**
 * Live stage progress against the real stack.
 *
 * The unit tests drive the stepper from synthetic frames; this one watches the pipeline itself, so
 * what it proves is that the events a real request emits reach the interface while that request is
 * still running — the thing a static stage list could never show.
 *
 * Scoped to the Clean testing workspace by name rather than by taking the first admin credential,
 * because a live Ask consumes a provider call and must land in the intended tenant.
 */

const WORKSPACE = 'Clean testing 2 curator';

function credentials() {
  const parsed = JSON.parse(process.env.MEDRAG_DEV_PRINCIPALS ?? '[]') as
    { token: string; role: string; display_name: string }[];
  const token = parsed.find(c => c.display_name === WORKSPACE)?.token;
  if (!token) throw new Error(`Development credentials for "${WORKSPACE}" are required.`);
  return token;
}

test('the stepper reports the stage the pipeline is actually running', async ({ page }, testInfo) => {
  test.setTimeout(300000);
  test.skip(process.env.MEDRAG_E2E_LIVE !== '1', 'Requires the real stack and the ingested corpus.');

  await page.goto('/ask');
  await page.getByLabel('Access key').fill(credentials());
  await page.getByRole('button', { name: 'Open workspace' }).click();
  await expect(page.getByRole('heading', { name: 'Educational use only' })).toBeVisible();

  await page.getByLabel('Your educational medical question')
    .fill('What is the tentorial surface of the cerebellum?');
  await page.getByRole('button', { name: 'Ask with evidence' }).click();

  // While the request runs: the stepper exists, some stage is running, and earlier ones are done.
  const stepper = page.getByRole('heading', { name: 'Working through your evidence' });
  await expect(stepper).toBeVisible({ timeout: 30000 });
  await expect(page.locator('.step-running').first()).toBeVisible({ timeout: 30000 });
  await expect(page.locator('.step-completed').first()).toBeVisible({ timeout: 60000 });
  // The running stage says how long it has been running, which is what answers "is it stuck?".
  await expect(page.getByText(/Current stage: \d+\.\d s/)).toBeVisible();
  // A second submission is refused while one is in flight.
  await expect(page.getByRole('button', { name: /…$/ })).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath('progress-running.png'), fullPage: true });

  // Reranking is the long stage on this machine, so its "still working" note is reachable.
  const reranking = page.locator('.step', { hasText: 'Selecting the most relevant passages' });
  if (await reranking.locator('.step-state', { hasText: 'Running now' }).isVisible()) {
    await expect(reranking).toContainText('Reranking can take several seconds');
  }

  const answer = page.getByRole('heading', { name: 'Answer', exact: true });
  const refusal = page.getByRole('heading', {
    name: /Insufficient evidence|Sources disagree|Could not verify an answer|The answering service failed/,
  });
  await expect(answer.or(refusal).first()).toBeVisible({ timeout: 240000 });

  // After the request: every stage has settled, and none is left spinning.
  await expect(page.getByRole('heading', { name: 'How this answer was produced' })).toBeVisible();
  await expect(page.locator('.step-running')).toHaveCount(0);
  await expect(page.locator('.step-pending')).toHaveCount(0);
  await expect(page.locator('.step-completed')).toHaveCount(7);
  await page.screenshot({ path: testInfo.outputPath('progress-complete.png'), fullPage: true });

  // The admin timing breakdown reports the whole request, verification included.
  await page.getByText('Timing', { exact: true }).click();
  await expect(page.getByRole('row', { name: /Verification/ })).toBeVisible();
  await expect(page.getByRole('row', { name: /Total/ })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('progress-timing.png'), fullPage: true });
});

test('an out-of-scope question skips the stages it never ran', async ({ page }, testInfo) => {
  test.setTimeout(120000);
  test.skip(process.env.MEDRAG_E2E_LIVE !== '1', 'Requires the real stack.');

  await page.goto('/ask');
  await page.getByLabel('Access key').fill(credentials());
  await page.getByRole('button', { name: 'Open workspace' }).click();
  await page.getByLabel('Your educational medical question')
    .fill('What antibiotic should I take for meningitis?');
  await page.getByRole('button', { name: 'Ask with evidence' }).click();

  await expect(page.getByRole('heading', { name: 'Not a question this workspace answers' }))
    .toBeVisible({ timeout: 60000 });
  // Five stages were never run, and say so rather than sitting pending forever.
  await expect(page.locator('.step-skipped')).toHaveCount(5);
  await expect(page.locator('.step', { hasText: 'Searching indexed sources' })).toContainText('Skipped');
  await page.screenshot({ path: testInfo.outputPath('progress-out-of-scope.png'), fullPage: true });
});
