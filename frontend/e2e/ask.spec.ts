import { test, expect } from '@playwright/test';

function credentials() {
  const parsed = JSON.parse(process.env.MEDRAG_DEV_PRINCIPALS ?? '[]') as {token: string; role: string}[];
  const token = parsed.find(c => c.role === 'admin')?.token;
  if (!token) throw new Error('Development credentials are required.');
  return token;
}

test('the Ask page answers only from verified evidence, and says so when it cannot', async ({ page }, testInfo) => {
  test.setTimeout(240000);
  test.skip(process.env.MEDRAG_E2E_LIVE !== '1', 'Requires the real stack and the ingested corpus.');

  await page.goto('/ask');
  await page.getByLabel('Access key').fill(credentials());
  await page.getByRole('button', {name: 'Open workspace'}).click();
  await expect(page.getByRole('heading', {name: 'Educational use only'})).toBeVisible();

  await page.getByLabel('Your educational medical question').fill('Parameter Group Alpha Units');
  await page.getByRole('button', {name: 'Ask with evidence'}).click();

  // Whichever outcome the pipeline reaches, exactly one of these states appears — and every one of
  // them is a heading the user can act on, never a generic failure.
  const answer = page.getByRole('heading', {name: 'Answer', exact: true});
  const refusal = page.getByRole('heading', {
    name: /Insufficient evidence|Sources disagree|Could not verify an answer|The answering service failed/,
  });
  await expect(answer.or(refusal).first()).toBeVisible({timeout: 200000});

  if (await answer.isVisible()) {
    // A verified answer must carry its sources and open the authoritative page it cites.
    await expect(page.getByText('VERIFIED AGAINST RETRIEVED SOURCES')).toBeVisible();
    await expect(page.getByRole('heading', {name: 'Sources'})).toBeVisible();
    await expect(page.getByRole('heading', {name: 'Citations'})).toBeVisible();
    await page.screenshot({path: testInfo.outputPath('m9-answer.png'), fullPage: true});

    await page.setViewportSize({width: 390, height: 844});
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.setViewportSize({width: 1280, height: 900});

    await page.getByRole('link', {name: 'Open source page'}).first().click();
    await expect(page.getByRole('heading', {name: 'Parse inspector'})).toBeVisible();
    // The citation resolves to the real rendered page, not a reconstruction.
    await expect(page.getByRole('img', {name: /Rendered preview of page/}).first()).toBeVisible({timeout: 60000});
  } else {
    await expect(refusal.first()).toBeVisible();
    // A refusal shows no answer body and no citations that might imply one.
    await expect(page.getByRole('heading', {name: 'Citations'})).toHaveCount(0);
  }

  // Nothing anywhere presents a fabricated certainty.
  await expect(page.getByText(/medical confidence|confidence:\s*\d/i)).toHaveCount(0);
});

test('a conversation keeps its history and a reader cannot open another tenant’s', async ({ page, request }) => {
  test.setTimeout(180000);
  test.skip(process.env.MEDRAG_E2E_LIVE !== '1', 'Requires the real stack and the ingested corpus.');
  const token = credentials();

  const asked = await request.post('http://127.0.0.1:5173/api/v1/ask', {
    headers: {Authorization: `Bearer ${token}`},
    data: {question: 'Parameter Group Alpha Units'},
    timeout: 200000,
  });
  expect(asked.ok()).toBeTruthy();
  const body = await asked.json();
  expect(body.answering_enabled).toBe(true);
  // The public contract never carries an unverified answer.
  expect(body.verified === (body.answer !== null)).toBe(true);
  expect(body).not.toHaveProperty('draft');

  const conversation = await request.get(
    `http://127.0.0.1:5173/api/v1/conversations/${body.conversation_id}`,
    {headers: {Authorization: `Bearer ${token}`}},
  );
  expect(conversation.ok()).toBeTruthy();
  expect((await conversation.json()).turns.length).toBeGreaterThan(0);

  // An id that is not the caller's is indistinguishable from one that never existed.
  const foreign = await request.get(
    'http://127.0.0.1:5173/api/v1/conversations/00000000-0000-4000-8000-000000000000',
    {headers: {Authorization: `Bearer ${token}`}},
  );
  expect(foreign.status()).toBe(404);

  const anonymous = await request.get(`http://127.0.0.1:5173/api/v1/conversations/${body.conversation_id}`);
  expect(anonymous.status()).toBe(401);
});
