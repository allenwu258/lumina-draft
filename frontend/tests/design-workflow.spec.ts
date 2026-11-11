import { test, expect } from '@playwright/test';

test('Design portrait workflow edits, confirms, renders and restores multiple pages', async ({
  page,
  request,
}, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const prefix = '/api/v1/settings/llm';
  const provider = await (
    await request.post(prefix + '/providers', {
      data: {
        name: 'Design workflow test',
        base_url: `http://127.0.0.1:${process.env.E2E_API_PORT || '18000'}/mock/v1`,
        auth_mode: 'none',
      },
    })
  ).json();
  const models = [];
  for (const name of ['design-outline', 'design-html']) {
    const added = await request.post(prefix + `/providers/${provider.id}/models`, {
      data: { remote_model_id: name, model_concurrency: 2 },
    });
    expect(added.status()).toBe(201);
    const model = await added.json();
    expect((await (await request.post(prefix + `/models/${model.id}/test`)).json()).status).toBe(
      'passed',
    );
    models.push(model);
  }
  const routes = (await (await request.get(prefix + '/business-routes')).json()).items;
  for (const code of ['default', 'design_outline', 'design_html']) {
    const route = routes.find((item: { code: string }) => item.code === code);
    expect(
      (
        await request.put(prefix + `/business-routes/${code}`, {
          data: {
            expected_revision: route.revision,
            model_id: code === 'design_html' ? models[1].id : models[0].id,
            reasoning_tier: 'off',
          },
        })
      ).ok(),
    ).toBeTruthy();
  }
  await page.goto('/');
  await page.locator('.entry-mode-trigger').click();
  await page.getByRole('menuitem', { name: /设计生成/ }).click();
  await expect(page.getByLabel('画布比例')).toHaveValue('0');
  await page.getByLabel('画布比例').selectOption('custom');
  await page.getByLabel('宽度 (px)').fill('1920');
  await page.getByLabel('高度 (px)').fill('1920');
  await page.locator('.composer textarea').fill('为社区市集设计两页竖版海报');
  await expect(page.getByRole('button', { name: '生成设计大纲', exact: true })).toBeDisabled();
  await page.getByLabel('画布比例').selectOption('0');
  await page.getByLabel('页数', { exact: true }).fill('2');
  await page.getByRole('button', { name: '生成设计大纲', exact: true }).click();
  const confirm = page.getByRole('button', { name: '确认并生成 2 页', exact: true });
  await expect(confirm).toBeVisible();
  await expect(page.locator('.design-spec')).toContainText('1080 × 1920');
  const pid = await page.evaluate(() => sessionStorage.getItem('lumina.activePresentation'));
  const getSnapshot = async () => await (await request.get(`/api/v1/presentations/${pid}`)).json();
  expect((await getSnapshot()).pages).toEqual([]);
  await page.getByLabel('第 1 页标题').fill('市集邀约');
  await page.getByLabel('版式说明').first().fill('主标题置顶，下方留白展示日期地点');
  await page.getByRole('button', { name: '保存修改', exact: true }).click();
  await expect
    .poll(async () => (await getSnapshot()).latest_outline.document.slides[0].title)
    .toBe('市集邀约');
  await confirm.click();
  const scene = page.locator('.scene-viewport');
  await expect(scene).toHaveAttribute('data-render-status', 'ready', { timeout: 20000 });
  const checkCanvasFit = async () => {
    const dimensions = await page.locator('.scene-canvas').evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const host = element.parentElement!.getBoundingClientRect();
      return {
        width: rect.width,
        height: rect.height,
        x: rect.x,
        y: rect.y,
        host: { x: host.x, y: host.y, right: host.right, bottom: host.bottom },
      };
    });
    expect(dimensions.width / dimensions.height).toBeCloseTo(9 / 16, 3);
    expect(dimensions.x).toBeGreaterThanOrEqual(dimensions.host.x - 1);
    expect(dimensions.y).toBeGreaterThanOrEqual(dimensions.host.y - 1);
    expect(dimensions.x + dimensions.width).toBeLessThanOrEqual(dimensions.host.right + 1);
    expect(dimensions.y + dimensions.height).toBeLessThanOrEqual(dimensions.host.bottom + 1);
  };
  await checkCanvasFit();
  await expect(page.locator('.canvas-size')).toHaveText('1080 × 1920');
  await expect(page.getByRole('button', { name: /导出 PPTX/ })).toHaveCount(0);
  const first = (await getSnapshot()).pages[0];
  const captured = await (await request.get(`/api/v1/scenes/${first.scene.scene_id}`)).json();
  expect(captured.schemaVersion).toBe('0.5.0');
  expect(captured.page).toMatchObject({ width: 1080, height: 1920 });
  await page.screenshot({
    path: testInfo.outputPath('design-portrait-canvas.png'),
    fullPage: true,
  });
  await page.getByRole('button', { name: 'HTML 预览', exact: true }).click();
  const frame = page.frameLocator('iframe.slide-frame');
  await expect(frame.locator('#tailwind-proof')).toHaveText('市集邀约');
  await expect(frame.locator('#ppt-page')).toHaveCSS('width', '1080px');
  await expect(frame.locator('#ppt-page')).toHaveCSS('height', '1920px');
  await page.getByRole('button', { name: 'HTML 源码', exact: true }).click();
  await expect(page.locator('.source-view')).toContainText('height:1920px');
  await page.getByRole('button', { name: '画布预览', exact: true }).click();
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(scene).toHaveAttribute('data-render-status', 'ready');
  await expect(page.locator('.canvas-page-label strong')).toHaveText('02');
  await page.reload();
  await expect(page.getByRole('tab', { name: /设计预览/ })).toHaveAttribute(
    'aria-selected',
    'true',
  );
  await expect(scene).toHaveAttribute('data-render-status', 'ready');
  await page.getByRole('tab', { name: /设计大纲/ }).click();
  await expect(page.getByLabel('第 1 页标题')).toHaveValue('市集邀约');
  await expect(page.getByLabel('版式说明').first()).toHaveAttribute('readonly', '');
  await page.getByRole('tab', { name: /设计预览/ }).click();
  await expect(scene).toHaveAttribute('data-render-status', 'ready');
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('.sidebar')).toHaveCSS('width', '60px');
  await expect(page.getByRole('button', { name: 'HTML 源码', exact: true })).toBeInViewport();
  await checkCanvasFit();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('design-mobile.png'), fullPage: true });
  expect(errors).toEqual([]);
});
