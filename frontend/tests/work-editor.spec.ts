import { expect, test } from '@playwright/test';
import { createHash } from 'node:crypto';

function editableScene(sourceText = '纵横家思想') {
  const style = {
    fontFamily: 'Arial', fontSize: 24, fontWeight: '400', fontStyle: 'normal',
    color: '#1f2933', letterSpacing: 0, decoration: 'none',
  };
  const width = 800;
  const height = 450;
  const textWidth = Math.max(90, sourceText.length * 24);
  return {
    schemaVersion: '0.4.0',
    source: { htmlHash: 'a'.repeat(64), cssHash: 'b'.repeat(64), converterVersion: 'dom-canvas-0.4.4' },
    environment: { chromium: 'test', platform: 'test', locale: 'zh-CN', timezone: 'Asia/Shanghai', dpr: 1, fontSignature: 'c'.repeat(64) },
    page: { width, height, background: '#ffffff' },
    nodes: [
      { id: 'root', sourcePath: 'main', kind: 'group', tagName: 'div', frame: { x: 0, y: 0, width, height }, opacity: 1, clip: null },
      { id: 'box', sourcePath: 'main/box', kind: 'group', tagName: 'div', frame: { x: 120, y: 100, width: 360, height: 120 }, opacity: 1, clip: null },
      { id: 'background', sourcePath: 'main/box', kind: 'shape', frame: { x: 120, y: 100, width: 360, height: 120 }, fill: '#eef4f1', radii: [0, 0, 0, 0], borders: [{ width: 0, color: '#000000', style: 'none' }, { width: 0, color: '#000000', style: 'none' }, { width: 0, color: '#000000', style: 'none' }, { width: 0, color: '#000000', style: 'none' }], shadows: [] },
      { id: 'text', sourcePath: 'main/box/text', kind: 'text', frame: { x: 136, y: 108, width: textWidth, height: 30 }, text: { ownerId: 'box', sourceText, paragraphs: [{ runs: [{ id: 'text_run', sourceStart: 0, sourceEnd: sourceText.length, style }] }], lines: [{ frame: { x: 136, y: 108, width: textWidth, height: 30 }, baseline: 132, fragments: [{ runId: 'text_run', sourceStart: 0, sourceEnd: sourceText.length, displayText: sourceText, frame: { x: 136, y: 108, width: textWidth, height: 30 }, baseline: 132, measuredWidth: textWidth }] }] } },
    ],
    paintOps: [{ op: 'begin', nodeId: 'root' }, { op: 'begin', nodeId: 'box' }, { op: 'draw', nodeId: 'background' }, { op: 'draw', nodeId: 'text' }, { op: 'end', nodeId: 'box' }, { op: 'end', nodeId: 'root' }],
    assets: [],
    textInventory: [{ sourcePath: 'main/box/text', sourceText, nodeId: 'text', reason: null, target: { kind: 'text', nodeId: 'text' } }],
    diagnostics: [],
  };
}

function imageScene() {
  const scene = editableScene('资源页面');
  const bytes = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');
  const assetId = 'asset-pixel';
  scene.nodes.push({ id: 'image', sourcePath: 'main/image', kind: 'image', frame: { x: 520, y: 80, width: 180, height: 180 }, assetId });
  scene.paintOps.splice(scene.paintOps.length - 1, 0, { op: 'draw', nodeId: 'image' });
  scene.assets.push({ id: assetId, mediaType: 'image/png', sha256: createHash('sha256').update(bytes).digest('hex'), byteLength: bytes.length, width: 1, height: 1, file: 'pixel.png', coveredNodeIds: ['image'], bakedEffects: [] });
  return { scene, bytes };
}

function whitespaceScene() {
  const sourceText = '\n        两种结盟策略\n      ';
  const visibleText = '两种结盟策略';
  const scene = editableScene(sourceText);
  const box = scene.nodes.find((node) => node.id === 'box');
  const text = scene.nodes.find((node): node is Extract<typeof scene.nodes[number], { kind: 'text' }> => node.id === 'text' && node.kind === 'text');
  if (!box || box.kind !== 'group' || !text) throw new Error('Whitespace fixture is incomplete');
  box.frame = { x: 120, y: 100, width: 360, height: 24 };
  text.frame = { x: 136, y: 104, width: visibleText.length * 16, height: 16 };
  text.text.sourceText = sourceText;
  text.text.paragraphs[0].runs[0].style = { ...text.text.paragraphs[0].runs[0].style, fontSize: 16 };
  text.text.lines = [{
    frame: text.frame,
    baseline: 118,
    fragments: [{
      runId: 'text_run', sourceStart: 9, sourceEnd: 9 + visibleText.length,
      displayText: visibleText, frame: text.frame, baseline: 118, measuredWidth: text.frame.width,
    }],
  }];
  scene.textInventory[0].sourceText = sourceText;
  return { scene, visibleText };
}

