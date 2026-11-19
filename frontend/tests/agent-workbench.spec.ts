import { expect, test } from '@playwright/test';

test('Agent produces two independently accessible decks from one conversation', async ({ page, request }) => {
  test.setTimeout(90000);
  const prefix = '/api/v1/settings/llm';
  const provider = await (await request.post(prefix + '/providers', { data: {
    name: 'Agent test', base_url: 'http://127.0.0.1:' + (process.env.E2E_API_PORT || '18000') + '/mock/v1', auth_mode: 'none',
  }})).json();
  const model = await (await request.post(prefix + '/providers/' + provider.id + '/models', {
    data: { remote_model_id: 'agent-test' },
  })).json();
  expect((await (await request.post(prefix + '/models/' + model.id + '/test')).json()).status).toBe('passed');
  const routes = (await (await request.get(prefix + '/business-routes')).json()).items;
  const route = routes.find((item: {code: string}) => item.code === 'default');
  expect((await request.put(prefix + '/business-routes/default', { data: {
    expected_revision: route.revision, model_id: model.id, reasoning_tier: 'off',
  }})).ok()).toBeTruthy();
  await page.goto('/');
  const subject = '准备给董事会的年度汇报';
  const recent = page.locator('.history-list button').filter({ hasText: subject });
  await expect(recent).toHaveCount(0);
  await page.locator('.composer textarea').fill(subject);
  await page.getByRole('button', { name: '开始创作' }).click();
  await expect(page.getByText('这次汇报希望董事会决定什么？')).toBeVisible();
  const checkDock = async () => {
    const offset = await page.evaluate(() => {
      const rect = (selector: string) => document.querySelector(selector)!.getBoundingClientRect();
      const composer = rect('.agent-composer');
      const cards = [rect('.chat-message.from-user'), rect('.chat-message.from-assistant')];
      return Math.max(Math.abs(rect('.agent-dock').bottom - window.innerHeight),
        ...cards.flatMap(card => [Math.abs(card.left - composer.left), Math.abs(card.width - composer.width)]));
    });
    expect(offset).toBeLessThan(2);
  };
  await checkDock();
  const input = page.getByLabel('继续对话');
  const shortHeight = (await input.boundingBox())!.height;
  await input.fill(Array(20).fill('多行输入').join('\n'));
  expect((await input.boundingBox())!.height).toBeGreaterThan(shortHeight);
  expect((await input.boundingBox())!.height).toBeLessThan(130);
  await input.fill('');
  expect((await input.boundingBox())!.height).toBe(shortHeight);
  await page.getByLabel('继续对话').fill('请核对当前页数');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  const reply = page.getByText('面向董事会，当前作品有 0 页。', { exact: true });
  await expect(reply).toHaveCount(1);
  await page.getByRole('button', { name: '模型与 API', exact: true }).click();
  await recent.click();
  await page.reload();
  await expect(reply).toBeVisible();
  await page.getByLabel('继续对话').fill('再核对一次');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(reply).toHaveCount(2);
  await expect(recent).toHaveCount(1);
  await expect(page.locator('.agent-observation')).toHaveCount(2);
  const workId = await page.evaluate(() => sessionStorage.getItem('lumina.activePresentation'));
  const workPath = '/api/v1/presentations/' + workId;
  const getWork = async () => (await request.get(workPath)).json();
  expect((await getWork()).latest_run).toBeNull();
  await page.getByLabel('继续对话').fill('请起草整份大纲');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  const firstCard = page.getByRole('article', {name:'大纲 V1', exact:true});
  await expect(firstCard.getByText('渠道分析', {exact:true})).toBeVisible();
  await expect(firstCard.getByRole('heading', {name:'作品 Brief'})).toBeVisible();
  await expect(page.getByRole('tablist', {name:'工作区视图'})).toHaveCount(0);
  const first = (await getWork()).latest_outline;
  await firstCard.getByRole('button', {name:'编辑大纲'}).click();
  await firstCard.getByLabel('制作目的').fill('供董事会决策的工作汇报');
  const cardTurns: string[] = [];
  page.on('request', sent => {
    if (sent.method() === 'POST' && new URL(sent.url()).pathname.endsWith('/turns')) {
      const action = sent.postDataJSON()?.action;
      if (action) cardTurns.push(action.type);
    }
  });
  await firstCard.getByRole('button', {name:'保存并生成 PPT'}).click();
  const secondCard = page.getByRole('article', {name:'大纲 V2', exact:true});
  await expect(secondCard.getByText('供董事会决策的工作汇报', {exact:true})).toBeVisible();
  await expect(secondCard.getByText('只读快照', {exact:false})).toBeVisible();
  await expect(firstCard.getByText('只读快照', {exact:false})).toBeVisible();
  await expect(firstCard.getByRole('textbox')).toHaveCount(0);
  const stale = await request.put(workPath + '/outline', {data: {outline_id:first.id, content_hash:first.content_hash, document:first.document}});
  expect(stale.status()).toBe(409);
  const firstPpt = page.getByRole('article', {name:'PPT · 大纲 V2', exact:true});
  await expect(firstPpt.getByText(/生成已完成/)).toBeVisible({timeout:30000});
  const firstBatch = (await getWork()).batch_id;
  const getBatch = async (id:string) => (await request.get(workPath + '/batches/' + id)).json();
  const firstResult = await getBatch(firstBatch);
  expect(firstResult.latest_outline.revision).toBe(2);
  await expect(firstPpt.getByRole('list', {name:'PPT 页面'}).locator(':scope > li')).toHaveCount(2);
  await firstPpt.getByRole('heading', {name:'1. 渠道分析'}).scrollIntoViewIfNeeded();
  await expect(firstPpt.frameLocator('iframe[title="第 1 页预览"]').getByRole('heading', {name:'渠道分析'})).toBeVisible();
  await firstPpt.getByRole('button', {name:'引用此页'}).nth(1).click();
  await expect(page.getByRole('button', {name:'第 2 页 · 移除引用'})).toBeInViewport();
  await checkDock();
  await input.fill('请核对当前页数');
  await input.press('Control+Enter');
  await expect(page.getByText('面向董事会，当前作品有 2 页。', {exact:true})).toBeInViewport();
  await page.getByRole('button', {name:'第 2 页 · 移除引用'}).click();
  const send = async (text:string) => {
    await input.fill(text);
    await page.getByRole('button', {name:'发送', exact:true}).click();
  };
  await send('把大纲 V2 改写为客户汇报大纲');
  const thirdCard = page.getByRole('article', {name:'大纲 V3', exact:true});
  await expect(thirdCard.getByText('当前草稿', {exact:false})).toBeVisible();
  await expect(thirdCard.getByRole('button', {name:'确认并生成 PPT'})).toBeEnabled();
  await send('按大纲 V3 生成 PPT');
  const secondPpt = page.getByRole('article', {name:'PPT · 大纲 V3', exact:true});
  await expect(secondPpt.getByRole('button', {name:'重试失败页'})).toBeVisible({timeout:30000});
  const secondBatch = (await getWork()).batch_id;
  expect(secondBatch).not.toBe(firstBatch);
  const failedResult = await getBatch(secondBatch);
  expect(failedResult.pages.map((entry:{status:string})=>entry.status)).toEqual(['SUCCEEDED','FAILED','SUCCEEDED']);
  await expect(firstPpt.getByRole('list', {name:'PPT 页面'}).locator(':scope > li')).toHaveCount(2);
  await expect(secondPpt.getByRole('list', {name:'PPT 页面'}).locator(':scope > li')).toHaveCount(3);
  await secondPpt.getByRole('button', {name:'重试失败页'}).click();
  await expect(secondPpt.getByText(/生成已完成/)).toBeVisible({timeout:30000});
  expect(cardTurns).toEqual(['compose_deck', 'retry_deck']);
  const secondResult = await getBatch(secondBatch);
  expect(secondResult.pages.map((entry:{title:string})=>entry.title)).toEqual(['客户现状','交付安排','下一步']);
  expect(secondResult.pages[0].run_id).toBe(failedResult.pages[0].run_id);
  expect(secondResult.pages[0].html_hash).toBe(failedResult.pages[0].html_hash);
  expect(secondResult.pages[1].run_id).not.toBe(failedResult.pages[1].run_id);
  expect(secondResult.pages[1].id).toBe(failedResult.pages[1].id);
  expect((await getBatch(firstBatch)).pages.map((entry:{html_hash:string})=>entry.html_hash)).toEqual(firstResult.pages.map((entry:{html_hash:string})=>entry.html_hash));
  await page.reload();
  await expect(page.locator('.outline-version-card')).toHaveCount(3);
  await expect(firstPpt.getByRole('heading', {name:'1. 渠道分析'})).toBeVisible();
  await expect(secondPpt.getByRole('heading', {name:'1. 客户现状'})).toBeVisible();
  await expect(page.locator('.conversation-feed > article.agent-artifact').locator('h2')).toHaveText([
    '大纲 V1', '大纲 V2', 'PPT · 大纲 V2', '大纲 V3', 'PPT · 大纲 V3',
  ]);
  await page.setViewportSize({width: 390, height: 640});
  await checkDock();
  await page.setViewportSize({width: 1440, height: 1000});
  await send('按大纲 V2 生成 PPT');
  await expect(page.getByText('这版大纲已确认，可从原 PPT 卡查看进度或重试。')).toBeVisible();
  await expect(page.locator('.agent-deck-card')).toHaveCount(2);
  await expect(recent).toHaveCount(1);
  const beforeExport = (await getWork()).conversation.messages;
  const listCalls = async () => (await (await request.get(prefix + '/calls?limit=100')).json()).items;
  const callsBeforeExport = await listCalls();
  const exported = [];
  for (const card of [firstPpt, secondPpt]) {
    await card.getByRole('button', {name:'导出 PPTX', exact:true}).click();
    const link = card.getByRole('link', {name:'下载 PPTX', exact:true});
    await expect(link).toBeVisible({timeout:20000});
    const url = await link.getAttribute('href');
    expect((await request.get(url!)).ok()).toBeTruthy();
    const [download] = await Promise.all([page.waitForEvent('download'), link.click()]);
    expect(download.suggestedFilename()).toMatch(/\.pptx$/i);
    exported.push(url);
  }
  expect(new Set(exported).size).toBe(2);
  expect((await getWork()).conversation.messages).toEqual(beforeExport);
  expect(await listCalls()).toEqual(callsBeforeExport);
  await page.reload();
  await expect(firstPpt.getByRole('link', {name:'下载 PPTX'})).toHaveAttribute('href', exported[0]!);
  await expect(secondPpt.getByRole('link', {name:'下载 PPTX'})).toHaveAttribute('href', exported[1]!);
});
