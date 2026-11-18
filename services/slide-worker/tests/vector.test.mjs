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

test("transparent page uses the document canvas background color", async () => {
  const scene = await capture(
    html("<p>Body background remains visible</p>", "#ppt-page{background:transparent}body{background:#123456}"),
    "body-background-color",
  );
  assert.equal(scene.page.background, "rgb(18, 52, 86)");
});

test("element CSS gradients are captured as background-only assets and keep text editable", async () => {
  const scene = await capture(
    html(
      '<div style="position:absolute;inset:0;background:linear-gradient(90deg,#ff0000,#0000ff)"><span style="position:absolute;left:100px;top:100px;color:white;font:28px Arial">Gradient foreground text</span></div>',
    ),
    "element-gradient-background",
  );
  const imageNode = scene.nodes.find(
    (node) => node.kind === "image" && node.sourcePath !== "main",
  );
  assert.ok(imageNode);
  const asset = scene.assets.find((candidate) => candidate.id === imageNode.assetId);
  assert.equal(asset.mediaType, "image/png");
  assert.ok(asset.bakedEffects.includes("css-background-image"));
  const png = PNG.sync.read(await readFile(join(temp, "element-gradient-background", asset.file)));
  assert.equal(png.width, 800);
  assert.equal(png.height, 450);
  for (let offset = 0; offset < png.data.length; offset += 4) {
    const [red, green, blue, alpha] = png.data.subarray(offset, offset + 4);
    assert.ok(alpha === 0 || !(red > 250 && green > 250 && blue > 250));
  }
  const text = scene.nodes.find(
    (node) => node.kind === "text" && node.text.sourceText === "Gradient foreground text",
  );
  assert.ok(text);
  assert.equal(
    scene.textInventory.find((item) => item.sourceText === text.text.sourceText).target.kind,
    "text",
  );
  const plan = planPptxPage(scene);
  assert.ok(plan.elements.some((element) => element.kind === "image" && element.node.id === imageNode.id));
  assert.ok(plan.elements.some((element) => element.kind === "text" && element.text === "Gradient foreground text"));
  assert.ok(!plan.elements.some((element) => element.kind === "raster"));
  assert.ok(scene.diagnostics.some((item) => item.code === "CSS_BACKGROUND_IMAGE_LOCAL_ASSET"));
});

test("document gradient is captured as a page background layer without flattening page text", async () => {
  const scene = await capture(
    html(
      '<p style="position:absolute;left:30px;top:40px;font:24px Arial;color:#fff">Document background text</p>',
      "#ppt-page{background:transparent}body{background:linear-gradient(90deg,#102030,#405060)}",
    ),
    "document-gradient-background",
  );
  const imageNode = scene.nodes.find(
    (node) => node.kind === "image" && node.id === "page_background_image",
  );
  assert.ok(imageNode);
  const asset = scene.assets.find((candidate) => candidate.id === imageNode.assetId);
  const png = PNG.sync.read(await readFile(join(temp, "document-gradient-background", asset.file)));
  assert.equal(png.width, 800);
  assert.equal(png.height, 450);
  assert.notDeepEqual(
    [...png.data.subarray(0, 3)],
    [...png.data.subarray((png.width - 1) * 4, (png.width - 1) * 4 + 3)],
  );
  assert.ok(scene.nodes.some((node) => node.kind === "text" && node.text.sourceText === "Document background text"));
  assert.ok(!scene.assets.some((candidate) => candidate.bakedEffects.includes("full-page-composite")));
  assert.ok(scene.diagnostics.some((item) => item.code === "CSS_PAGE_BACKGROUND_LOCAL_ASSET"));
});

test("Windows font map tries installed stack names before aliases and defaults", () => {
  const style = {
    fontFamily: '"PingFang SC", Arial',
    fontSize: 18,
    fontWeight: "400",
    fontStyle: "normal",
    letterSpacing: 0,
  };
  assert.equal(resolveFont(style, "Latin").family, "Arial");
  assert.equal(resolveFont(style, "中文").family, "Microsoft YaHei");
  assert.equal(
    resolveFont({ ...style, fontFamily: '"Unknown Typeface"' }, "中文").family,
    "Microsoft YaHei",
  );
});

