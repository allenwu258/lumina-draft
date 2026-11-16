import { after, before, test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { chromium } from "playwright-core";
import { tailwind } from "./fixtures.mjs";

const profile = JSON.parse(readFileSync(
  new URL("../../../backend/app/tailwind-ppt-v1.json", import.meta.url), "utf8",
));
let browser;
before(async () => { browser = await chromium.launch({ headless: true }); });
after(async () => { await browser?.close(); });

test("every admitted utility has a compiled browser selector", async () => {
  const page = await browser.newPage();
  try {
    await page.setContent(tailwind(""));
    const missing = await page.evaluate((classes) => {
      const selectors = new Set();
      function scan(rules) {
        for (const rule of rules) {
          if (rule.selectorText) {
            for (const selector of rule.selectorText.split(",")) selectors.add(selector.trim());
          }
          if (rule.cssRules) scan(rule.cssRules);
        }
      }
      for (const sheet of document.styleSheets) scan(sheet.cssRules);
      return classes.filter((token) => !selectors.has("." + CSS.escape(token)));
    }, profile.classes);
    assert.deepEqual(missing, [], "The prompt must never advertise uncompiled classes");
  } finally { await page.close(); }
});

test("composer colors, typography and decorative effects render locally", async () => {
  const page = await browser.newPage();
  try {
    await page.setContent(tailwind(`
      <div id="gradient" class="bg-linear-to-br from-indigo-900 via-violet-700 to-rose-500">
        <h1 id="title" class="text-[30px] text-white leading-tight">Visual hierarchy</h1>
      </div>
      <div id="accent" class="size-16 rotate-3 bg-ppt-primary/10 shadow-lg"></div>`));
    const actual = await page.evaluate(() => {
      const title = getComputedStyle(document.querySelector("#title"));
      const accent = getComputedStyle(document.querySelector("#accent"));
      return {
        fontSize: title.fontSize, lineHeight: title.lineHeight, color: title.color,
        gradient: getComputedStyle(document.querySelector("#gradient")).backgroundImage,
        width: accent.width, height: accent.height, rotate: accent.rotate,
        shadow: accent.boxShadow, colorAlpha: accent.backgroundColor, opacity: accent.opacity,
      };
    });
    assert.equal(actual.fontSize, "30px");
    assert.equal(actual.lineHeight, "37.5px");
    assert.equal(actual.color, "rgb(255, 255, 255)");
    assert.match(actual.gradient, /^linear-gradient/);
    assert.equal(actual.width, "64px");
    assert.equal(actual.height, "64px");
    assert.equal(actual.rotate, "3deg");
    assert.notEqual(actual.shadow, "none");
    assert.match(actual.colorAlpha, /(?:0\.1|10%)/);
    assert.equal(actual.opacity, "1", "Background alpha must not fade the text");
  } finally { await page.close(); }
});

test("utilities found in failed model outputs have the intended computed styles", async () => {
  const page = await browser.newPage();
  try {
    await page.setContent(tailwind(`
      <section id="column" class="flex flex-col h-40"><div id="footer" class="mt-auto border-l-2 border-ppt-primary text-[17px]">Footer</div></section>
      <div id="baseline" class="flex items-baseline"><span class="text-lg">Baseline</span></div>
      <div id="small" class="size-7"></div><div id="narrow" class="w-5 h-1"></div>
      <div id="offset" class="absolute left-72 top-32 w-1 h-1"></div>`));
    const actual = await page.evaluate(() => {
      const style = (id) => getComputedStyle(document.getElementById(id));
      const bounds = (id) => document.getElementById(id).getBoundingClientRect();
      return {
        footerBottom: bounds("footer").bottom, columnBottom: bounds("column").bottom,
        border: style("footer").borderLeftWidth, fontSize: style("footer").fontSize,
        alignment: style("baseline").alignItems, size: style("small").width,
        width: style("narrow").width, left: style("offset").left, top: style("offset").top,
      };
    });
    assert.equal(actual.footerBottom, actual.columnBottom);
    assert.equal(actual.border, "2px");
    assert.equal(actual.fontSize, "17px");
    assert.equal(actual.alignment, "baseline");
    assert.equal(actual.size, "28px");
    assert.equal(actual.width, "20px");
    assert.equal(actual.left, "288px");
    assert.equal(actual.top, "128px");
  } finally { await page.close(); }
});

test("representative composer layouts keep readable content inside the canvas", async () => {
  const layouts = [
    `<header class="shrink-0 mb-6"><h1 class="text-[30px] leading-tight">中文长标题与 English：空间预算和可读性</h1></header>
     <section class="flex-1 min-h-0 grid grid-cols-12 gap-6">
       <div class="col-span-7 min-w-0 bg-indigo-900 text-white p-6 rounded-2xl"><p class="text-5xl font-bold mb-4">72%</p><p class="text-ppt-body leading-relaxed">重点数字、事实与结论形成清楚的视觉层级。</p></div>
       <div class="col-span-5 min-w-0 flex flex-col gap-4"><p>分栏使用网格跨列，间距计入可用宽度。</p><p>正文自然换行，保留完整的关键内容。</p></div>
     </section>`,
    `<section class="flex-1 min-h-0 flex gap-8 items-center"><div class="flex-1 min-w-0"><p class="text-sm text-ppt-muted mb-4">STRATEGY / 2026</p><h1 class="text-[40px] leading-tight font-bold">以清晰的结构<br>表达复杂的观点</h1><p class="mt-6 text-ppt-body leading-relaxed">开放构图、协调配色和充分留白，让内容保持主角。</p></div><div class="w-56 shrink-0"><div class="size-48 rounded-full bg-linear-to-br from-cyan-300 to-blue-800"></div><p class="text-sm mt-4">从信息关系出发组织视觉焦点</p></div></section>`,
    `<h1 class="shrink-0 text-ppt-title mb-6">从内容到表达的三个步骤</h1><section class="flex-1 min-h-0 grid grid-cols-3 gap-6">${["理解信息关系", "安排视觉层级", "检查空间边界"].map((title, i) => `<article class="min-w-0 border-t border-ppt-primary pt-4"><p class="text-4xl text-ppt-primary mb-4">0${i+1}</p><h2 class="text-[20px] font-semibold mb-3">${title}</h2><p class="text-ppt-body leading-relaxed break-words">保留关键事实与必要说明，让长文本在可用列宽内自然换行。</p></article>`).join("")}</section>`,
  ];
  const page = await browser.newPage({ viewport: { width: 800, height: 450 } });
  try {
    for (const body of layouts) {
      await page.setContent(tailwind(body).replace(
        'class="bg-ppt-background p-8 font-ppt text-ppt-text"',
        'class="relative flex flex-col bg-ppt-background p-8 font-ppt text-ppt-body text-ppt-text"',
      ));
      await page.evaluate(() => document.fonts.ready);
      const overflow = await page.evaluate(() => {
        const root = document.querySelector("#ppt-page");
        const bounds = root.getBoundingClientRect();
        const failures = [];
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
          const node = walker.currentNode;
          if (!node.textContent.trim()) continue;
          const range = document.createRange();
          range.selectNodeContents(node);
          if ([...range.getClientRects()].some(r => r.left < bounds.left - 1 || r.right > bounds.right + 1 || r.top < bounds.top - 1 || r.bottom > bounds.bottom + 1)) failures.push(node.textContent);
        }
        if (root.scrollWidth > root.clientWidth || root.scrollHeight > root.clientHeight) failures.push("canvas overflow");
        return failures;
      });
      assert.deepEqual(overflow, []);
    }
  } finally { await page.close(); }
});
