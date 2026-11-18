import { expect, test } from '@playwright/test';

test('Agent keeps one recent creation and resumes its context across three turns', async ({ page, request }) => {
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
  await page.locator('.entry-mode-trigger').click();
  await page.getByRole('menuitem', { name: /Agent 模式/ }).click();
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
  const firstCard = page.getByRole('button', {name: /大纲 V1/});
  await expect(firstCard).toBeVisible();
  const first = (await getWork()).latest_outline;
  await page.getByLabel('继续对话').fill('将渠道分析放最后');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(page.getByRole('button', {name: /大纲 V2/})).toBeVisible();
  const latest = await getWork();
  expect(latest.latest_outline.document.slides[1].title).toBe('渠道分析');
  const stale = await request.post(workPath + '/confirm', {data: {outline_id:first.id, content_hash:first.content_hash}});
  expect(stale.status()).toBe(409);
  await page.reload();
  await firstCard.click();
  const historical = firstCard.locator('..').locator('ol > li');
  await expect(historical.first()).toContainText('渠道分析');
  await expect(historical.last()).toContainText('行动计划');
  await page.locator('.workbench').evaluate(element => { element.scrollTop = element.scrollHeight; });
  await checkDock();
  await page.setViewportSize({width: 390, height: 640});
  await checkDock();
  await page.setViewportSize({width: 1280, height: 560});
  await checkDock();
  await expect(recent).toHaveCount(1);
  await page.getByRole('button', {name: '确认并生成 2 页'}).click();
  await expect.poll(async () => (await getWork()).presentation.status, {timeout:30000}).toBe('READY');
  const secondPage = page.locator('.page-thumb').nth(1);
  await secondPage.click();
  await expect(page.getByRole('button', {name:'第 2 页 · 移除引用'})).toBeInViewport();
  await checkDock();
  await input.fill('请核对当前页数');
  await input.press('Control+Enter');
  await expect(page.getByText('面向董事会，当前作品有 2 页。', {exact: true})).toBeInViewport();
  await expect(secondPage).toHaveClass(/active/);
});
