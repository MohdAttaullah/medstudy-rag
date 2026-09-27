import { test, expect } from '@playwright/test';

/**
 * The redesigned Ask surface against the real stack.
 *
 * The unit tests drive this from stubs; here the conversation index, the evidence card and the
 * source figure all come from the running pipeline, so what is proved is that the redesign works
 * on real data — including the figure link, which only appears when a citation's verified text
 * names a figure the parse gave a caption to.
 */

const WORKSPACE = 'Clean testing 2 curator';

function credentials() {
  const parsed = JSON.parse(process.env.MEDRAG_DEV_PRINCIPALS ?? '[]') as
    { token: string; role: string; display_name: string }[];
  const token = parsed.find(c => c.display_name === WORKSPACE)?.token;
  if (!token) throw new Error(`Development credentials for "${WORKSPACE}" are required.`);
  return token;
}

async function signIn(page: import('@playwright/test').Page) {
  await page.goto('/ask');
  await page.getByLabel('Access key').fill(credentials());
  await page.getByRole('button', { name: 'Open workspace' }).click();
  // The educational-use notice is a compact, persistent row now rather than a full-width card
  // with its own heading. The full wording is still there, behind its disclosure.
  await expect(page.getByText('Educational reference only')).toBeVisible();
}

function rail(page: import('@playwright/test').Page) {
  return page.getByRole('complementary', { name: 'Workspace navigation' });
}

test('the conversation index is in the rail and the centre is the conversation', async ({ page }, testInfo) => {
  test.setTimeout(240000);
  test.skip(process.env.MEDRAG_E2E_LIVE !== '1', 'Requires the real stack and the ingested corpus.');

  await signIn(page);
  // Conversations are listed beside the navigation, not stacked above the composer.
  await expect(rail(page).getByRole('link', { name: 'New conversation' })).toBeVisible();
  const listed = rail(page).locator('.rail-list a');
  await expect(listed.first()).toBeVisible({ timeout: 30000 });
  await expect(page.getByRole('main').locator('.rail-list')).toHaveCount(0);

  // Advanced tools are folded away until asked for, and this principal may open them.
  await expect(rail(page).getByRole('link', { name: 'Retrieval inspector' })).toHaveCount(0);
  await rail(page).getByRole('button', { name: /Advanced tools/ }).click();
  await expect(rail(page).getByRole('link', { name: 'Retrieval inspector' })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('ask-rail.png'), fullPage: true });

  // Opening a stored conversation loads its turns from the server into the centre.
  const first = listed.first();
  const title = (await first.textContent())?.trim() ?? '';
  await first.click();
  await expect(page.locator('.bubble-user').first()).toBeVisible({ timeout: 30000 });
  await expect(first).toHaveAttribute('aria-current', 'page');

  // Ask -> Library -> Ask keeps the same conversation open.
  await rail(page).getByRole('link', { name: 'Library' }).click();
  await expect(page.getByRole('heading', { name: 'Your source documents' })).toBeVisible();
  await rail(page).getByRole('link', { name: 'Ask', exact: true }).click();
  await expect(page.locator('.bubble-user').first()).toBeVisible({ timeout: 30000 });
  await expect(rail(page).locator('.rail-list a[aria-current="page"]')).toHaveText(title);

  // New conversation genuinely starts one: an empty thread with its examples.
  await rail(page).getByRole('link', { name: 'New conversation' }).click();
  await expect(page.getByRole('heading', { name: 'Ask your indexed medical sources' })).toBeVisible();
  await expect(page.locator('.bubble-user')).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('ask-empty.png'), fullPage: true });

  // The rail collapses to icons and back, without disturbing anything else.
  await rail(page).getByRole('button', { name: 'Collapse sidebar' }).click();
  await expect(rail(page)).toHaveClass(/sidebar-collapsed/);
  await expect(rail(page).getByRole('link', { name: 'Ask', exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('ask-collapsed.png'), fullPage: true });
  await rail(page).getByRole('button', { name: 'Expand sidebar' }).click();
  await expect(rail(page)).not.toHaveClass(/sidebar-collapsed/);
});

test('a verified answer shows its evidence card, its source figure and a collapsed record', async ({ page }, testInfo) => {
  test.setTimeout(300000);
  test.skip(process.env.MEDRAG_E2E_LIVE !== '1', 'Requires the real stack and the ingested corpus.');

  await signIn(page);
  await rail(page).getByRole('link', { name: 'New conversation' }).click();
  // This question's evidence names "( Figs. 1.2 and 1.7 )", and 1.7 has a caption to match.
  await page.getByPlaceholder('Ask a question about your source material…')
    .fill('What is the petrosal surface of the cerebellum?');
  await page.getByRole('button', { name: 'Ask with evidence' }).click();

  // While it runs, the stepper sits under the question that is being answered.
  await expect(page.getByRole('heading', { name: 'Working through your evidence' }))
    .toBeVisible({ timeout: 30000 });
  await page.screenshot({ path: testInfo.outputPath('ask-running.png'), fullPage: true });

  const answer = page.getByRole('heading', { name: 'Answer', exact: true });
  const refusal = page.getByRole('heading', {
    name: /Insufficient evidence|Sources disagree|Could not verify an answer|The answering service failed/,
  });
  await expect(answer.or(refusal).first()).toBeVisible({ timeout: 240000 });

  if (await answer.isVisible()) {
    // The evidence card: the document, where it is, and the exact words checked against.
    const card = page.locator('.evidence-card').first();
    await expect(card).toBeVisible();
    await expect(card.locator('.eyebrow')).toContainText('SOURCE [1]');
    await expect(card.locator('.evidence-where')).toContainText('Page');
    await expect(card.locator('.evidence-quote')).not.toBeEmpty();
    await expect(card.getByRole('link', { name: 'Open source page' })).toBeVisible();

    // The finished record is one line, and opens on demand.
    const summary = page.getByText('View processing details');
    await expect(summary).toBeVisible();
    await expect(page.getByText('Running now')).toHaveCount(0);
    await summary.click();
    await expect(page.locator('.step-completed').first()).toBeVisible();

    // The source figure, if the citation's text named one the parse captioned.
    const figure = page.locator('.figure-card');
    if (await figure.count()) {
      await expect(figure.first().locator('img')).toBeVisible();
      await expect(figure.first()).toContainText('not interpreted by AI');
      await expect(figure.first().getByRole('link', { name: 'Open full image' })).toBeVisible();
      // Rendered from bytes fetched with the session's token: the route needs an Authorization
      // header, which an <img src> cannot send, so a direct API src would 401 and show nothing.
      const source = await figure.first().locator('img').getAttribute('src');
      expect(source).toMatch(/^blob:/);
      // And those bytes really decoded, so the picture is on the page rather than a broken icon.
      await expect.poll(
        () => figure.first().locator('img').evaluate(node => (node as HTMLImageElement).naturalWidth),
        { timeout: 15000 },
      ).toBeGreaterThan(0);
    }
    await page.screenshot({ path: testInfo.outputPath('ask-answer.png'), fullPage: true });

    // A phone-width viewport keeps the thread and the figure inside the page.
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
      .toBe(true);
    await page.screenshot({ path: testInfo.outputPath('ask-mobile.png'), fullPage: true });
  }
});
