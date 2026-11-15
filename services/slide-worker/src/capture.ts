import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { platform } from "node:os";
import type { Browser } from "playwright-core";
import {
  assertScene,
  CONVERTER_VERSION,
  SCENE_VERSION,
  DESIGN_CONVERTER_VERSION,
  DESIGN_SCENE_VERSION,
  PPT_CANVAS,
  isDesignCanvas,
  type CanvasSize,
  type SceneIR,
} from "@lumina/scene-core";
import { extractDOM } from "./extract.js";
import { orderPaint } from "./paint.js";

export const hash = (value: string | Buffer) =>
  createHash("sha256").update(value).digest("hex");
export const canonical = (v: unknown): string =>
  JSON.stringify(v, (_key, value) =>
    value && typeof value === "object" && !Array.isArray(value)
      ? Object.fromEntries(
          Object.entries(value).sort(([a], [b]) => a.localeCompare(b)),
        )
      : value,
  );
const PAGE_RASTER_FALLBACK_CODES = new Set([
  "CSS_EFFECT_OMITTED",
  "CSS_TRANSFORM_OMITTED",
  "CSS_LIST_MARKER_OMITTED",
  "CSS_PSEUDO_CONTENT_OMITTED",
  "CSS_TEXT_STYLE_OMITTED",
  "CSS_TEXT_TRANSFORM_OMITTED",
  "CSS_BORDER_STYLE_APPROXIMATED",
  "CSS_PER_SIDE_DASHED_BORDER_APPROXIMATED",
  "CSS_ELLIPTICAL_RADIUS_APPROXIMATED",
  "CSS_INSET_SHADOW_OMITTED",
  "CSS_SHADOW_OMITTED",
  "SVG_EFFECT_OMITTED",
]);
export async function convert(
  input: {
    html: string;
    htmlHash: string;
    cssHash: string;
    converterVersion?: string;
    canvas?: CanvasSize;
  },
  runtime: { browser: Browser; outputDir: string },
  signal?: AbortSignal,
): Promise<SceneIR> {
  const converterVersion = input.converterVersion ?? CONVERTER_VERSION;
  const isDesign = converterVersion === DESIGN_CONVERTER_VERSION;
  const canvas = input.canvas ?? (isDesign ? undefined : PPT_CANVAS);
  if (!canvas || (isDesign ? !isDesignCanvas(canvas) :
      converterVersion !== CONVERTER_VERSION ||
      canvas.width !== PPT_CANVAS.width || canvas.height !== PPT_CANVAS.height))
    throw new Error("INVALID_CAPTURE_CANVAS");
  const { width, height } = canvas;
  const context = await runtime.browser.newContext({
    viewport: { width, height },
    deviceScaleFactor: 1,
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    serviceWorkers: "block",
    colorScheme: "light",
    reducedMotion: "reduce",
  });
  const abort = () => void context.close();
  signal?.addEventListener("abort", abort, { once: true });
  try {
    await context.route("**/*", (route) => route.abort());
    const page = await context.newPage();
    page.setDefaultTimeout(12000);
    // Static admission runs in Python. CSP is installed before parsing any author markup.
    await page.setContent(
      "<!doctype html><html><head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'\"></head><body></body></html>",
    );
    await page.evaluate((html) => {
      const parsed = new DOMParser().parseFromString(html, "text/html");
      // Language and body styles affect inherited fonts and all downstream geometry.
      // Copy presentation facts only; author event handlers are never attached.
      for (const [source, target] of [
        [parsed.documentElement, document.documentElement],
        [parsed.body, document.body],
      ]) {
        for (const attr of source.attributes)
          if (
            /^(lang|dir|class|id|style|bgcolor|text)$/.test(attr.name) ||
            /^(data-|aria-)/.test(attr.name)
          )
            target.setAttribute(attr.name, attr.value);
      }
      document.head.append(
        ...[...parsed.head.childNodes].filter(
          (n) =>
            n instanceof HTMLStyleElement ||
            (n instanceof HTMLMetaElement && !n.hasAttribute("http-equiv")),
        ),
      );
      document.body.replaceChildren(...parsed.body.childNodes);
    }, input.html);
    await page.addStyleTag({
      content:
        "*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}",
    });
    await page.evaluate(async () => {
      await document.fonts.ready;
      let last = "",
        stable = 0;
      for (let attempt = 0; attempt < 30; attempt++) {
        await new Promise(requestAnimationFrame);
        const next = JSON.stringify(
          [...document.querySelectorAll("#ppt-page, #ppt-page *")].map((e) => {
            const r = e.getBoundingClientRect();
            return [r.x, r.y, r.width, r.height];
          }),
        );
        stable = next === last ? stable + 1 : 0;
        last = next;
        if (stable >= 2) return;
      }
      throw new Error("LAYOUT_UNSTABLE");
    });
    const data = await page.evaluate(extractDOM, { width, height });
    const rasterizedDiagnostics = data.diagnostics.filter((d) =>
      PAGE_RASTER_FALLBACK_CODES.has(d.code),
    );
    let pageRaster:
      | { bytes: Buffer; id: string; file: string; pixelWidth: number; pixelHeight: number }
      | undefined;
    if (rasterizedDiagnostics.length) {
      const screenshot = await page.locator("#ppt-page").screenshot({
        type: "png",
        animations: "disabled",
        timeout: 5000,
      });
      if (
        screenshot.length < 24 ||
        !screenshot.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))
      )
        throw new Error("PAGE_RASTER_INVALID");
      const pixelWidth = screenshot.readUInt32BE(16);
      const pixelHeight = screenshot.readUInt32BE(20);
      if (pixelWidth !== width || pixelHeight !== height)
        throw new Error("PAGE_RASTER_DIMENSIONS");
      if (screenshot.length > 8 * 1024 * 1024)
        throw new Error("SCENE_ASSET_LIMIT");
      const assetId = `fallback_${hash(input.htmlHash + rasterizedDiagnostics.map((d) => d.code).join(",")).slice(0, 20)}`;
      const nodeId = "main_page_raster_image";
      pageRaster = {
        bytes: screenshot,
        id: assetId,
        file: `${assetId}.png`,
        pixelWidth,
        pixelHeight,
      };
      for (const diagnostic of rasterizedDiagnostics) {
        diagnostic.nodeId = nodeId;
        diagnostic.details = {
          ...(diagnostic.details || {}),
          pageRasterFallback: true,
          editability: "page-image",
        };
      }
      if (data.diagnostics.length >= 512) data.diagnostics.length = 511;
      data.diagnostics.push({
        code: "FULL_PAGE_RASTER_FALLBACK",
        message: "The source Chromium page was preserved as one bounded page image because one or more visual features cannot be represented safely as native Scene elements.",
        sourcePath: "main",
        nodeId,
        severity: "warning",
        details: {
          assetId,
          width,
          height,
          pixelWidth,
          pixelHeight,
          sourceFeatureCount: rasterizedDiagnostics.length,
          sourceFeatures: [...new Set(rasterizedDiagnostics.map((d) => d.code))].join(","),
          strategy: "full-page-raster",
          editability: "page-image",
        },
      });
      data.nodes = [
        {
          id: nodeId,
          sourcePath: "main",
          kind: "image",
          frame: { x: 0, y: 0, width, height },
          assetId,
        },
      ];
      data.paintOps = [{ op: "draw", nodeId }];
      data.paintGroups = [];
      data.images = [];
      data.fonts = [];
      for (const item of data.textInventory) {
        if (item.target?.kind === "hidden") continue;
        item.nodeId = nodeId;
        item.reason = "page-raster";
        item.target = { kind: "asset", nodeId, assetId };
      }
    }
    if (!pageRaster)
      data.paintOps = orderPaint(data.nodes, data.paintOps, data.paintGroups);
    await mkdir(runtime.outputDir, { recursive: true });
    const assets: SceneIR["assets"] = [];
    let totalBytes = 0;
    for (const img of data.images) {
      for (const vector of [true, false]) {
        const bytes = vector
          ? Buffer.from(img.svg, "utf8")
          : Buffer.from(img.data, "base64");
        totalBytes += bytes.length;
        if (bytes.length > 8 * 1024 * 1024 || totalBytes > 32 * 1024 * 1024)
          throw new Error("SCENE_ASSET_LIMIT");
        const id = img.id + (vector ? "" : "_png"),
          file = id + (vector ? ".svg" : ".png");
        await writeFile(join(runtime.outputDir, file), bytes);
        assets.push({
          id,
          mediaType: vector ? "image/svg+xml" : "image/png",
          sha256: hash(bytes),
          byteLength: bytes.length,
          width: vector ? img.width : img.pixelWidth,
          height: vector ? img.height : img.pixelHeight,
          file,
          coveredNodeIds: [img.id + "_image"],
          bakedEffects: vector ? [] : ["svg-content"],
          ...(vector ? { fallbackAssetId: img.id + "_png" } : {}),
        });
      }
    }
    if (pageRaster) {
      totalBytes += pageRaster.bytes.length;
      if (totalBytes > 32 * 1024 * 1024) throw new Error("SCENE_ASSET_LIMIT");
      await writeFile(join(runtime.outputDir, pageRaster.file), pageRaster.bytes);
      assets.push({
        id: pageRaster.id,
        mediaType: "image/png",
        sha256: hash(pageRaster.bytes),
        byteLength: pageRaster.bytes.length,
        width: pageRaster.pixelWidth,
        height: pageRaster.pixelHeight,
        file: pageRaster.file,
        coveredNodeIds: ["main_page_raster_image"],
        bakedEffects: [
          ...new Set(rasterizedDiagnostics.map((d) => d.code)),
          "full-page-composite",
        ],
      });
    }
    const fontMetrics = await page.evaluate((fonts) => {
      const ctx = document.createElement("canvas").getContext("2d")!;
      return fonts.map((font) => {
        ctx.font = font;
        const m = ctx.measureText("Lumina 中文 0123456789");
        return [
          font,
          m.width,
          m.fontBoundingBoxAscent,
          m.fontBoundingBoxDescent,
        ];
      });
    }, data.fonts);
    const scene: SceneIR = {
      schemaVersion: isDesign ? DESIGN_SCENE_VERSION : SCENE_VERSION,
      source: {
        htmlHash: input.htmlHash,
        cssHash: input.cssHash,
        converterVersion,
      },
      environment: {
        chromium: runtime.browser.version(),
        platform: platform(),
        locale: "zh-CN",
        timezone: "Asia/Shanghai",
        dpr: 1,
        fontSignature: hash(canonical(fontMetrics)),
      },
      page: { width, height, background: "white" },
      nodes: data.nodes,
      paintOps: data.paintOps,
      assets,
      textInventory: data.textInventory,
      diagnostics: data.diagnostics,
    };
    assertScene(scene);
    return scene;
  } finally {
    signal?.removeEventListener("abort", abort);
    await context.close().catch(() => {});
  }
}
