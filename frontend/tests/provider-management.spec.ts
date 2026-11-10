import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';
const apiOrigin = `http://127.0.0.1:${process.env.E2E_API_PORT || '18000'}`;

test('Provider configuration, model discovery, routing and presentation generation form a complete flow', async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/settings/providers');
  await expect(page.getByRole('heading', { name: '模型与 API', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '添加 Provider', exact: true }).last().click();
  await page.getByLabel('显示名称', { exact: true }).fill('E2E Provider');
  await page.getByLabel('API 根地址', { exact: true }).fill(`${apiOrigin}/mock/v1`);
  await page.getByLabel('认证方式', { exact: true }).selectOption('none');
  await page.getByRole('button', { name: '保存 Provider', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'E2E Provider', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '获取可用模型', exact: true }).click();
  const discovery = page.getByRole('dialog', { name: '导入可用模型' });
  await expect(discovery.getByText('接口未提供上下文', { exact: true })).toBeVisible();
  await discovery.getByRole('button', { name: '导入所选模型' }).click();
  await expect(discovery).toBeHidden();
  const directory = page.locator('.model-directory');
  await expect(directory.locator('tbody tr')).toHaveCount(2);
  for (const id of ['outline-model', 'html-model']) {
    const row = directory.locator('tbody tr').filter({ hasText: id });
    await row.getByRole('button', { name: '测试', exact: true }).click();
    await expect(row.getByText('结构化 · 通过', { exact: true })).toBeVisible();
  }
  await directory
    .locator('tbody tr')
    .filter({ hasText: 'html-model' })
    .getByRole('button', { name: '编辑', exact: true })
    .click();
  const editor = page.getByRole('dialog', { name: '模型设置' });
  await editor.getByLabel('上下文窗口（Token）').fill('64000');
  await editor.getByLabel('模型并发限制').fill('4');
  await editor.getByRole('button', { name: '保存模型信息' }).click();
  await expect(editor.getByText('模型信息已保存。')).toBeVisible();
  await editor.getByRole('button', { name: '添加档位' }).click();
  await editor.getByLabel('档位代码 5', { exact: true }).fill('deep');
  await editor.getByLabel('档位名称 5', { exact: true }).fill('Deep');
  await editor.getByLabel('API 参数值 5', { exact: true }).fill('xhigh');
  await editor.getByRole('button', { name: '保存思考档位' }).click();
  await expect(editor.getByText('思考档位已保存。')).toBeVisible();
  await editor.getByRole('button', { name: '关闭模型设置' }).click();
  const htmlRow = directory.locator('tbody tr').filter({ hasText: 'html-model' });
  await htmlRow.getByRole('button', { name: '测试', exact: true }).click();
  await expect(htmlRow.getByText('结构化 · 通过', { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('providers.png'), fullPage: true });

  await page.getByRole('button', { name: '业务路由', exact: true }).click();
  const defaultRoute = page
    .locator('.route-card')
    .filter({ has: page.getByRole('heading', { name: '全局默认', exact: true }) });
  await defaultRoute
    .getByLabel('模型', { exact: true })
    .selectOption({ label: 'Outline Model · outline-model' });
  await defaultRoute.getByLabel('思考预算', { exact: true }).selectOption('medium');
  await defaultRoute.getByRole('button', { name: '保存路由' }).click();
  await expect(defaultRoute.getByText('revision 2 · 已就绪', { exact: true })).toBeVisible();
  const htmlRoute = page
    .locator('.route-card')
    .filter({ has: page.getByRole('heading', { name: 'PPT 内容 HTML 生成', exact: true }) });
  await htmlRoute
    .getByLabel('模型', { exact: true })
    .selectOption({ label: 'HTML Model · html-model' });
  await htmlRoute.getByLabel('思考预算', { exact: true }).selectOption('deep');
  await htmlRoute.getByRole('button', { name: '保存路由' }).click();
  await expect(htmlRoute.getByText('revision 2 · 已就绪', { exact: true })).toBeVisible();
  await page.reload();
  await expect(htmlRoute.getByLabel('思考预算', { exact: true })).toHaveValue('deep');
  await page.screenshot({ path: testInfo.outputPath('routing.png'), fullPage: true });

  await page.getByRole('button', { name: '返回工作台', exact: true }).click();
  await page.locator('.composer textarea').fill('制作一个用于验收 Provider 管理闭环的演示文稿。');
  await page.getByRole('button', { name: '生成说明书', exact: true }).click();
  const outlineBullets = page.locator('.outline-bullets').first();
  await expect(outlineBullets).toBeVisible();
  const longOutline = Array.from(
    { length: 7 },
    (_, index) =>
      `要点 ${index + 1}：这是一条用于验证完整显示的长内容，确保在较窄卡片中换行后仍然可以完整阅读。`,
  ).join('\n');
  await outlineBullets.fill(longOutline);
  for (const viewport of [
    { width: 1440, height: 1000 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    const dimensions = await outlineBullets.evaluate((element: HTMLTextAreaElement) => ({
      scrollHeight: element.scrollHeight,
      clientHeight: element.clientHeight,
      overflowY: getComputedStyle(element).overflowY,
    }));
    expect(dimensions.scrollHeight - dimensions.clientHeight).toBeLessThanOrEqual(1);
    expect(dimensions.overflowY).toBe('hidden');
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: testInfo.outputPath('outline-full.png'), fullPage: true });
  await page.getByRole('button', { name: '确认并生成 1 页', exact: true }).click();
  await expect(page.getByText('页面已就绪', { exact: true })).toBeVisible();
  const historyItem = page.locator('.history-item').first();
  const historyIcon = await historyItem.locator('.history-mode-icon').boundingBox();
  const historyTitle = await historyItem.locator('span').last().boundingBox();
  expect(historyIcon).not.toBeNull();
  expect(historyTitle).not.toBeNull();
  expect(historyTitle!.x - (historyIcon!.x + historyIcon!.width)).toBeLessThanOrEqual(16);
  const generationTab = page.getByRole('tab', { name: /页面生成/ });
  await expect(generationTab).toHaveAttribute('aria-selected', 'true');
  const sceneCanvas = page.locator(
    '.canvas-stage .scene-viewport[data-render-status="ready"] canvas',
  );
  await expect(sceneCanvas).toBeVisible();
  await expect(page.getByRole('button', { name: '新版预览', exact: true })).toHaveClass('selected');
  const sceneId = await page.locator('.scene-viewport').getAttribute('data-scene-id');
  expect(sceneId).toBeTruthy();
  const sceneResponse = await page.request.get(`/api/v1/scenes/${sceneId}`);
  expect(sceneResponse.ok()).toBe(true);
  const scene = await sceneResponse.json();
  expect(scene.schemaVersion).toBe('0.3.0');
  expect(
    scene.nodes.some(
      (n: any) => n.kind === 'text' && n.text.sourceText.includes('Provider 验收完成'),
    ),
  ).toBe(true);
  await page.getByRole('button', { name: 'HTML 预览', exact: true }).click();
  const slideFrame = page.locator('.canvas-stage iframe.slide-frame');
  await expect(slideFrame).toBeVisible();
  const slide = await slideFrame.contentFrame();
  await expect(slide!.locator('[data-tailwind-profile="tailwind-ppt-v1"]')).toBeAttached();
  await expect
    .poll(() =>
      slide!.locator('#tailwind-proof').evaluate((element) => ({
        display: getComputedStyle(element.parentElement!).display,
        fontSize: getComputedStyle(element).fontSize,
        color: getComputedStyle(element).color,
      })),
    )
    .toEqual({ display: 'flex', fontSize: '32px', color: 'rgb(31, 41, 55)' });
  for (const viewport of [
    { width: 1440, height: 1000 },
    { width: 1793, height: 1100 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await expect
      .poll(async () => {
        const frame = await slideFrame.boundingBox();
        const stage = await page.locator('.canvas-stage').boundingBox();
        return Boolean(
          frame &&
          stage &&
          frame.x >= stage.x - 1 &&
          frame.y >= stage.y - 1 &&
          frame.x + frame.width <= stage.x + stage.width + 1 &&
          frame.y + frame.height <= stage.y + stage.height + 1,
        );
      })
      .toBe(true);
  }
  await page.getByRole('button', { name: 'HTML 源码', exact: true }).click();
  await expect(page.locator('.source-view')).toContainText('data-css-profile="tailwind-ppt-v1"');
  await page.getByRole('button', { name: '新版预览', exact: true }).click();
  await expect(sceneCanvas).toBeVisible();
  let rawHtmlRequests = 0;
  await page.route('**/api/v1/page-tasks/*/html', (route) => {
    rawHtmlRequests++;
    return route.abort();
  });
  let allowScene = false;
  await page.route(`**/api/v1/scenes/${sceneId}`, (route) =>
    allowScene
      ? route.continue()
      : route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ detail: { message: '场景加载暂时失败' } }),
        }),
  );
  await page.reload();
  await expect(page.getByRole('button', { name: '重试渲染', exact: true })).toBeVisible();
  allowScene = true;
  await page.getByRole('button', { name: '重试渲染', exact: true }).click();
  await expect(sceneCanvas).toBeVisible();
  expect(await page.locator('.scene-viewport').getAttribute('data-scene-id')).toBe(sceneId);
  expect(rawHtmlRequests).toBe(0);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole('button', { name: '导出 PPTX', exact: true }).click();
  const downloadLink = page.getByRole('link', { name: '下载 PPTX', exact: true });
  await expect(downloadLink).toBeVisible({ timeout: 20000 });
  const downloadUrl = await downloadLink.getAttribute('href');
  expect(rawHtmlRequests).toBe(0);
  await page.reload();
  await expect(downloadLink).toBeVisible();
  expect(await downloadLink.getAttribute('href')).toBe(downloadUrl);
  const downloaded = page.waitForEvent('download');
  await downloadLink.click();
  const download = await downloaded;
  expect(download.suggestedFilename()).toBe('Provider 验收作品.pptx');
  const bytes = await readFile((await download.path())!);
  expect(bytes.subarray(0, 2).toString()).toBe('PK');
  expect(bytes.length).toBeGreaterThan(5000);
  const repeatedDownload = await page.request.get(downloadUrl!);
  expect(await repeatedDownload.body()).toEqual(bytes);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(downloadLink).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('pptx-download-mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole('tab', { name: /大纲/ }).click();
  await expect(page.locator('.outline-title-input').first()).toHaveAttribute('readonly', '');
  await generationTab.click();
  await expect(generationTab).toHaveAttribute('aria-selected', 'true');
  await page.getByRole('button', { name: '模型设置', exact: true }).click();
  await page.getByRole('button', { name: '调用记录', exact: true }).click();
  const calls = page.locator('.calls-card tbody');
  await expect(calls.getByText('ppt_outline', { exact: true })).toBeVisible();
  await expect(calls.getByText('ppt_html', { exact: true })).toBeVisible();
  const htmlCall = calls.locator('tr').filter({ hasText: 'ppt_html' });
  await expect(htmlCall.getByText('html-model', { exact: true })).toBeVisible();
  await htmlCall.getByRole('button', { name: '详情', exact: true }).click();
  const detail = page.getByRole('dialog', { name: '模型调用详情' });
  await expect(detail.locator('pre')).toContainText('"reasoning_effort": "xhigh"');
  await expect(detail.locator('pre')).not.toContainText('credential_ref');
  await detail.getByRole('button', { name: '关闭', exact: true }).click();
  expect(errors).toEqual([]);
});

test('Entry mode routing keeps unavailable workflows visible but blocked', async ({ page }) => {
  await page.goto('/');
  const mode = page.locator('.entry-mode-trigger');
  await mode.click();
  const menu = page.getByRole('menu', { name: '选择创作模式' });
  await expect(menu.getByRole('menuitem', { name: /Agent 模式/ })).toBeVisible();
  await expect(menu.getByRole('menuitem', { name: /设计生成/ })).toHaveAttribute(
    'aria-disabled',
    'true',
  );
  await menu.getByRole('menuitem', { name: /Agent 模式/ }).click();
  await expect(page.getByPlaceholder(/步骤化/)).toBeVisible();
  await page.locator('.composer textarea').fill('用 Agent 方式梳理一个产品发布会');
  await expect(page.getByRole('button', { name: '开始创作', exact: true })).toBeEnabled();
  await mode.click();
  await expect(menu.getByRole('menuitem', { name: /图片生成/ })).toBeDisabled();
  await expect(page.getByRole('button', { name: '开始创作', exact: true })).toBeEnabled();
});

test('API key input is write-only and cleared after save', async ({ page }) => {
  await page.goto('/settings/providers');
  await page.getByRole('button', { name: '添加 Provider', exact: true }).last().click();
  await page.getByLabel('显示名称', { exact: true }).fill('Credential Provider');
  await page.getByLabel('API 根地址', { exact: true }).fill('https://example.com/v1');
  const key = 'browser-test-key';
  await page.getByLabel('API Key', { exact: true }).fill(key);
  await page.getByRole('button', { name: '保存 Provider', exact: true }).click();
  await expect(page.getByText('已配置 · 版本 1')).toBeVisible();
  await expect(page.locator('input[type=password]')).toHaveValue('');
  expect(
    await page.evaluate(() =>
      JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }),
    ),
  ).not.toContain(key);
  await page.getByRole('button', { name: '撤销全部凭据', exact: true }).click();
  await page.getByRole('button', { name: '确认撤销', exact: true }).click();
  await expect(page.getByText('尚未配置，可稍后添加', { exact: true })).toBeVisible();
});

