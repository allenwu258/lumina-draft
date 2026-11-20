import { expect, test } from '@playwright/test';

test('loads a saved SceneIR, autosaves its name and exports the saved revision', async ({ page }) => {
  const savedNames: string[] = [];
  const exportRevisions: number[] = [];
  let revision = 1;
  let savedName = '纵横家思想';
  const work = () => ({
    id: 'work-preview',
    name: savedName,
    type: 'presentation',
    revision,
    page_count: 1,
    pages: [{ id: 'work-page-1', position: 1, title: '思想体系' }],
    latest_export: null,
  });
  const scene = {
    schemaVersion: '0.4.0',
    source: {
      htmlHash: 'a'.repeat(64),
      cssHash: 'b'.repeat(64),
      converterVersion: 'dom-canvas-0.4.4',
    },
    environment: {
      chromium: 'test',
      platform: 'test',
      locale: 'zh-CN',
      timezone: 'Asia/Shanghai',
      dpr: 1,
      fontSignature: 'c'.repeat(64),
    },
    page: { width: 800, height: 450, background: '#ffffff' },
    nodes: [],
    paintOps: [],
    assets: [],
    textInventory: [],
    diagnostics: [],
  };
  const completedExport = {
    id: 'export-work-1',
    status: 'SUCCEEDED',
    work_revision: 2,
    error: null,
    download_url: '/api/v1/exports/export-work-1/file',
    filename: '纵横家思想.pptx',
  };

  await page.clock.install();
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET' && path === '/api/v1/works')
      return route.fulfill({ json: [work()] });
    if (request.method() === 'GET' && path === '/api/v1/works/work-preview')
      return route.fulfill({ json: work() });
    if (request.method() === 'GET' && path === '/api/v1/works/work-preview/pages/work-page-1')
      return route.fulfill({ json: { ...work().pages[0], scene_hash: 'd'.repeat(64), scene } });
    if (request.method() === 'PATCH' && path === '/api/v1/works/work-preview') {
      const body = request.postDataJSON() as { name: string; base_revision: number };
      expect(body.base_revision).toBe(revision);
      revision += 1;
      savedName = body.name;
      savedNames.push(body.name);
      return route.fulfill({ json: { ...work(), name: body.name } });
    }
    if (request.method() === 'POST' && path === '/api/v1/works/work-preview/exports') {
      const body = request.postDataJSON() as { expected_revision: number };
      exportRevisions.push(body.expected_revision);
      return route.fulfill({ status: 202, json: completedExport });
    }
    throw new Error(`Unexpected request: ${request.method()} ${path}`);
  });

  await page.goto('/edit/work-preview');
  await expect(page.getByLabel('作品名称')).toHaveValue('纵横家思想');
  await expect(page.getByRole('button', { name: /思想体系/ })).toBeVisible();
  await expect(page.locator('.work-canvas-host canvas')).toBeVisible();
  const rendered = await page.locator('.work-canvas-host canvas').evaluate((canvas) => {
    const context = (canvas as HTMLCanvasElement).getContext('2d');
    return context?.getImageData(1, 1, 1, 1).data[3];
  });
  expect(rendered).toBe(255);

  await page.getByLabel('作品名称').fill('纵横家思想修订');
  await page.clock.fastForward(60_000);
  await expect(page.getByText('已保存 · v2')).toBeVisible();
  expect(savedNames).toEqual(['纵横家思想修订']);

  await page.getByRole('button', { name: '导出 PPTX' }).click();
  await expect(page.getByRole('link', { name: '下载 PPTX' })).toBeVisible();
  expect(exportRevisions).toEqual([2]);

  await page.reload();
  await expect(page.getByLabel('作品名称')).toHaveValue('纵横家思想修订');
  await expect(page.getByText('已保存 · v2')).toBeVisible();

  await page.goto('/works');
  await page.getByRole('button', { name: /纵横家思想修订/ }).click();
  await expect(page).toHaveURL(/\/edit\/work-preview$/);
  await expect(page.locator('.work-canvas-host canvas')).toBeVisible();
});