function siblingConstrainedTextScene() {
  const bodyText = '服务既有权力目标时，未必能保护弱势者';
  const scene = editableScene(bodyText);
  const owner = scene.nodes.find((node) => node.id === 'box');
  const body = scene.nodes.find((node) => node.id === 'text');
  if (!owner || owner.kind !== 'group' || !body || body.kind !== 'text')
    throw new Error('Sibling-constrained text fixture is incomplete');

  owner.frame = { x: 425, y: 160, width: 320, height: 64 };
  const style = { ...body.text.paragraphs[0].runs[0].style, fontSize: 20 };
  const firstLineText = bodyText.slice(0, 12);
  const secondLineText = bodyText.slice(12);
  const firstLine = {
    x: 505, y: 184, width: 240, height: 20,
  };
  const secondLine = {
    x: 425, y: 206, width: 48, height: 20,
  };
  body.frame = { x: 425, y: 184, width: 320, height: 42 };
  body.text = {
    ownerId: 'box',
    sourceText: bodyText,
    paragraphs: [{ runs: [{ id: 'text_run', sourceStart: 0, sourceEnd: bodyText.length, style }] }],
    lines: [
      {
        frame: firstLine,
        baseline: 200,
        fragments: [{ runId: 'text_run', sourceStart: 0, sourceEnd: firstLineText.length, displayText: firstLineText, frame: firstLine, baseline: 200, measuredWidth: 240 }],
      },
      {
        frame: secondLine,
        baseline: 222,
        fragments: [{ runId: 'text_run', sourceStart: firstLineText.length, sourceEnd: bodyText.length, displayText: secondLineText, frame: secondLine, baseline: 222, measuredWidth: 120 }],
      },
    ],
  };
  body.text.ownerId = 'box';

  const labelStyle = { ...style, color: '#b28b42' };
  const labelText = '权力导向';
  const dividerText = '｜';
  const makeSibling = (id: string, sourcePath: string, text: string, frame: { x: number; y: number; width: number; height: number }) => ({
    id,
    sourcePath,
    kind: 'text' as const,
    frame,
    text: {
      ownerId: 'box',
      sourceText: text,
      paragraphs: [{ runs: [{ id: `${id}_run`, sourceStart: 0, sourceEnd: text.length, style: labelStyle }] }],
      lines: [{ frame, baseline: frame.y + 20, fragments: [{ runId: `${id}_run`, sourceStart: 0, sourceEnd: text.length, displayText: text, frame, baseline: frame.y + 20, measuredWidth: text === labelText ? 80 : 20 }] }],
    },
  });
  scene.nodes.push(
    makeSibling('label', 'main/box/label', labelText, { x: 425, y: 164, width: 64, height: 20 }),
    makeSibling('divider', 'main/box/divider', dividerText, { x: 489, y: 164, width: 16, height: 20 }),
  );
  const bodyDraw = scene.paintOps.findIndex((op) => op.op === 'draw' && op.nodeId === 'text');
  scene.paintOps.splice(bodyDraw, 0, { op: 'draw', nodeId: 'label' }, { op: 'draw', nodeId: 'divider' });
  scene.textInventory = [
    { sourcePath: 'main/box/label', sourceText: labelText, nodeId: 'label', reason: null, target: { kind: 'text', nodeId: 'label' } },
    { sourcePath: 'main/box/divider', sourceText: dividerText, nodeId: 'divider', reason: null, target: { kind: 'text', nodeId: 'divider' } },
    { sourcePath: 'main/box/text', sourceText: bodyText, nodeId: 'text', reason: null, target: { kind: 'text', nodeId: 'text' } },
  ];
  return { scene, bodyText };
}

