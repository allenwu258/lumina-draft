import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { platform } from "node:os";
import type { Browser } from "playwright-core";
import {
  assertScene,
  CONVERTER_VERSION,
  SCENE_VERSION,
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
export async function convert(
  input: { html: string; htmlHash: string; cssHash: string },
  runtime: { browser: Browser; outputDir: string },
  signal?: AbortSignal,
): Promise<SceneIR> {
  const context = await runtime.browser.newContext({
    viewport: { width: 800, height: 450 },
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
    const data = await page.evaluate(extractDOM);
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
      schemaVersion: SCENE_VERSION,
      source: {
        htmlHash: input.htmlHash,
        cssHash: input.cssHash,
        converterVersion: CONVERTER_VERSION,
      },
      environment: {
        chromium: runtime.browser.version(),
        platform: platform(),
        locale: "zh-CN",
        timezone: "Asia/Shanghai",
        dpr: 1,
        fontSignature: hash(canonical(fontMetrics)),
      },
      page: { width: 800, height: 450, background: "white" },
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
