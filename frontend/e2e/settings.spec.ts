import { test, expect } from '@playwright/test';

function token(role: string) {
  const values = JSON.parse(process.env.MEDRAG_DEV_PRINCIPALS ?? '[]') as {token:string;role:string}[];
  const found = values.find(v => v.role === role);
  if (!found) throw new Error('Development credentials are required.');
  return found.token;
}

test('admin previews and applies a tenant policy with explicit confirmation', async ({page, request}, info) => {
  test.skip(process.env.MEDRAG_E2E_LIVE !== '1', 'Requires the running M10 stack.');
  const headers = {Authorization:'Bearer '+token('admin')};
  const response = await request.get('http://127.0.0.1:5173/api/v1/settings',{headers});
  expect(response.ok()).toBe(true);
  const before = await response.json();
  const value = before.settings.find((s:{key:string})=>s.key==='retrieval.rrf_k').effective_value as number;
  const next = value === 1000 ? 999 : value + 1;
  let changed = false;
  try {
    await page.goto('/settings');
    await page.getByLabel('Access key').fill(token('admin'));
    await page.getByRole('button',{name:'Open workspace'}).click();
    await page.getByRole('button',{name:'Retrieval',exact:true}).click();
    await page.getByLabel('Find a setting').fill('rrf k');
    await page.getByRole('button',{name:'Edit retrieval / rrf k'}).click();
    await page.getByLabel('Desired value').fill(String(next));
    await page.getByRole('button',{name:'Preview change'}).click();
    const apply = page.getByRole('button',{name:'Apply confirmed change'});
    await expect(apply).toBeDisabled();
    await page.getByLabel('I confirm this change and its impact.').check();
    await apply.click();
    await expect(page.getByRole('status')).toContainText('Subsequent requests use this policy.');
    changed = true;
    await expect(page.getByRole('heading',{name:`Revision ${before.revision+1} · ACTIVE`})).toBeVisible();
    await page.screenshot({path:info.outputPath('m10-settings-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:info.outputPath('m10-settings-mobile.png'),fullPage:true});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
    await page.getByLabel('Find a setting').fill('');
    await page.getByRole('button',{name:'Chunking',exact:true}).click();
    await page.getByRole('button',{name:'Edit chunking / child target tokens'}).click();
    await expect(page.getByText(/Create a new chunk run/).first()).toBeVisible();
    await page.getByRole('button',{name:'Cancel',exact:true}).click();
    await page.getByRole('button',{name:'Safety',exact:true}).click();
    await expect(page.getByRole('button',{name:'Edit ask / requires verified pass'})).toHaveCount(0);
  } finally {
    if(changed) {
      const latest = await (await request.get('http://127.0.0.1:5173/api/v1/settings',{headers})).json();
      // Do not overwrite a concurrent administrator's revision.
      if(latest.revision === before.revision+1) {
        const change={expected_revision:latest.revision,changes:[{key:'retrieval.rrf_k',value}]};
        const preview=await (await request.post('http://127.0.0.1:5173/api/v1/settings/preview',{headers,data:change})).json();
        const restored=await request.post('http://127.0.0.1:5173/api/v1/settings/changes',{headers,data:{...change,preview_token:preview.preview_token,confirmed:true,reason:'Restore policy after M10 browser verification'}});
        expect(restored.ok()).toBe(true);
      }
    }
  }
});

test('reader cannot administer configuration or fetch its history',async({page,request})=>{
  test.skip(process.env.MEDRAG_E2E_LIVE !== '1','Requires the running M10 stack.');
  await page.goto('/settings');
  await page.getByLabel('Access key').fill(token('reader'));
  await page.getByRole('button',{name:'Open workspace'}).click();
  await expect(page.getByRole('heading',{name:'Administrator access required'})).toBeVisible();
  const headers={Authorization:'Bearer '+token('reader')};
  expect((await request.get('http://127.0.0.1:5173/api/v1/settings/history',{headers})).status()).toBe(403);
  expect((await request.post('http://127.0.0.1:5173/api/v1/settings/changes',{headers,data:{expected_revision:0,changes:[{key:'retrieval.rrf_k',value:2}],preview_token:'0'.repeat(64),confirmed:true}})).status()).toBe(403);
});
