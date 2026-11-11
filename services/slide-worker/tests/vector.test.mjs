import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, mkdir } from "node:fs/promises";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { chromium } from "playwright-core";
import { PNG } from "pngjs";
import { assertScene, textTarget } from "@lumina/scene-core";
import { convert, hash } from "../dist/capture.js";
import { planPptxPage } from "../dist/export/plan.js";
import { resolveFont } from "../dist/export/fonts.js";
import { sceneToSvg } from "../dist/export/svg.js";
import { writePptx } from "../dist/export/writer.js";

let browser, temp;
before(async () => {
  browser = await chromium.launch({ headless: true });
  temp = await mkdtemp(join(tmpdir(), "lumina-vector-"));
});
after(async () => {
  await browser?.close();
  await rm(temp, { recursive: true, force: true });
});
const html = (body, css = "") =>
  `<!doctype html><html><head><style>body{margin:0}#ppt-page{position:relative;width:800px;height:450px;overflow:hidden}${css}</style></head><body><main id="ppt-page">${body}</main></body></html>`;
const capture = (source, name) =>
  convert(
    { html: source, htmlHash: hash(source), cssHash: hash("") },
    { browser, outputDir: join(temp, name) },
  );

test("SVG retains computed styles, local references and text ownership without baking root opacity twice", async () => {
  const source = html(
    '<svg viewBox="0 0 300 140" width="300" height="140" style="opacity:.5;color:oklch(.6 .15 260)"><defs><clipPath id="crop"><rect width="250" height="140"/></clipPath></defs><g clip-path="url(#crop)"><rect width="300" height="140" fill="currentColor"/><text x="20" y="70">Vector 中文</text></g></svg>',
    'svg text{fill:var(--label);font:600 24px "Microsoft YaHei";letter-spacing:1px;text-anchor:start}#ppt-page{--label:#ffffff}',
  );
  const scene = await capture(source, "style");
  assert.equal(scene.schemaVersion, "0.4.0");
  const asset = scene.assets.find((a) => a.mediaType === "image/svg+xml");
  const svg = await readFile(join(temp, "style", asset.file), "utf8");
  assert.match(svg, /viewBox="0 0 300 140"/);
  assert.match(svg, /clip-path: url\((?:&quot;)?#crop(?:&quot;)?\)/);
  assert.match(svg, /letter-spacing: 1px/);
  assert.doesNotMatch(svg, /currentColor|var\(|oklch\(/);
  assert.match(svg, /opacity: 1;/);
  assert.equal(
    scene.assets.find((a) => a.id === asset.fallbackAssetId).width,
    900,
  );
  const text = scene.textInventory.find((t) => t.sourceText === "Vector 中文");
  assert.equal(text.target.kind, "asset");
  assert.equal(text.target.assetId, asset.id);
  const invalid = structuredClone(scene);
  invalid.textInventory.find(
    (t) => t.sourceText === "Vector 中文",
  ).target.assetId = asset.fallbackAssetId;
  assert.throws(() => assertScene(invalid), /INVENTORY_TARGET/);
  const legacy = structuredClone(scene);
  legacy.schemaVersion = "0.3.0";
  legacy.diagnostics = [];
  legacy.source.converterVersion = "dom-canvas-0.3.0";
  legacy.assets = legacy.assets.filter((a) => a.mediaType === "image/png");
  legacy.nodes.find((n) => n.kind === "image").assetId = legacy.assets[0].id;
  legacy.textInventory.forEach((t) => {
    delete t.target;
    if (t.reason === "svg-asset") t.nodeId = null;
  });
  assertScene(legacy);
  assert.equal(
    textTarget(
      legacy,
      legacy.textInventory.find((t) => t.sourceText === "Vector 中文"),
    ).kind,
    "asset",
  );
});

test("capture preserves document language and inherited body style", async () => {
  const source = html("<p>继承语言与字体</p>")
    .replace("<html>", '<html lang="zh-CN">')
    .replace(
      "<body>",
      '<body style="font-family:Microsoft YaHei;font-size:24px;color:#2563eb">',
    );
  const scene = await capture(source, "inheritance");
  const text = scene.nodes.find((n) => n.kind === "text");
  const style = text.text.paragraphs[0].runs[0].style;
  assert.equal(style.fontFamily, '"Microsoft YaHei"');
  assert.equal(style.fontSize, 24);
  assert.equal(style.color, "rgb(37, 99, 235)");
});

test("complex clipped geometry emits SVG while ordinary and outside-page text retain correct coverage", async () => {
  const source = html(
    '<div style="position:absolute;left:20px;top:30px;width:230px;height:150px;border-radius:25px 8px 32px 15px;border:8px solid #2563eb;background:#bfdbfe"></div><div style="position:absolute;left:300px;top:30px;width:100px;height:100px;overflow:hidden;border-radius:30px"><div style="width:160px;height:160px;background:#f97316"></div></div><p style="position:absolute;left:20px;top:260px;font:20px Arial">Visible text</p><p style="position:absolute;left:900px;top:50px;font:20px Arial">Outside text</p>',
  );
  const scene = await capture(source, "geometry");
  const plan = planPptxPage(scene);
  assert.equal(plan.elements.filter((e) => e.kind === "raster").length, 0);
  assert.equal(plan.elements.filter((e) => e.kind === "svg").length, 2);
  assert.equal(plan.invisibleNodeIds.length, 1);
  assert.ok(
    plan.elements.some((e) => e.kind === "text" && e.text === "Visible text"),
  );
  const page = await browser.newPage({ viewport: { width: 800, height: 450 } });
  try {
    await page.setContent(source);
    const expected = PNG.sync.read(await page.screenshot());
    const regions = plan.elements
      .filter((e) => e.kind === "svg")
      .map((e) => ({
        ...e.region,
        svg: sceneToSvg(scene, e.region.nodeIds, e.region.frame),
      }));
    await page.setContent(
      '<html><body style="margin:0"><canvas width="800" height="450"></canvas></body></html>',
    );
    await page.evaluate(async (regions) => {
      const ctx = document.querySelector("canvas").getContext("2d");
      ctx.fillStyle = "white";
      ctx.fillRect(0, 0, 800, 450);
      for (const r of regions) {
        const img = new Image();
        img.src =
          "data:image/svg+xml;charset=utf-8," + encodeURIComponent(r.svg);
        await img.decode();
        ctx.drawImage(img, r.frame.x, r.frame.y, r.frame.width, r.frame.height);
      }
    }, regions);
    const actual = PNG.sync.read(await page.screenshot());
    let different = 0;
    for (let y = 0; y < 220; y++)
      for (let x = 0; x < 500; x++) {
        const i = (y * 800 + x) * 4;
        if (
          [0, 1, 2].some(
            (c) => Math.abs(expected.data[i + c] - actual.data[i + c]) > 25,
          )
        )
          different++;
      }
    assert.ok(
      different / 110000 < 0.01,
      `geometry difference ${different}/110000`,
    );
  } finally {
    await page.close();
  }
});

test("font failure only flattens its display fragment and shadows keep the body native", async () => {
  const scene = await capture(
    html(
      '<p style="position:absolute;left:20px;top:20px;font:20px Arial">Keep first line<br>Unsupported second line</p><div style="position:absolute;left:20px;top:150px;width:100px;height:100px;background:#2563eb;box-shadow:2px 3px 5px #999"></div>',
    ),
    "fragments",
  );
  const plan = planPptxPage(scene, (style, text) => {
    if (text.includes("second") || [...text].some((c) => c === "U"))
      throw new Error("FONT_UNAVAILABLE: injected");
    return resolveFont(style, text);
  });
  assert.ok(
    plan.elements.some(
      (e) => e.kind === "text" && e.text === "Keep first line",
    ),
  );
  const raster = plan.elements.find(
    (e) => e.kind === "raster" && e.region.reasonCode === "FONT_METRICS",
  );
  assert.equal(raster.region.fragmentKeys.length, 1);
  assert.ok(raster.region.frame.width < 400 && raster.region.frame.height < 40);
  assert.ok(
    plan.elements.some((e) => e.kind === "raster" && e.region.shadowOnly),
  );
  assert.ok(
    plan.elements.some(
      (e) => e.kind === "shape" && e.node.shadows.length === 0,
    ),
  );
  const output = join(temp, "shadow-export");
  await mkdir(output);
  const report = await writePptx(
    {
      title: "Shadow validation",
      inputHash: "a".repeat(64),
      pages: [{ scene, sceneId: "shadow", assets: {} }],
    },
    output,
    resolve,
    browser,
  );
  assert.equal(
    report.pages[0].rasterNodeIds.length,
    0,
    "shadow-only bitmap must not count the foreground as flattened",
  );
});