test('preserves padded text anchors while editing, aligning, dragging and reopening', async ({ page }) => {
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
  let savedScene = editableScene();
  let savedEditor = {
    version: 1,
    text: {} as Record<string, {
      align: 'left' | 'center' | 'right';
      box: { x: number; y: number; width: number; height: number };
      baseline: number;
    }>,
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
      return route.fulfill({ json: { ...work().pages[0], scene_hash: 'd'.repeat(64), scene: savedScene, editor: savedEditor } });
    if (request.method() === 'PUT' && path === '/api/v1/works/work-preview/pages/work-page-1') {
      const body = request.postDataJSON() as { base_revision: number; scene: typeof savedScene; editor: typeof savedEditor };
      expect(body.base_revision).toBe(revision);
      savedScene = body.scene;
      savedEditor = body.editor;
      revision += 1;
      return route.fulfill({ json: { work_revision: revision, page_id: 'work-page-1', scene_hash: 'e'.repeat(64), editor: savedEditor, saved_at: new Date().toISOString() } });
    }
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
      return route.fulfill({ status: 202, json: { ...completedExport, work_revision: body.expected_revision } });
    }
    if (request.method() === 'GET' && path === '/api/v1/exports/export-work-1/file')
      return route.fulfill({ body: 'pptx', contentType: 'application/vnd.openxmlformats-officedocument.presentationml.presentation' });
    throw new Error(`Unexpected request: ${request.method()} ${path}`);
  });

  await page.goto('/edit/work-preview');
  await expect(page.getByLabel('作品名称')).toHaveValue('纵横家思想');
  await expect(page.getByRole('button', { name: /思想体系/ })).toBeVisible();
  await expect(page.locator('.work-canvas-host canvas')).toBeVisible();
  const host = page.locator('.work-canvas-host');
  const bounds = await host.boundingBox();
  expect(bounds).not.toBeNull();
  const clickText = () => page.mouse.dblclick(bounds!.x + bounds!.width * 0.2, bounds!.y + bounds!.height * 0.25);
  const canvas = host.locator('canvas');
  const selection = page.getByTestId('scene-selection');
  const editor = page.getByLabel('文字内容');
  const at = (x: number, y: number) => ({
    x: bounds!.x + x * bounds!.width / 800,
    y: bounds!.y + y * bounds!.height / 450,
  });
  const textPoint = at(160, 112.5);
  const backgroundPoint = at(450, 190);
  const pixels = async () => {
    const image = await canvas.evaluate(element => (element as HTMLCanvasElement).toDataURL());
    return createHash('sha256').update(image).digest('hex');
  };
  await expect.poll(() => canvas.evaluate(element =>
    (element as HTMLCanvasElement).getContext('2d')!.getImageData(1, 1, 1, 1).data[3],
  )).toBe(255);
  await page.mouse.move(bounds!.x + bounds!.width * 0.2, bounds!.y + bounds!.height * 0.25);
  await expect(page.getByTestId('scene-hover')).toHaveCSS('border-top-style', 'dashed');
  await expect(host).toHaveCSS('cursor', 'grab');
  await page.mouse.move(bounds!.x + bounds!.width * 0.95, bounds!.y + bounds!.height * 0.95);
  await expect(page.getByTestId('scene-hover')).toHaveCount(0);
  await page.mouse.click(bounds!.x + bounds!.width * 0.2, bounds!.y + bounds!.height * 0.25);
  await expect(page.getByTestId('scene-selection')).toBeVisible();
  await expect(page.getByLabel('文字内容')).toBeHidden();
  const initial = await pixels();
  const originalFrame = (await selection.boundingBox())!;
  expect((originalFrame.x - bounds!.x) / (bounds!.width / 800)).toBeCloseTo(120, 0);
  await page.mouse.click(backgroundPoint.x, backgroundPoint.y);
  expect(await selection.boundingBox()).toEqual(originalFrame);
  await page.mouse.click(textPoint.x, textPoint.y);
  await expect(editor).toBeFocused();
  const caret = await editor.evaluate((input: HTMLTextAreaElement) => input.selectionStart);
  expect(caret).toBeGreaterThan(0);
  expect(caret).toBeLessThan('纵横家思想'.length);
  await page.keyboard.press('Escape');
  await expect(host).toBeFocused();
  await expect(selection).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(selection).toHaveCount(0);

  await page.mouse.move(backgroundPoint.x, backgroundPoint.y);
  await page.mouse.down();
  await expect(host).toHaveAttribute('data-gesture', 'pressed');
  await page.mouse.move(backgroundPoint.x + 3, backgroundPoint.y);
  await page.mouse.up();
  expect(await selection.boundingBox()).toEqual(originalFrame);
  await expect(page.getByRole('button', { name: '保存' })).toBeDisabled();

  await page.mouse.move(backgroundPoint.x, backgroundPoint.y);
  await page.mouse.down();
  await page.mouse.move(backgroundPoint.x + 20, backgroundPoint.y + 10);
  await expect(host).toHaveAttribute('data-gesture', 'dragging');
  await expect(host).toHaveCSS('cursor', 'grabbing');
  expect((await selection.boundingBox())!.x).toBeCloseTo(originalFrame.x + 20, 0);
  await expect(page.getByRole('button', { name: '保存' })).toBeDisabled();
  await page.keyboard.press('Escape');
  await page.mouse.up();
  expect(await selection.boundingBox()).toEqual(originalFrame);
  await expect.poll(pixels).toBe(initial);

  await page.keyboard.press('Shift+ArrowRight');
  expect((await selection.boundingBox())!.x).toBeCloseTo(originalFrame.x + 10 * bounds!.width / 800, 0);
  await page.keyboard.press('Shift+ArrowLeft');
  await page.keyboard.press('ArrowDown');
  expect((await selection.boundingBox())!.y).toBeCloseTo(originalFrame.y + bounds!.height / 450, 0);
  await page.keyboard.press('ArrowUp');
  await expect(page.getByRole('button', { name: '保存' })).toBeDisabled();
  await page.mouse.click(bounds!.x + bounds!.width * 0.95, bounds!.y + bounds!.height * 0.95);
  await expect(page.getByTestId('scene-selection')).toHaveCount(0);
  await page.mouse.click(bounds!.x + bounds!.width * 0.2, bounds!.y + bounds!.height * 0.25);
  await expect(page.getByTestId('scene-selection')).toBeVisible();
  await host.press('Enter');
  await expect(page.getByLabel('文字内容')).toBeVisible();
  await page.getByLabel('文字内容').press('Escape');
  await expect(page.getByLabel('文字内容')).toBeHidden();
  await clickText();
  await expect(editor).toHaveValue('纵横家思想');
  const overlay = (await editor.boundingBox())!;
  expect((overlay.x - bounds!.x) / (bounds!.width / 800)).toBeCloseTo(136, 0);
  await editor.press('Escape');
  await expect(editor).toBeHidden();
  await expect.poll(pixels).toBe(initial);
  await expect(page.getByRole('button', { name: '保存' })).toBeDisabled();
  await clickText();
  await expect(editor).toBeVisible();
  await page.mouse.click(bounds!.x + bounds!.width * 0.95, bounds!.y + bounds!.height * 0.95);
  await expect(editor).toBeHidden();
  await expect(page.getByTestId('scene-selection')).toHaveCount(0);
  await clickText();
  const editedText = '纵横家思想体系及其在战国时期所面对的政治选择与历史影响';
  await editor.fill(editedText);
  await page.getByLabel('字号').fill('30');
  await page.getByRole('button', { name: '加粗' }).click();
  await page.getByRole('button', { name: '居中对齐' }).click();
  await page.getByRole('button', { name: '保存' }).click();
  await expect(page.getByText('已保存 · v2')).toBeVisible();
  expect(savedScene.textInventory[0].sourceText).toBe(editedText);
  expect(savedEditor.text.text.align).toBe('center');
  expect(savedScene.nodes.find((node) => node.id === 'text')?.text.paragraphs[0].runs[0].style.fontWeight).toBe('700');
  expect(savedScene.nodes.find((node) => node.id === 'text')?.text.paragraphs[0].runs[0].style.fontSize).toBe(25);
  const lines = savedScene.nodes.find(node => node.id === 'text')!.text!.lines;
  expect(lines.length).toBeGreaterThan(1);
  expect(lines.flatMap(line => line.fragments.map(fragment => fragment.displayText)).join('')).toBe(editedText);
  expect(lines[0].baseline).toBe(132);
  for (const line of lines) {
    expect(line.fragments[0].frame.x).toBe(line.frame.x);
    expect(line.frame.x + line.frame.width / 2).toBeCloseTo(308, 0);
  }

  await editor.press('Escape');
  await expect(editor).toBeHidden();
  for (const [button, alignment] of [['右对齐', 'right'], ['左对齐', 'left']] as const) {
    await page.getByRole('button', { name: button }).click();
    await page.getByRole('button', { name: '保存' }).click();
    await expect(page.getByText(`已保存 · v${alignment === 'right' ? 3 : 4}`)).toBeVisible();
    const aligned = savedScene.nodes.find(node => node.id === 'text')!.text!.lines;
    expect(aligned[0].fragments[0].frame.x).toBe(aligned[0].frame.x);
    expect(alignment === 'right' ? aligned[0].frame.x + aligned[0].frame.width : aligned[0].frame.x)
      .toBeCloseTo(alignment === 'right' ? 480 : 136, 0);
  }
  const beforeDrag = savedEditor.text.text;
  const movedBounds = await host.boundingBox();
  await page.mouse.move(movedBounds!.x + movedBounds!.width * 0.2, movedBounds!.y + movedBounds!.height * 0.27);
  await page.mouse.down();
  await page.mouse.move(movedBounds!.x + movedBounds!.width * 0.25, movedBounds!.y + movedBounds!.height * 0.32);
  await page.mouse.up();
  await page.getByRole('button', { name: '保存' }).click();
  await expect(page.getByText('已保存 · v5')).toBeVisible();
  expect(savedEditor.text.text.box.x - beforeDrag.box.x).toBeCloseTo(40, 0);
  expect(savedEditor.text.text.baseline - beforeDrag.baseline).toBeCloseTo(22.5, 0);
  const rendered = await page.locator('.work-canvas-host canvas').evaluate((canvas) => {
    const context = (canvas as HTMLCanvasElement).getContext('2d');
    return context?.getImageData(1, 1, 1, 1).data[3];
  });
  expect(rendered).toBe(255);

  await page.getByLabel('作品名称').fill('纵横家思想修订');
  await page.clock.fastForward(60_000);
  await expect(page.getByText('已保存 · v6')).toBeVisible();
  expect(savedNames).toEqual(['纵横家思想修订']);

  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出并下载 PPTX' }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('纵横家思想.pptx');
  await expect(page.getByRole('link', { name: '下载 PPTX' })).toHaveCount(0);
  expect(exportRevisions).toEqual([6]);
  const secondDownloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出并下载 PPTX' }).click();
  expect((await secondDownloadPromise).suggestedFilename()).toBe('纵横家思想.pptx');
  expect(exportRevisions).toEqual([6, 6]);

  await page.mouse.click(movedBounds!.x + movedBounds!.width * 0.75, movedBounds!.y + movedBounds!.height * 0.75);
  const savedPixels = await pixels();
  await page.reload();
  await expect(page.getByLabel('作品名称')).toHaveValue('纵横家思想修订');
  await expect(page.getByText('已保存 · v6')).toBeVisible();
  await expect.poll(pixels).toBe(savedPixels);

  await page.goto('/works');
  await page.getByRole('button', { name: /纵横家思想修订/ }).click();
  await expect(page).toHaveURL(/\/edit\/work-preview$/);
  await expect(page.locator('.work-canvas-host canvas')).toBeVisible();
});

