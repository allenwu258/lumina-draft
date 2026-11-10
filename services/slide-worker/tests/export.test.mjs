import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { chromium } from "playwright-core";
import { PNG } from "pngjs";
import { convert, hash } from "../dist/capture.js";
import { writePptx } from "../dist/export/writer.js";
import { planPptxPage } from "../dist/export/plan.js";
import { color } from "../dist/export/color.js";
import { resolveFont } from "../dist/export/fonts.js";
import { rasterRegions } from "../dist/export/raster.js";
import { fixture, css } from "./fixtures.mjs";

test("saved Scene exports native text, tables and assets without HTML or browser", async () => {
  const temp = await mkdtemp(join(tmpdir(), "lumina-pptx-"));
  const browser = await chromium.launch({ headless: true });
  try {
    const html =
      '<!doctype html><html><body style="margin:0"><main id="ppt-page" style="position:relative;width:800px;height:450px;background:white;font-family:Microsoft YaHei,sans-serif"><h1 style="margin:0;position:absolute;left:40px;top:36px;font-size:32px">中文标题 English</h1><p style="position:absolute;left:40px;top:120px;font-size:20px">第一行正文<br>第二行文字</p><div style="position:absolute;left:40px;top:230px;width:200px;height:80px;background:#dbeafe;border:2px solid #2563eb;border-radius:12px"></div></main></body></html>';
    const scene = await convert(
      { html, htmlHash: hash(html), cssHash: hash("") },
      { browser, outputDir: join(temp, "capture") },
    );
    await browser.close();
    const output = join(temp, "out");
    await mkdir(output);
    const report = await writePptx(
      {
        title: "中文测试",
        inputHash: "a".repeat(64),
        pages: [{ scene, sceneId: "native", assets: {} }],
      },
      output,
      resolve,
    );
    assert.equal(report.pages[0].rasterRegions, 0);
    assert.ok(
      report.pages[0].nativeTexts.join("").includes("中文标题 English"),
    );
    assert.ok(report.pages[0].nativeTexts.join("").includes("第二行文字"));
    const style = scene.nodes.find((n) => n.kind === "text").text.paragraphs[0]
      .runs[0].style;
    assert.ok(
      resolveFont(style, "↶").family,
      "system glyph fallback must resolve a symbol absent from the requested face",
    );
    const fallbackPlan = planPptxPage(scene, () => {
      throw new Error("FONT_UNAVAILABLE: mixed glyph fallback");
    });
    assert.ok(
      fallbackPlan.elements.some(
        (e) => e.kind === "raster" && e.region.reason === "字体度量差异",
      ),
    );
    assert.ok(
      (await readFile(join(output, "presentation.pptx"))).length > 5000,
    );
    const large = join(temp, "twenty");
    await mkdir(large);
    const twenty = await writePptx(
      {
        title: "20 pages",
        inputHash: "c".repeat(64),
        pages: Array.from({ length: 20 }, (_, i) => ({
          scene,
          sceneId: `page-${i + 1}`,
          assets: {},
        })),
      },
      large,
      resolve,
    );
    assert.equal(twenty.pages.length, 20);
    assert.deepEqual(
      twenty.pages.map((p) => p.sceneId),
      Array.from({ length: 20 }, (_, i) => `page-${i + 1}`),
    );
    const acceptance = resolve("data/pptx-acceptance/native");
    await mkdir(acceptance, { recursive: true });
    await writeFile(
      join(acceptance, "presentation.pptx"),
      await readFile(join(output, "presentation.pptx")),
    );
    await writeFile(join(acceptance, "scene.json"), JSON.stringify(scene));
  } finally {
    await browser.close();
    await rm(temp, { recursive: true, force: true });
  }
});

test("composite Scene export preserves outside native text and transparent regions", async () => {
  const temp = await mkdtemp(join(tmpdir(), "lumina-pptx-composite-"));
  const browser = await chromium.launch({ headless: true });
  try {
    const scene = await convert(
      { html: fixture, htmlHash: hash(fixture), cssHash: hash(css) },
      { browser, outputDir: join(temp, "capture") },
    );
    const assets = Object.fromEntries(
      scene.assets.map((a) => [a.id, join(temp, "capture", a.file)]),
    );
    const output = join(temp, "out");
    await mkdir(output);
    const report = await writePptx(
      {
        title: "局部合成",
        inputHash: "b".repeat(64),
        pages: [
          { scene, sceneId: "composite", assets },
          { scene, sceneId: "second", assets },
        ],
      },
      output,
      resolve,
      browser,
    );
    assert.equal(report.pages.length, 2);
    assert.ok(report.pages[0].nativeTexts.join("").includes("HELLO SCENE IR"));
    assert.ok(report.pages[0].rasterRegions > 0);
    assert.equal(
      new Set(report.pages[0].coveredNodeIds).size,
      scene.paintOps.filter((o) => o.op === "draw").length,
    );
    const region = planPptxPage(scene).elements.find(
      (e) => e.kind === "raster",
    ).region;
    const bytes = Object.fromEntries(
      await Promise.all(
        scene.assets.map(async (a) => [
          a.id,
          (await readFile(assets[a.id])).toString("base64"),
        ]),
      ),
    );
    const [data] = await rasterRegions(browser, scene, bytes, [region]);
    const png = PNG.sync.read(Buffer.from(data.split(",")[1], "base64"));
    assert.ok(
      png.data.some((v, i) => i % 4 === 3 && v < 255),
      "region must retain transparency",
    );
    const acceptance = resolve("data/pptx-acceptance/composite");
    await mkdir(acceptance, { recursive: true });
    await writeFile(
      join(acceptance, "presentation.pptx"),
      await readFile(join(output, "presentation.pptx")),
    );
    await writeFile(join(acceptance, "scene.json"), JSON.stringify(scene));
  } finally {
    await browser.close();
    await rm(temp, { recursive: true, force: true });
  }
});

test("computed Tailwind colors convert to sRGB and alpha without silent black", () => {
  assert.deepEqual(color("rgba(37, 99, 235, 0.5)"), {
    color: "2563EB",
    transparency: 50,
  });
  assert.deepEqual(color("oklch(1 0 0)"), { color: "FFFFFF", transparency: 0 });
  assert.deepEqual(color("oklch(0 0 0 / 0.25)"), {
    color: "000000",
    transparency: 75,
  });
  assert.throws(() => color("unknown(1 2 3)"));
});
