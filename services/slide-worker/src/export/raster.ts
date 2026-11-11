import { readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import type { Browser } from "playwright-core";
import type { SceneIR } from "@lumina/scene-core";
import type { RasterRegion } from "./plan.js";

export async function rasterRegions(
  browser: Browser,
  scene: SceneIR,
  assets: Record<string, string>,
  regions: RasterRegion[],
  signal?: AbortSignal,
): Promise<string[]> {
  if (!regions.length) return [];
  const context = await browser.newContext({
    viewport: { width: 800, height: 450 },
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    deviceScaleFactor: 1,
  });
  const abort = () => void context.close();
  signal?.addEventListener("abort", abort, { once: true });
  try {
    await context.route("**/*", (route) => route.abort());
    const page = await context.newPage();
    await page.setContent(
      '<!doctype html><html><body><canvas id="scene"></canvas></body></html>',
    );
    await page.addScriptTag({
      content: await readFile(
        new URL("./renderer.browser.js", import.meta.url),
        "utf8",
      ),
    });
    return await page.evaluate(
      async ({ scene, assets, regions }) => {
        const library = (
          window as unknown as {
            LuminaScene: {
              decodeAsset: typeof import("@lumina/scene-renderer").decodeAsset;
              SceneRenderer: new (
                c: HTMLCanvasElement,
              ) => import("@lumina/scene-renderer").SceneRenderer;
            };
          }
        ).LuminaScene;
        const renderer = new library.SceneRenderer(
          document.querySelector("canvas")!,
        );
        try {
          await renderer.load(
            scene,
            async (a) => {
              const bytes = Uint8Array.from(atob(assets[a.id]), (c) =>
                c.charCodeAt(0),
              );
              return library.decodeAsset(bytes, a.mediaType);
            },
            async (s, text) => {
              const font = `${s.fontStyle} ${s.fontWeight} ${s.fontSize}px ${s.fontFamily}`;
              await document.fonts.load(font, text);
              if (!document.fonts.check(font, text))
                throw new Error("RASTER_FONT_UNAVAILABLE");
            },
          );
          return regions.map((r) => {
            const canvas = renderer.renderRegion(
              r.frame,
              r.nodeIds,
              3,
              r.shadowOnly,
              r.fragmentKeys,
            );
            const data = canvas.toDataURL("image/png");
            canvas.width = canvas.height = 1;
            return data;
          });
        } finally {
          renderer.dispose();
        }
      },
      { scene, assets, regions },
    );
  } finally {
    signal?.removeEventListener("abort", abort);
    await context.close();
  }
}

export async function readSceneAssets(
  scene: SceneIR,
  paths: Record<string, string>,
  within: (path: string) => string,
): Promise<Record<string, string>> {
  const assets: Record<string, string> = {};
  for (const a of scene.assets) {
    const raw = await readFile(within(paths[a.id]));
    if (
      raw.length !== a.byteLength ||
      createHash("sha256").update(raw).digest("hex") !== a.sha256
    )
      throw new Error("EXPORT_ASSET_HASH");
    assets[a.id] = raw.toString("base64");
  }
  return assets;
}