test('keeps the editor mounted while loading an asset page', async ({ page }) => {
  const asset = imageScene();
  const pageOne = editableScene();
  const work = {
    id: 'asset-work',
    name: '资源页面测试',
    type: 'presentation',
    revision: 1,
    page_count: 2,
    pages: [
      { id: 'asset-page-1', position: 1, title: '普通页面' },
      { id: 'asset-page-2', position: 2, title: '图片页面' },
    ],
    latest_export: null,
  };
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET' && path === '/api/v1/works/asset-work')
      return route.fulfill({ json: work });
    if (request.method() === 'GET' && path === '/api/v1/works/asset-work/pages/asset-page-1')
      return route.fulfill({ json: { ...work.pages[0], scene_hash: 'a'.repeat(64), scene: pageOne, editor: { version: 1, text: {} } } });
    if (request.method() === 'GET' && path === '/api/v1/works/asset-work/pages/asset-page-2')
      return route.fulfill({ json: { ...work.pages[1], scene_hash: 'b'.repeat(64), scene: asset.scene, editor: { version: 1, text: {} } } });
    if (request.method() === 'GET' && path === '/api/v1/works/asset-work/pages/asset-page-2/assets/asset-pixel')
      return route.fulfill({ body: asset.bytes, contentType: 'image/png' });
    throw new Error(`Unexpected request: ${request.method()} ${path}`);
  });

  await page.goto('/edit/asset-work');
  await expect(page.locator('.work-canvas-host canvas')).toBeVisible();
  await page.locator('.work-pages button').nth(1).click();
  await expect(page.getByText('第 2 页 · 图片页面')).toBeVisible();
  await expect(page.locator('.work-canvas-host canvas')).toBeVisible();
  expect(pageErrors).toEqual([]);
});