test('Provider settings and dialogs fit a narrow screen with keyboard navigation', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/settings/providers');
  await page.getByRole('button', { name: '添加 Provider', exact: true }).last().click();
  await page.getByLabel('显示名称', { exact: true }).fill('Narrow Screen Provider');
  await page.getByLabel('API 根地址', { exact: true }).fill('https://example.com/v1');
  await page.getByLabel('认证方式', { exact: true }).selectOption('none');
  await page.getByRole('button', { name: '保存 Provider', exact: true }).click();
  await expect(
    page.getByRole('heading', { name: 'Narrow Screen Provider', exact: true }),
  ).toBeVisible();
  const assertFits = async () => {
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
    ).toBe(true);
    expect(
      await page
        .locator('.provider-settings')
        .evaluate((element) => element.scrollWidth <= element.clientWidth + 1),
    ).toBe(true);
  };
  await assertFits();
  await page.screenshot({ path: testInfo.outputPath('providers-mobile.png'), fullPage: true });
  const addModel = page.getByRole('button', { name: '手动添加', exact: true });
  await addModel.click();
  const dialog = page.getByRole('dialog', { name: '模型设置' });
  await expect(dialog.getByRole('button', { name: '关闭模型设置' })).toBeFocused();
  await dialog.getByLabel('远程 Model ID', { exact: true }).fill('mobile-model');
  await dialog.getByRole('button', { name: '保存模型信息' }).click();
  await expect(dialog.getByText('模型信息已保存。')).toBeVisible();
  const bounds = await dialog.boundingBox();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  expect(await dialog.evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(
    true,
  );
  await page.screenshot({ path: testInfo.outputPath('model-mobile.png'), fullPage: true });
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(addModel).toBeFocused();
  await page.getByRole('button', { name: '业务路由', exact: true }).click();
  await assertFits();
  await page.screenshot({ path: testInfo.outputPath('routing-mobile.png'), fullPage: true });
});

