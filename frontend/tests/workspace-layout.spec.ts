import { test, expect } from '@playwright/test';

type LayoutScenario = 'home' | 'outline' | 'generation';

let scenario: LayoutScenario = 'home';
let presentationCount = 0;
let pageCount = 0;

function makeSnapshot() {
  const slides = Array.from({ length: pageCount }, (_, index) => ({
    slide_id: `slide-${index + 1}`,
    page_number: index + 1,
    page_type: 'content',
    title: `第 ${index + 1} 页`,
    key_message: '核心观点',
    content_outline: ['第一条要点', '第二条要点'],
    design_notes: '',
    summary: '页面摘要',
  }));
  const confirmed = scenario === 'generation';
  return {
    presentation: {
      id: 'layout-audit',
      user_request: '前端布局回归测试',
      status: confirmed ? 'READY' : 'WAITING_CONFIRMATION',
      entry_mode: 'ppt',
      workflow_code: 'ppt_create',
      canvas: { width: 800, height: 450 },
      conversation_id: null,
      created_at: '2026-11-16T00:00:00Z',
    },
    latest_outline: {
      id: 'outline-1',
      revision: 1,
      content_hash: 'layout-audit',
      confirmed,
      document: {
        schema_version: '1',
        brief: {
          title: '布局回归测试',
          purpose: '验证列表和容器的滚动边界',
          audience: '产品团队',
          language: 'zh-CN',
          tone: '专业',
          assumptions: [],
        },
        style_spec: {
          description: '清晰的演示文稿布局',
          colors: { primary: '#7056d8' },
          typography: {
            font_family: 'Microsoft YaHei',
            title_size_px: 32,
            body_size_px: 18,
            caption_size_px: 12,
          },
          layout_rules: ['保持留白'],
        },
        slides,
      },
    },
    pages: confirmed
      ? slides.map((slide, index) => ({
          id: `page-${index + 1}`,
          run_id: 'run-1',
          page_number: index + 1,
          title: slide.title,
          status: 'PENDING',
          html_hash: null,
          error: null,
          error_code: null,
        }))
      : [],
    active_run: null,
    active_job: null,
    latest_run: null,
    latest_job: null,
    active_streams: {},
    director_stream: null,
    last_event_seq: 0,
  };
}

test.beforeEach(async ({ page }) => {
  await page.route('https://fonts.googleapis.com/**', (route) =>
    route.fulfill({ contentType: 'text/css', body: '' }),
  );
  await page.route('https://fonts.gstatic.com/**', (route) => route.abort());
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== 'GET')
      throw new Error(`Unexpected mutating request: ${request.method()} ${path}`);

    if (path.endsWith('/events'))
      return route.fulfill({ contentType: 'text/event-stream', body: ': layout audit\n\n' });

    const data =
      path === '/api/v1/presentations'
        ? {
            items: Array.from({ length: presentationCount }, (_, index) => ({
              id: `presentation-${index + 1}`,
              user_request: `测试作品 ${index + 1}`,
              entry_mode: 'ppt',
              status: 'READY',
            })),
          }
        : path === '/api/v1/presentations/layout-audit'
          ? makeSnapshot()
          : path.endsWith('/usage')
            ? {
                presentation_id: 'layout-audit',
                call_count: 0,
                input_tokens: 0,
                output_tokens: 0,
                total_tokens: 0,
                input_unreported_calls: 0,
                output_unreported_calls: 0,
                total_unreported_calls: 0,
              }
            : path === '/api/v1/settings/llm/default'
              ? { name: 'Layout Test', model: 'mock-model', enabled: true, revision: 1 }
              : { items: [] };

    return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
  });
});