test('opens imported text with template whitespace as visible editable text', async ({ page }) => {
  const fixture = whitespaceScene();
  const work = {
    id: 'whitespace-work',
    name: '模板空白测试',
    type: 'presentation',
    revision: 1,
    page_count: 1,
    pages: [{ id: 'whitespace-page-1', position: 1, title: '模板空白' }],
    latest_export: null,
  };
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET' && path === '/api/v1/works/whitespace-work')
      return route.fulfill({ json: work });
    if (request.method() === 'GET' && path === '/api/v1/works/whitespace-work/pages/whitespace-page-1')
      return route.fulfill({ json: { ...work.pages[0], scene_hash: 'w'.repeat(64), scene: fixture.scene, editor: { version: 1, text: {} } } });
    throw new Error(`Unexpected request: ${request.method()} ${path}`);
  });

  await page.goto('/edit/whitespace-work');
  await expect(page.locator('.work-canvas-host canvas')).toBeVisible();
  const host = page.locator('.work-canvas-host');
  const bounds = await host.boundingBox();
  expect(bounds).not.toBeNull();
  const point = {
    x: bounds!.x + 160 * bounds!.width / 800,
    y: bounds!.y + 112 * bounds!.height / 450,
  };
  await page.mouse.click(point.x, point.y);
  await page.waitForTimeout(50);
  await page.mouse.click(point.x, point.y);

  const editor = page.getByLabel('文字内容');
  await expect(editor).toBeFocused();
  await expect(editor).toHaveValue(fixture.visibleText);
  const metrics = await editor.evaluate((input) => ({
    clientHeight: input.clientHeight,
    scrollHeight: input.scrollHeight,
  }));
  expect(metrics.scrollHeight).toBeLessThanOrEqual(metrics.clientHeight + 1);
});