test("mixed-script text is always split into the first covering font runs", async () => {
  const scene = await capture(
    html(
      '<p style="position:absolute;left:20px;top:20px;font:24px Arial, Microsoft YaHei">Latin 中文</p>',
    ),
    "mixed-font-runs",
  );
  const node = scene.nodes.find(
    (candidate) => candidate.kind === "text" && candidate.text.sourceText === "Latin 中文",
  );
  const fragment = node.text.lines[0].fragments[0];
  fragment.measuredWidth = [...fragment.displayText].length * 9;
  const resolver = (style, text) => {
    const cjk = /[\u3400-\u9fff]/u.test(text);
    return {
      family: cjk ? "CjkFace" : "LatinFace",
      postscript: cjk ? "CjkFace-Regular" : "LatinFace-Regular",
      fileSha256: (cjk ? "b" : "a").repeat(64),
      ascent: 800,
      descent: 200,
      unitsPerEm: 1000,
      width: [...text].length * 9,
      bold: false,
      weight: 400,
      requestedWeight: 400,
      faceWeight: 400,
      italic: false,
    };
  };
  const plan = planPptxPage(scene, resolver);
  const runs = plan.elements.filter((element) => element.kind === "text");
  assert.equal(runs.map((run) => run.text).join(""), fragment.displayText);
  assert.deepEqual(
    [...new Set(runs.map((run) => run.font.family))],
    ["LatinFace", "CjkFace"],
  );
});

test("text-only SVG becomes editable native text", async () => {
  const scene = await capture(
    html(
      '<svg width="240" height="80"><text x="20" y="52" fill="#2563eb" font-family="Arial" font-size="24">Editable SVG 文字</text></svg>',
    ),
    "simple-svg-text",
  );
  const inventory = scene.textInventory.find(
    (item) => item.sourceText === "Editable SVG 文字",
  );
  assert.equal(inventory.target.kind, "text");
  assert.equal(scene.assets.length, 0);
  const node = scene.nodes.find(
    (candidate) => candidate.kind === "text" && candidate.text.sourceText === inventory.sourceText,
  );
  assert.equal(node.text.paragraphs[0].runs[0].style.color, "rgb(37, 99, 235)");
  assert.ok(scene.diagnostics.some((item) => item.code === "SVG_TEXT_NATIVE"));
});

test("plain SVG groups preserve editable text while transformed groups remain vector assets", async () => {
  const editable = await capture(
    html(
      '<svg width="240" height="80"><g fill="#2563eb"><text x="20" y="52" font-family="Arial" font-size="24">Grouped text</text></g></svg>',
    ),
    "grouped-svg-text",
  );
  assert.equal(
    editable.textInventory.find((item) => item.sourceText === "Grouped text").target.kind,
    "text",
  );
  assert.equal(editable.assets.length, 0);

  const transformed = await capture(
    html(
      '<svg width="240" height="80"><g transform="scale(1.2)"><text x="20" y="52" font-size="24">Transformed text</text></g></svg>',
    ),
    "transformed-svg-text",
  );
  assert.equal(
    transformed.textInventory.find((item) => item.sourceText === "Transformed text").target.kind,
    "asset",
  );
  assert.ok(!transformed.diagnostics.some((item) => item.code === "SVG_TEXT_NATIVE"));
});

test("complex SVG effects remain vector assets and unsafe URL references are omitted locally", async () => {
  const scene = await capture(
    html(
      '<svg width="180" height="90"><defs><filter id="soft"><feGaussianBlur stdDeviation="1"/></filter></defs><rect width="80" height="70" fill="#2563eb" style="filter:url(#soft);background-image:url(https://example.invalid/remote.png)"/><rect x="95" width="80" height="70" fill="url(https://example.invalid/remote.svg#paint)"/></svg>',
    ),
    "svg-effects",
  );
  const asset = scene.assets.find((item) => item.mediaType === "image/svg+xml");
  assert.ok(asset);
  assert.ok(scene.assets.find((item) => item.id === asset.fallbackAssetId));
  const svg = await readFile(join(temp, "svg-effects", asset.file), "utf8");
  assert.match(svg, /filter:\s*url\((?:&quot;)?#soft(?:&quot;)?\)/);
  assert.doesNotMatch(svg, /example\.invalid/);
  assert.ok(scene.diagnostics.some((item) => item.code === "SVG_EFFECT_RETAINED_ASSET"));
  assert.ok(scene.diagnostics.some((item) => item.code === "SVG_EXTERNAL_REFERENCE_OMITTED"));
  assert.ok(scene.diagnostics.some((item) => item.code === "CSS_BACKGROUND_RESOURCE_OMITTED"));
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

test("PPTX-inexpressible font weights flatten only their source fragment", async () => {
  const scene = await capture(
    html(
      '<p style="position:absolute;left:20px;top:20px;font:500 20px Arial">Medium weight</p><p style="position:absolute;left:20px;top:70px;font:400 20px Arial">Regular weight</p>',
    ),
    "font-weight-fallback",
  );
  const plan = planPptxPage(scene);
  assert.ok(
    plan.elements.some(
      (element) => element.kind === "raster" && element.region.reasonCode === "FONT_WEIGHT",
    ),
  );
  assert.ok(
    plan.elements.some((element) => element.kind === "text" && element.text === "Regular weight"),
  );
  assert.ok(
    plan.elements.every(
      (element) => element.kind !== "text" || element.text !== "Medium weight",
    ),
  );
});