test('keeps the sidebar navigation fixed as the recent works list grows', async ({ page }) => {
  scenario = 'home';
  presentationCount = 0;
  pageCount = 0;
  await page.addInitScript(() => sessionStorage.removeItem('lumina.activePresentation'));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  await expect(page.locator('.new-button')).toHaveCSS('height', '38px');
  await expect(page.locator('.side-item').first()).toHaveCSS('height', '36px');

  presentationCount = 100;
  await page.reload();
  await expect(page.locator('.history-item')).toHaveCount(100);
  await expect(page.locator('.new-button')).toHaveCSS('height', '38px');
  await expect(page.locator('.side-item').first()).toHaveCSS('height', '36px');
  const history = await page.locator('.history-list').evaluate((element) => ({
    clientHeight: element.clientHeight,
    scrollHeight: element.scrollHeight,
    bottom: element.getBoundingClientRect().bottom,
  }));
  expect(history.scrollHeight).toBeGreaterThan(history.clientHeight);
  expect(history.bottom).toBeLessThanOrEqual(900);
});

test('keeps the top bar and welcome content usable in a short desktop viewport', async ({ page }) => {
  scenario = 'home';
  presentationCount = 100;
  pageCount = 0;
  await page.addInitScript(() => sessionStorage.removeItem('lumina.activePresentation'));
  await page.setViewportSize({ width: 1440, height: 500 });
  await page.goto('/');

  await expect(page.locator('.topbar')).toHaveCSS('height', '62px');
  await expect(page.locator('.new-button')).toHaveCSS('height', '38px');
  await expect(page.locator('.side-item').first()).toHaveCSS('height', '36px');
  const welcome = await page.locator('.welcome').evaluate((element) => ({
    clientHeight: element.clientHeight,
    scrollHeight: element.scrollHeight,
  }));
  expect(welcome.scrollHeight).toBeGreaterThan(welcome.clientHeight);
  await page.locator('.welcome').evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  expect(await page.locator('.welcome').evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
});

test('scrolls a long page rail without moving the preview out of view', async ({ page }) => {
  scenario = 'generation';
  presentationCount = 100;
  pageCount = 40;
  await page.addInitScript(() => sessionStorage.setItem('lumina.activePresentation', 'layout-audit'));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  await expect(page.locator('.page-thumb')).toHaveCount(40);
  const before = await page.locator('.workbench').evaluate((element) => element.scrollTop);
  await page.locator('.page-thumb').last().click();
  const rail = await page.locator('.page-rail-list').evaluate((element) => ({
    clientHeight: element.clientHeight,
    scrollHeight: element.scrollHeight,
    scrollTop: element.scrollTop,
  }));
  expect(rail.scrollHeight).toBeGreaterThan(rail.clientHeight);
  expect(rail.scrollTop).toBeGreaterThan(0);
  expect(await page.locator('.workbench').evaluate((element) => element.scrollTop)).toBe(before);
  const stage = await page.locator('.canvas-stage').boundingBox();
  expect(stage).not.toBeNull();
  expect(stage!.y).toBeGreaterThanOrEqual(62);
  expect(stage!.y + stage!.height).toBeLessThanOrEqual(900);
});

test('fits the outline and generation layouts between the desktop and mobile breakpoints', async ({
  page,
}) => {
  presentationCount = 0;
  pageCount = 20;
  for (const width of [901, 1000, 1099, 1100, 1199, 1239, 1240]) {
    await page.setViewportSize({ width, height: 800 });
    scenario = 'outline';
    await page.addInitScript(() => sessionStorage.setItem('lumina.activePresentation', 'layout-audit'));
    await page.goto('/');
    await expect(page.locator('.outline-card')).toHaveCount(20);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    const outlineWidth = await page.locator('.outline-layout').evaluate((element) => ({
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
    }));
    expect(outlineWidth.scrollWidth).toBeLessThanOrEqual(outlineWidth.clientWidth + 1);

    scenario = 'generation';
    await page.reload();
    await expect(page.locator('.page-thumb')).toHaveCount(20);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await expect(page.locator('.page-rail-list')).toHaveCSS(
      'flex-direction',
      width <= 1239 ? 'row' : 'column',
    );
  }

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('.sidebar')).toHaveCSS('width', '60px');
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});