test('preserves a sibling-constrained first-line indent while editing', async ({ page }) => {
  const fixture = siblingConstrainedTextScene();
  const work = {
    id: 'sibling-work',
    name: '同行文本约束测试',
    type: 'presentation',
    revision: 1,
    page_count: 1,
    pages: [{ id: 'sibling-page-1', position: 1, title: '同行文本约束' }],
    latest_export: null,
  };
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET' && path === '/api/v1/works/sibling-work')
      return route.fulfill({ json: work });
    if (request.method() === 'GET' && path === '/api/v1/works/sibling-work/pages/sibling-page-1')
      return route.fulfill({ json: { ...work.pages[0], scene_hash: 's'.repeat(64), scene: fixture.scene, editor: { version: 1, text: {} } } });
    throw new Error(`Unexpected request: ${request.method()} ${path}`);
  });

  await page.goto('/edit/sibling-work');
  const host = page.locator('.work-canvas-host');
  await expect(host.locator('canvas')).toBeVisible();
  await expect.poll(() => host.locator('canvas').evaluate((element) =>
    (element as HTMLCanvasElement).getContext('2d')!.getImageData(130, 110, 1, 1).data[3],
  )).toBe(255);
  const bounds = await host.boundingBox();
  expect(bounds).not.toBeNull();
  const point = {
    x: bounds!.x + 550 * bounds!.width / 800,
    y: bounds!.y + 194 * bounds!.height / 450,
  };
  await page.mouse.click(point.x, point.y);
  await page.waitForTimeout(50);
  await page.mouse.click(point.x, point.y);

  const editor = page.getByLabel('文字内容');
  await expect(editor).toBeFocused();
  await expect(editor).toHaveValue(fixture.bodyText);
  await expect(editor).toHaveCSS('text-indent', '80px');
  const metrics = await editor.evaluate((input: HTMLTextAreaElement) => ({
    clientHeight: input.clientHeight,
    scrollHeight: input.scrollHeight,
    lineHeight: Number.parseFloat(getComputedStyle(input).lineHeight),
  }));
  expect(metrics.scrollHeight).toBeGreaterThan(metrics.lineHeight * 1.5);
  expect(metrics.scrollHeight).toBeLessThanOrEqual(metrics.clientHeight + 1);
});