for (const concurrency of [1, 20]) {
  test(`Page spinners match model concurrency ${concurrency}`, async ({
    page,
    request,
  }, testInfo) => {
    const prefix = '/api/v1/settings/llm';
    const created = await request.post(prefix + '/providers', {
      data: {
        name: `Capacity ${concurrency}`,
        base_url: `${apiOrigin}/mock/v1`,
        auth_mode: 'none',
      },
    });
    expect(created.status()).toBe(201);
    const provider = await created.json();
    const added = await request.post(prefix + `/providers/${provider.id}/models`, {
      data: { remote_model_id: `capacity-${concurrency}`, model_concurrency: concurrency },
    });
    expect(added.status()).toBe(201);
    const model = await added.json();
    const tested = await request.post(prefix + `/models/${model.id}/test`);
    expect((await tested.json()).status).toBe('passed');
    let routes = (await (await request.get(prefix + '/business-routes')).json()).items;
    for (const code of ['default', 'ppt_outline', 'ppt_html']) {
      const route = routes.find((item: { code: string }) => item.code === code);
      const saved = await request.put(prefix + `/business-routes/${code}`, {
        data: {
          expected_revision: route.revision,
          model_id: code === 'default' ? model.id : null,
          reasoning_tier: code === 'default' ? 'off' : null,
        },
      });
      expect(saved.ok()).toBeTruthy();
    }
    await request.post('/api/v1/mock/html-capacity', { data: { hold: true } });
    try {
      await page.goto('/');
      await page.locator('.new-button').click();
      await page.locator('.composer textarea').fill('concurrency-check');
      await page.getByRole('button', { name: '生成说明书', exact: true }).click();
      await page.getByRole('button', { name: '确认并生成 20 页', exact: true }).click();
      const rail = page.locator('.page-rail');
      await expect(rail.locator('.thumb-state.running .spin')).toHaveCount(concurrency);
      await expect(rail.locator('.thumb-state.waiting_model_capacity')).toHaveCount(
        20 - concurrency,
      );
      await expect
        .poll(async () => (await (await request.get('/api/v1/mock/html-capacity')).json()).active)
        .toBe(concurrency);
      await page.reload();
      await expect(rail.locator('.thumb-state.running .spin')).toHaveCount(concurrency);
      await expect(rail.locator('.thumb-state.waiting_model_capacity')).toHaveCount(
        20 - concurrency,
      );
      await page.screenshot({
        path: testInfo.outputPath(`capacity-${concurrency}.png`),
        fullPage: true,
      });
    } finally {
      await request.post('/api/v1/mock/html-capacity', { data: { hold: false } });
    }
    await expect(page.locator('.page-rail .thumb-state.succeeded')).toHaveCount(20);
    await expect(page.locator('.page-rail .spin')).toHaveCount(0);
  });
}
