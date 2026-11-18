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
// Unsupported effects are reported by extractDOM and retained as native
// content wherever possible. The converter no longer turns a whole page into
// a bitmap merely because one local effect, transform or border is approximate.
// A page raster remains an explicit last-resort path for future capabilities,
// but no current diagnostic opts into it automatically.
const PAGE_RASTER_FALLBACK_CODES = new Set<string>();
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
      let blockedBackgroundCount = 0;
      let blockedSvgReferenceCount = 0;
      let blockedCssReferenceCount = 0;
      const splitLayers = (value: string) => {
        const layers: string[] = [];
        let start = 0,
          depth = 0,
          quote = "",
          escaped = false;
        for (let index = 0; index < value.length; index++) {
          const char = value[index];
          if (escaped) escaped = false;
          else if (char === "\\") escaped = true;
          else if (quote) {
            if (char === quote) quote = "";
          } else if (char === "'" || char === '"') quote = char;
          else if (char === "(") depth++;
          else if (char === ")") depth = Math.max(0, depth - 1);
          else if (char === "," && depth === 0) {
            layers.push(value.slice(start, index).trim());
            start = index + 1;
          }
        }
        layers.push(value.slice(start).trim());
        return layers.filter(Boolean);
      };
      const sanitizeBackground = (declaration: CSSStyleDeclaration) => {
        const shorthand = declaration.getPropertyValue("background");
        const image =
          declaration.getPropertyValue("background-image") ||
          (shorthand
            ? (() => {
                const probe = parsed.createElement("span").style;
                probe.setProperty("background", shorthand);
                return probe.getPropertyValue("background-image");
              })()
            : "");
        if (!image || !/url\(/i.test(image)) return;
        const layers = splitLayers(image);
        const safeLayers = layers.filter((layer) => {
          const urls = [...layer.matchAll(/url\(\s*(['"]?)(.*?)\1\s*\)/gi)];
          const external = urls.some(
            (match) => !/^data:image\//i.test(match[2].trim()),
          );
          if (external) blockedBackgroundCount++;
          return !external;
        });
        if (safeLayers.length !== layers.length)
          declaration.setProperty(
            "background-image",
            safeLayers.length ? safeLayers.join(", ") : "none",
            "important",
          );
      };
      const sanitizeCssUrls = (declaration: CSSStyleDeclaration) => {
        sanitizeBackground(declaration);
        for (const property of Array.from({ length: declaration.length }, (_, index) => declaration.item(index))) {
          if (property === "background" || property === "background-image") continue;
          const value = declaration.getPropertyValue(property);
          const sanitized = value.replace(
            /url\(\s*(['"]?)(.*?)\1\s*\)/gi,
            (reference, _quote, rawTarget) => {
              const target = String(rawTarget).trim();
              if (target.startsWith("#") || /^data:image\//i.test(target))
                return reference;
              blockedCssReferenceCount++;
              return "none";
            },
          );
          if (sanitized !== value)
            declaration.setProperty(property, sanitized, "important");
        }
      };
      const visitRules = (rules: CSSRuleList) => {
        for (const rule of rules) {
          if ("style" in rule && rule.style instanceof CSSStyleDeclaration)
            sanitizeCssUrls(rule.style);
          if ("cssRules" in rule && rule.cssRules)
            visitRules(rule.cssRules as CSSRuleList);
        }
      };
      for (const element of [parsed.documentElement, parsed.body, ...parsed.querySelectorAll("*")]) {
        sanitizeCssUrls((element as HTMLElement).style);
        if (element.namespaceURI === "http://www.w3.org/2000/svg")
          for (const attribute of [...element.attributes]) {
            if (/^(?:xlink:)?href$/i.test(attribute.name)) {
              if (
                !attribute.value.startsWith("#") &&
                !/^data:image\//i.test(attribute.value)
              ) {
                element.removeAttribute(attribute.name);
                blockedSvgReferenceCount++;
              }
              continue;
            }
            if (!/url\(/i.test(attribute.value)) continue;
            const sanitized = attribute.value.replace(
              /url\(\s*(['"]?)(.*?)\1\s*\)/gi,
              (reference, _quote, rawTarget) => {
                const target = String(rawTarget).trim();
                if (target.startsWith("#") || /^data:image\//i.test(target))
                  return reference;
                blockedSvgReferenceCount++;
                return "none";
              },
            );
            if (sanitized !== attribute.value)
              element.setAttribute(attribute.name, sanitized);
          }
      }
      for (const style of parsed.querySelectorAll("style"))
        try {
          if (style.sheet) visitRules(style.sheet.cssRules);
        } catch {
          blockedBackgroundCount++;
        }
      if (blockedBackgroundCount)
        parsed.documentElement.setAttribute(
          "data-lumina-blocked-background-count",
          String(blockedBackgroundCount),
        );
      if (blockedSvgReferenceCount)
        parsed.documentElement.setAttribute(
          "data-lumina-blocked-svg-reference-count",
          String(blockedSvgReferenceCount),
        );
      if (blockedCssReferenceCount)
        parsed.documentElement.setAttribute(
          "data-lumina-blocked-css-reference-count",
          String(blockedCssReferenceCount),
        );
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
    const backgroundAssets: {
      id: string;
      nodeId: string;
      groupId?: string;
      captureId?: string;
      bytes: Buffer;
      width: number;
      height: number;
      bakedEffect: string;
      fallbackFill?: string;
    }[] = [];
    const pngSize = (bytes: Buffer, expected?: CanvasSize) => {
      if (
        bytes.length < 24 ||
        !bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))
      )
        throw new Error("BACKGROUND_PNG_INVALID");
      const imageWidth = bytes.readUInt32BE(16);
      const imageHeight = bytes.readUInt32BE(20);
      if (
        imageWidth <= 0 ||
        imageHeight <= 0 ||
        imageWidth > 8192 ||
        imageHeight > 8192 ||
        imageWidth * imageHeight > 8_000_000 ||
        (expected && (imageWidth !== expected.width || imageHeight !== expected.height))
      )
        throw new Error("BACKGROUND_PNG_DIMENSIONS");
      return { width: imageWidth, height: imageHeight };
    };
    const markBackgroundFailure = (captureId?: string) => {
      const diagnostic = data.diagnostics.find(
        (item) =>
          item.code === "CSS_BACKGROUND_IMAGE_LOCAL_ASSET" &&
          item.details?.captureId === captureId,
      );
      if (diagnostic) {
        diagnostic.code = "CSS_BACKGROUND_CAPTURE_FAILED";
        diagnostic.message = "The local CSS background image could not be captured; its solid fallback and other page content were retained.";
        diagnostic.severity = "warning";
        if (diagnostic.details) delete diagnostic.details.captureId;
      }
      if (!captureId)
        for (const item of data.diagnostics)
          if (item.code === "CSS_PAGE_BACKGROUND_LOCAL_ASSET") {
            item.code = "CSS_BACKGROUND_CAPTURE_FAILED";
            item.message = "The document background image could not be captured; its solid fallback and page content were retained.";
            item.severity = "warning";
          }
    };
    const removeBackgroundNode = (
      nodeId: string,
      groupId?: string,
      fallbackFill?: string,
    ) => {
      const removed = new Set([nodeId, ...(groupId ? [groupId] : [])]);
      data.nodes = data.nodes.filter((node) => !removed.has(node.id));
      data.paintOps = data.paintOps.filter((op) => !removed.has(op.nodeId));
      data.paintGroups = data.paintGroups.filter((group) => !removed.has(group.id));
      if (fallbackFill && !/^transparent$|^rgba\(0, 0, 0, 0\)$/.test(fallbackFill)) {
        const shape = data.nodes.find(
          (node) => node.kind === "shape" && node.sourcePath === data.backgrounds.find((item) => item.nodeId === nodeId)?.sourcePath,
        );
        if (shape?.kind === "shape") shape.fill = fallbackFill;
      }
    };
    if (!pageRaster && data.pageBackgroundCapture) {
      try {
        const screenshot = await page.screenshot({
          type: "png",
          omitBackground: true,
          animations: "disabled",
          timeout: 5000,
          style: "html,body{visibility:visible!important;background-color:transparent!important;-webkit-text-fill-color:transparent!important;text-shadow:none!important}html *,body *{visibility:hidden!important}html::before,html::after,body::before,body::after{content:none!important}",
        });
        const size = pngSize(screenshot, { width, height });
        if (screenshot.length > 8 * 1024 * 1024)
          throw new Error("BACKGROUND_PNG_SIZE");
        const nodeId = "page_background_image";
        const groupId = "page_background_group";
        const assetId = "page_background_asset";
        const frame = { x: 0, y: 0, width, height };
        data.nodes.unshift(
          {
            id: groupId,
            sourcePath: "main",
            kind: "group",
            tagName: "background",
            frame,
            opacity: 1,
            clip: null,
          },
          { id: nodeId, sourcePath: "main", kind: "image", frame, assetId },
        );
        data.paintOps.unshift(
          { op: "begin", nodeId: groupId },
          { op: "draw", nodeId },
          { op: "end", nodeId: groupId },
        );
        data.paintGroups.unshift({
          id: groupId,
          context: false,
          positioned: false,
          z: 0,
        });
        backgroundAssets.push({
          id: assetId,
          nodeId,
          groupId,
          bytes: screenshot,
          width: size.width,
          height: size.height,
          bakedEffect: "css-page-background",
        });
      } catch {
        markBackgroundFailure();
      }
    }
    if (!pageRaster) {
      for (const background of data.backgrounds) {
        try {
          const selector = `[data-lumina-background-capture="${background.captureId}"]`;
          const screenshot = await page.locator(selector).screenshot({
            type: "png",
            scale: "css",
            omitBackground: true,
            animations: "disabled",
            timeout: 5000,
            style: `html,html *{opacity:1!important}html,body{background:transparent!important}*{visibility:hidden!important}${selector}{visibility:visible!important;background-color:transparent!important;background-clip:padding-box!important;border-color:transparent!important;box-shadow:none!important;outline:none!important;text-shadow:none!important;-webkit-text-fill-color:transparent!important;-webkit-text-stroke-color:transparent!important;text-decoration-color:transparent!important}${selector} *{visibility:hidden!important}${selector}::before,${selector}::after{content:none!important}`,
          });
          const size = pngSize(screenshot);
          if (screenshot.length > 4 * 1024 * 1024)
            throw new Error("BACKGROUND_PNG_SIZE");
          backgroundAssets.push({
            id: background.assetId,
            nodeId: background.nodeId,
            captureId: background.captureId,
            fallbackFill: background.fallbackFill,
            bytes: screenshot,
            width: size.width,
            height: size.height,
            bakedEffect: "css-background-image",
          });
        } catch {
          removeBackgroundNode(background.nodeId, undefined, background.fallbackFill);
          markBackgroundFailure(background.captureId);
        }
      }
      await page
        .locator("[data-lumina-background-capture]")
        .evaluateAll((elements) =>
          elements.forEach((element) =>
            element.removeAttribute("data-lumina-background-capture"),
          ),
        );
    }
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
    let backgroundBytes = 0;
    for (const background of backgroundAssets) {
      const maxAssetBytes = background.groupId ? 8 : 4;
      if (
        background.bytes.length > maxAssetBytes * 1024 * 1024 ||
        backgroundBytes + background.bytes.length > 12 * 1024 * 1024 ||
        totalBytes + background.bytes.length > 32 * 1024 * 1024
      ) {
        removeBackgroundNode(background.nodeId, background.groupId, background.fallbackFill);
        markBackgroundFailure(background.captureId);
        continue;
      }
      const file = `${background.id}.png`;
      await writeFile(join(runtime.outputDir, file), background.bytes);
      totalBytes += background.bytes.length;
      backgroundBytes += background.bytes.length;
      assets.push({
        id: background.id,
        mediaType: "image/png",
        sha256: hash(background.bytes),
        byteLength: background.bytes.length,
        width: background.width,
        height: background.height,
        file,
        coveredNodeIds: [background.nodeId],
        bakedEffects: [background.bakedEffect],
      });
    }
    if (!pageRaster)
      data.paintOps = orderPaint(data.nodes, data.paintOps, data.paintGroups);
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
      page: { width, height, background: data.pageBackground },
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
