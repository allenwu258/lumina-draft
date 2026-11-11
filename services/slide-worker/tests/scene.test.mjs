import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { chromium } from "playwright-core";
import { build } from "esbuild";
import { PNG } from "pngjs";
import { assertScene, DESIGN_CONVERTER_VERSION } from "@lumina/scene-core";
import { convert, canonical, hash } from "../dist/capture.js";
import { css, fixture, tailwind } from "./fixtures.mjs";

let browser, temp, scene, bundle;
before(async () => {
  browser = await chromium.launch({ headless: true });
  temp = await mkdtemp(join(tmpdir(), "lumina-scene-"));
  bundle = (
    await build({
      stdin: {
        contents: `import {SceneRenderer,decodeAsset} from '@lumina/scene-renderer';window.SceneRenderer=SceneRenderer;window.decodeAsset=decodeAsset;`,
        resolveDir: resolve("."),
      },
      bundle: true,
      write: false,
      format: "iife",
      platform: "browser",
    })
  ).outputFiles[0].text;
});
after(async () => {
  await browser?.close();
  await rm(temp, { recursive: true, force: true });
});
const input = (html) => ({ html, htmlHash: hash(html), cssHash: hash(css) });
for (const [width, height] of [[1080, 1920], [900, 1200], [1080, 1080], [1280, 720]]) {
  test(`Design ${width}x${height} captures the intrinsic canvas and renders at reduced size`, async () => {
    const html = tailwind('<div class="h-full flex flex-col justify-between"><h1 class="text-ppt-title">Design viewport</h1><div class="bg-ppt-primary h-16 w-full"></div></div>')
      .replace("width:800px;height:450px", `width:${width}px;height:${height}px`);
    const result = await convert({
      ...input(html), canvas: { width, height }, converterVersion: DESIGN_CONVERTER_VERSION,
    }, { browser, outputDir: join(temp, `design-${width}-${height}`) });
    assertScene(result);
    assert.equal(result.schemaVersion, "0.5.0");
    assert.equal(result.page.width, width);
    assert.equal(result.page.height, height);
    assert.ok(result.nodes.some(n => n.kind === "text" && n.text.sourceText === "Design viewport"));
    const page = await browser.newPage({ viewport: { width, height } });
    try {
      await page.setContent(html);
      const expected = PNG.sync.read(await page.locator("#ppt-page").screenshot());
      await page.setContent('<html><body style="margin:0"><canvas></canvas></body></html>');
      await page.addScriptTag({ content: bundle });
      await page.evaluate(async ({ scene, width, height }) => {
        const renderer = new window.SceneRenderer(document.querySelector("canvas"));
        await renderer.load(scene, async () => { throw new Error("Unexpected asset"); },
          async (style, sample) => { await document.fonts.load(`${style.fontSize}px ${style.fontFamily}`, sample); });
        renderer.resize(width / 2, height / 2, 2);
        renderer.render();
      }, { scene: result, width, height });
      const actual = PNG.sync.read(await page.locator("canvas").screenshot({ scale: "device" }));
      assert.equal(actual.width, width / 2);
      assert.equal(actual.height, height / 2);
      for (const [x, y] of [[100, 100], [width / 2, height - 50], [width / 2, height / 2]]) {
        const e = (y * expected.width + x) * 4;
        const a = (Math.floor(y / 2) * actual.width + Math.floor(x / 2)) * 4;
        assert.deepEqual(actual.data.subarray(a, a + 4), expected.data.subarray(e, e + 4));
      }
    } finally { await page.close(); }
    await assert.rejects(convert({
      ...input(html), canvas: { width: 800, height: 450 }, converterVersion: DESIGN_CONVERTER_VERSION,
    }, { browser, outputDir: join(temp, "wrong-size") }), /UNSUPPORTED_CANVAS/);
  });
}
test("Design rejects unbounded dimensions and PPT rejects custom capture dimensions", async () => {
  for (const canvas of [{ width: 319, height: 450 }, { width: 1920, height: 1920 }, { width: 1080.5, height: 1920 }])
    await assert.rejects(convert({ ...input(fixture), canvas, converterVersion: DESIGN_CONVERTER_VERSION },
      { browser, outputDir: join(temp, "invalid") }), /INVALID_CAPTURE_CANVAS/);
  await assert.rejects(convert({ ...input(fixture), canvas: { width: 1080, height: 1920 } },
    { browser, outputDir: join(temp, "invalid-ppt") }), /INVALID_CAPTURE_CANVAS/);
});
test("real Chromium capture preserves text, table, SVG and deterministic identity", async () => {
  const results = [];
  for (let i = 0; i < 3; i++)
    results.push(
      await convert(input(fixture), {
        browser,
        outputDir: join(temp, "capture" + i),
      }),
    );
  scene = results[0];
  assertScene(scene);
  assert.equal(hash(canonical(results[0])), hash(canonical(results[1])));
  assert.equal(hash(canonical(results[1])), hash(canonical(results[2])));
  const texts = scene.nodes.filter((n) => n.kind === "text");
  const display = texts
    .flatMap((n) =>
      n.text.lines.flatMap((l) => l.fragments.map((f) => f.displayText)),
    )
    .join("");
  assert.match(display, /HELLO SCENE IR/);
  assert.match(display, /重点内容/);
  assert.match(display, /保留  空格/);
  assert.equal(scene.nodes.filter((n) => n.kind === "table").length, 1);
  assert.equal(scene.assets.length, 2);
  assert.equal(scene.assets[0].mediaType, "image/svg+xml");
  assert.equal(scene.assets[0].fallbackAssetId, scene.assets[1].id);
  assert.equal(texts.filter((n) => n.text.sourceText === "转换服务").length, 1);
  assert.equal(
    scene.textInventory.filter((t) => t.sourceText === "转换服务").length,
    1,
  );
  assert.ok(texts.some((n) => n.text.lines.length > 1));
  assert.ok(
    texts.some((n) => n.text.paragraphs[0].runs[0].style.letterSpacing > 0),
  );
  assert.ok(!scene.assets.some((a) => a.width >= 800 && a.height >= 450));
});
test("Canvas consumes only SceneIR/assets and matches independent HTML regions at DPR 1 and 2", async () => {
  const page = await browser.newPage({
    viewport: { width: 800, height: 450 },
    deviceScaleFactor: 1,
  });
  try {
    await page.setContent(fixture);
    await page.evaluate(() => document.fonts.ready);
    const expected = PNG.sync.read(
      await page.locator("#ppt-page").screenshot(),
    );
    const assets = Object.fromEntries(
      await Promise.all(
        scene.assets.map(async (a) => [
          a.id,
          (await readFile(join(temp, "capture0", a.file))).toString("base64"),
        ]),
      ),
    );
    await page.setContent(
      '<html><body style="margin:0"><canvas></canvas></body></html>',
    );
    await page.route("**/*", (route) => route.abort());
    await page.addScriptTag({ content: bundle });
    for (const dpr of [1, 2]) {
      await page.evaluate(
        async ({ scene, assets, dpr }) => {
          const canvas = document.querySelector("canvas");
          const renderer = new window.SceneRenderer(canvas);
          window.renderer?.dispose();
          window.renderer = renderer;
          await renderer.load(
            scene,
            async (asset) =>
              window.decodeAsset(
                Uint8Array.from(atob(assets[asset.id]), (c) => c.charCodeAt(0)),
                asset.mediaType,
              ),
            async (s, text) => {
              await document.fonts.load(
                `${s.fontStyle} ${s.fontWeight} ${s.fontSize}px ${s.fontFamily}`,
                text,
              );
            },
          );
          renderer.resize(800, 450, dpr);
          renderer.render();
        },
        { scene, assets, dpr },
      );
      const actual = PNG.sync.read(
        await page.locator("canvas").screenshot({ scale: "css" }),
      );
      await mkdir("test-results/scene", { recursive: true });
      await writeFile("test-results/scene/html.png", PNG.sync.write(expected));
      await writeFile(
        `test-results/scene/canvas-dpr${dpr}.png`,
        PNG.sync.write(actual),
      );
      // Independent HTML screenshot is the oracle. Test text, cards, assets and cells separately.
      for (const [name, x, y, w, h] of [
        ["title", 28, 25, 600, 50],
        ["rich text", 40, 85, 350, 140],
        ["SVG opacity", 410, 90, 130, 100],
        ["table", 30, 320, 730, 95],
      ]) {
        let different = 0,
          total = 0;
        for (let py = y; py < y + h; py++)
          for (let px = x; px < x + w; px++) {
            const i = (py * 800 + px) * 4;
            const delta = Math.max(
              ...[0, 1, 2].map((c) =>
                Math.abs(expected.data[i + c] - actual.data[i + c]),
              ),
            );
            if (delta > 35) different++;
            total++;
          }
        assert.ok(
          different / total < 0.075,
          `${name} DPR ${dpr}: ${((different / total) * 100).toFixed(2)}% pixels differ; ${temp}`,
        );
        if (name !== "SVG opacity") {
          // Windows HTML uses LCD subpixel antialiasing; Canvas uses grayscale.
          // Compare glyph silhouettes in both directions, ignoring fringe color.
          const dark = (png, i) =>
            png.data[i] * 0.2126 +
              png.data[i + 1] * 0.7152 +
              png.data[i + 2] * 0.0722 <
            160;
          for (const [source, target, label] of [
            [expected, actual, "recall"],
            [actual, expected, "precision"],
          ]) {
            let ink = 0,
              matched = 0;
            for (let py = y; py < y + h; py++)
              for (let px = x; px < x + w; px++) {
                const i = (py * 800 + px) * 4;
                if (!dark(source, i)) continue;
                ink++;
                let found = false;
                for (let dy = -1; dy <= 1 && !found; dy++)
                  for (let dx = -1; dx <= 1; dx++)
                    if (dark(target, ((py + dy) * 800 + px + dx) * 4)) {
                      found = true;
                      break;
                    }
                if (found) matched++;
              }
            assert.ok(
              ink > 100 && matched / ink > 0.95,
              `${name} DPR ${dpr}: text ink ${label} ${((matched / ink) * 100).toFixed(2)}%`,
            );
          }
        }
      }
    }
  } finally {
    await page.close();
  }
});
test("mixed direct text, blocks, invisible text and collapsed whitespace retain unique provenance", async () => {
  const s = await convert(
    input(
      tailwind(
        '<div>before<div>block</div>after <span>inline</span><span class="hidden">secret</span></div>',
      ),
    ),
    { browser, outputDir: join(temp, "mixed") },
  );
  for (const source of ["before", "block", "after ", "inline"])
    assert.equal(
      s.nodes.filter((n) => n.kind === "text" && n.text.sourceText === source)
        .length,
      1,
    );
  assert.equal(
    s.textInventory.find((t) => t.sourceText === "secret").reason,
    "display:none",
  );
  const invalid = structuredClone(s);
  invalid.paintOps.push(invalid.paintOps.find((p) => p.op === "draw"));
  assert.throws(() => assertScene(invalid));
});
test("stacking descendants, group opacity and overflow clips match browser paints", async () => {
  const cases = [
    '<div class="absolute inset-0"><div class="absolute inset-0 z-10 bg-ppt-primary"></div></div><div class="absolute inset-0 bg-ppt-surface"></div>',
    '<div class="absolute inset-0 opacity-50 bg-ppt-primary"><div class="absolute inset-0 bg-ppt-primary"></div></div>',
    '<div class="absolute inset-0 rounded-full overflow-hidden"><div class="absolute inset-0 z-10 bg-ppt-primary"></div></div><div class="absolute inset-0 opacity-50 bg-ppt-surface"></div>',
  ];
  const page = await browser.newPage({ viewport: { width: 800, height: 450 } });
  try {
    for (let i = 0; i < cases.length; i++) {
      const html = tailwind(cases[i]);
      const s = await convert(input(html), {
        browser,
        outputDir: join(temp, "paint" + i),
      });
      await page.setContent(html);
      const expected = PNG.sync.read(
        await page.locator("#ppt-page").screenshot(),
      );
      await page.setContent(
        '<html><body style="margin:0"><canvas></canvas></body></html>',
      );
      await page.addScriptTag({ content: bundle });
      await page.evaluate(async (s) => {
        const r = new window.SceneRenderer(document.querySelector("canvas"));
        await r.load(
          s,
          () => {
            throw new Error("Unexpected asset");
          },
          async () => {},
        );
        r.resize(800, 450, 1);
        r.render();
      }, s);
      const actual = PNG.sync.read(await page.locator("canvas").screenshot());
      for (const [x, y] of [
        [1, 1],
        [400, 225],
        [5, 225],
        [795, 440],
      ]) {
        const p = (y * 800 + x) * 4;
        for (let c = 0; c < 4; c++)
          assert.ok(
            Math.abs(expected.data[p + c] - actual.data[p + c]) < 3,
            `case ${i}, ${x}/${y}, channel ${c}`,
          );
      }
    }
    const shadow = await convert(
      input(
        tailwind(
          '<div class="shadow-md rounded-lg bg-ppt-surface p-4">shadow</div>',
        ),
      ),
      { browser, outputDir: join(temp, "shadow") },
    );
    assert.ok(shadow.diagnostics.some((d) => d.code === "SHADOW_CANVAS_BLUR"));
  } finally {
    await page.close();
  }
});
test("author scripts and external resources are never executed by capture", async () => {
  const s = await convert(
    input(
      tailwind(
        '<script>document.querySelector("main").textContent="EXECUTED"</script><p>safe</p>',
      ),
    ),
    { browser, outputDir: join(temp, "safe") },
  );
  assert.equal(
    s.nodes
      .filter((n) => n.kind === "text")
      .map((n) => n.text.sourceText)
      .join(""),
    "safe",
  );
});
test("unsupported effects fail explicitly and do not poison the hot browser", async () => {
  await assert.rejects(
    convert(input(tailwind('<div style="filter:blur(1px)">bad</div>')), {
      browser,
      outputDir: join(temp, "bad"),
    }),
    /UNSUPPORTED_EFFECT/,
  );
  assert.ok(
    (
      await convert(input(tailwind("<p>next page</p>")), {
        browser,
        outputDir: join(temp, "next"),
      })
    ).nodes.length,
  );
  assert.equal(browser.contexts().length, 0);
});
