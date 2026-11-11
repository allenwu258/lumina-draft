import { createRequire } from "node:module";
import { writeFile, readFile } from "node:fs/promises";
import { join, dirname } from "node:path";
import type { Browser } from "playwright-core";
import { textTarget, type SceneIR, type Rect } from "@lumina/scene-core";
import { planPptxPage, inch, point, type PagePlan } from "./plan.js";
import { sceneToSvg } from "./svg.js";
import { hash } from "../capture.js";
import { serializePresentation, type SlidePacket } from "./serialize.js";
import { color } from "./color.js";
import { rasterRegions, readSceneAssets } from "./raster.js";

export type ExportInput = {
  warnings?: string[];
  title: string;
  inputHash: string;
  pages: { scene: SceneIR; assets: Record<string, string>; sceneId: string }[];
};
const geometry = (r: Rect) => ({
  x: inch(r.x),
  y: inch(r.y),
  w: inch(Math.max(r.width, 0.05)),
  h: inch(Math.max(r.height, 0.05)),
});

/** Standard PptxGenJS APIs only; no OOXML replacement or HTML access. */
export async function writePptx(
  input: ExportInput,
  output: string,
  within: (path: string) => string,
  browser?: Browser,
  signal?: AbortSignal,
) {
  if (!input.pages.length || input.pages.length > 20)
    throw new Error("INVALID_EXPORT_PAGES");
  if (!browser) throw new Error("EXPORT_BROWSER_REQUIRED");
  const slides: SlidePacket[] = [];
  const alpha = (value: ReturnType<typeof color>, opacity: number) => ({
    ...value,
    transparency: 100 - (100 - value.transparency) * opacity,
  });
  const reports: {
      sceneId: string;
      nativeTexts: string[];
      nativeFragments: { nodeId: string; fragmentKey: string; text: string }[];
      coveredNodeIds: string[];
      rasterNodeIds: string[];
      invisibleNodeIds: string[];
      rasterRegions: number;
      fonts: string[];
      svgRegions: {
        nodeIds: string[];
        frame: Rect;
        reasonCode: string;
        sha256: string;
        objectName: string;
      }[];
      rasterDetails: import("./plan.js").RasterRegion[];
      assetTextCount: number;
      vectorTextCount: number;
      rasterAssetTextCount: number;
      rasterTextCount: number;
      compatibilityPngCount: number;
    }[] = [],
    warnings: string[] = [...(input.warnings || [])];
  for (const [index, page] of input.pages.entries()) {
    try {
      if (signal?.aborted) throw new Error("EXPORT_ABORTED");
      const plan: PagePlan = planPptxPage(page.scene);
      const assets = await readSceneAssets(page.scene, page.assets, within);
      const regions = plan.elements.flatMap((e) =>
        e.kind === "raster" ? [e.region] : [],
      );
      if (
        regions.reduce(
          (sum, r) => sum + r.frame.width * r.frame.height * 9,
          0,
        ) > 32_000_000
      )
        throw new Error("RASTER_PAGE_PIXEL_LIMIT");
      const rasters = regions.length
        ? await rasterRegions(browser, page.scene, assets, regions, signal)
        : [];
      const packet: SlidePacket = {
        background: color(page.scene.page.background),
        objects: [],
      };
      const slide = {
        addImage: (options: import("pptxgenjs").default.ImageProps) =>
          packet.objects.push({ kind: "image", options }),
        addText: (
          text: string,
          options: import("pptxgenjs").default.TextPropsOptions,
        ) => packet.objects.push({ kind: "text", text, options }),
        addShape: (
          shape: "rect" | "roundRect",
          options: import("pptxgenjs").default.ShapeProps,
        ) => packet.objects.push({ kind: "shape", shape, options }),
      };
      slides.push(packet);
      const svgRegions: (typeof reports)[number]["svgRegions"] = [];
      const nativeFragments: (typeof reports)[number]["nativeFragments"] = [];
      const nativeTexts: string[] = [],
        fonts = new Set<string>();
      let ri = 0;
      for (const e of plan.elements) {
        if (e.kind === "raster") {
          slide.addImage({
            data: rasters[ri++],
            ...geometry(e.region.frame),
            objectName: `raster:${e.region.nodeIds.join(",")}`,
          });
          continue;
        }
        if (e.kind === "svg") {
          const svg = sceneToSvg(page.scene, e.region.nodeIds, e.region.frame);
          const objectName = `vector:${svgRegions.length}`;
          slide.addImage({
            data:
              "data:image/svg+xml;base64," +
              Buffer.from(svg).toString("base64"),
            ...geometry(e.region.frame),
            objectName,
          });
          svgRegions.push({ ...e.region, sha256: hash(svg), objectName });
          continue;
        }
        if (e.kind === "image") {
          const asset = page.scene.assets.find((a) => a.id === e.node.assetId)!;
          const objectName = `image:${e.node.id}`;
          if (asset.mediaType === "image/svg+xml")
            svgRegions.push({
              nodeIds: [e.node.id],
              frame: e.node.frame,
              reasonCode: "SOURCE_SVG",
              sha256: asset.sha256,
              objectName,
            });
          slide.addImage({
            data:
              "data:" + asset.mediaType + ";base64," + assets[e.node.assetId],
            transparency: 100 * (1 - e.opacity),
            ...geometry(e.node.frame),
            objectName: `image:${e.node.id}`,
          });
          continue;
        }
        if (e.kind === "text") {
          const size = e.style.fontSize,
            ascent = (size * e.font.ascent) / e.font.unitsPerEm;
          const frame = {
            x: e.frame.x,
            y: e.baseline - ascent,
            width: Math.max(e.frame.width, e.font.width) + 0.5,
            height: Math.max(e.frame.height, size * 1.5),
          };
          slide.addText(e.text, {
            ...geometry(frame),
            fontFace: e.font.family,
            fontSize: point(size),
            bold: e.font.bold,
            italic: e.style.fontStyle === "italic",
            ...alpha(color(e.style.color), e.opacity),
            charSpacing: point(e.style.letterSpacing),
            underline: e.style.decoration.includes("underline")
              ? { style: "sng" }
              : undefined,
            strike: e.style.decoration.includes("line-through")
              ? "sngStrike"
              : undefined,
            margin: 0,
            fit: "none",
            wrap: false,
            align: "left",
            valign: "top",
            paraSpaceAfter: 0,
            paraSpaceBefore: 0,
            lineSpacingMultiple: 1,
            lang: "zh-CN",
            objectName: `text:${e.nodeId}`,
          });
          nativeTexts.push(e.text);
          nativeFragments.push({
            nodeId: e.nodeId,
            fragmentKey: e.fragmentKey,
            text: e.text,
          });
          fonts.add(e.font.family);
          continue;
        }
        const n = e.node,
          b = n.borders[0],
          uniform = n.borders.every(
            (x) =>
              x.width === b.width && x.color === b.color && x.style === b.style,
          ),
          radius = n.radii[0];
        const borderWidth =
          uniform && b.style !== "none" && b.style !== "hidden" ? b.width : 0;
        const frame = {
          x: n.frame.x + borderWidth / 2,
          y: n.frame.y + borderWidth / 2,
          width: n.frame.width - borderWidth,
          height: n.frame.height - borderWidth,
        };
        slide.addShape(radius ? "roundRect" : "rect", {
          ...geometry(frame),
          rectRadius: inch(Math.max(0, radius - borderWidth / 2)),
          fill: alpha(color(n.fill), e.opacity),
          line:
            uniform && b.width && b.style !== "none" && b.style !== "hidden"
              ? { ...alpha(color(b.color), e.opacity), width: point(b.width) }
              : { color: "000000", transparency: 100 },
          objectName: `shape:${n.id}`,
        });
        if (!uniform) {
          const f = n.frame,
            [t, r, bot, l] = n.borders;
          const frames = [
            { x: f.x, y: f.y, width: f.width, height: t.width },
            {
              x: f.x + f.width - r.width,
              y: f.y,
              width: r.width,
              height: f.height,
            },
            {
              x: f.x,
              y: f.y + f.height - bot.width,
              width: f.width,
              height: bot.width,
            },
            { x: f.x, y: f.y, width: l.width, height: f.height },
          ];
          n.borders.forEach((border, i) => {
            if (
              border.width &&
              border.style !== "none" &&
              border.style !== "hidden"
            )
              slide.addShape("rect", {
                ...geometry(frames[i]),
                fill: alpha(color(border.color), e.opacity),
                line: { transparency: 100 },
                objectName: `border:${n.id}:${i}`,
              });
          });
        }
      }
      warnings.push(...plan.warnings.map((w) => `第 ${index + 1} 页：${w}`));
      const assetText = page.scene.textInventory
        .filter((t) => t.sourceText.trim())
        .map((t) => textTarget(page.scene, t))
        .filter((t) => t.kind === "asset");
      const vectorIds = new Set(svgRegions.flatMap((r) => r.nodeIds));
      const visibleAssetText = assetText.filter(
        (t) => !plan.invisibleNodeIds.includes(t.nodeId),
      );
      const vectorTextCount = visibleAssetText.filter((t) =>
        vectorIds.has(t.nodeId),
      ).length;
      reports.push({
        sceneId: page.sceneId,
        nativeTexts,
        nativeFragments,
        coveredNodeIds: plan.coveredNodeIds,
        rasterNodeIds: [
          ...new Set(
            regions.filter((r) => !r.shadowOnly).flatMap((r) => r.nodeIds),
          ),
        ],
        invisibleNodeIds: plan.invisibleNodeIds,
        rasterRegions: regions.length,
        fonts: [...fonts],
        svgRegions,
        rasterDetails: regions,
        assetTextCount: assetText.length,
        vectorTextCount,
        rasterAssetTextCount: visibleAssetText.length - vectorTextCount,
        rasterTextCount: page.scene.nodes.filter(
          (n) =>
            n.kind === "text" && regions.some((r) => r.nodeIds.includes(n.id)),
        ).length,
        compatibilityPngCount: svgRegions.length,
      });
      const assetTextCount = reports.at(-1)!.assetTextCount;
      if (assetTextCount)
        warnings.push(
          `第 ${index + 1} 页：${assetTextCount} 段文字保留在图形资产中，无法作为独立文本框编辑`,
        );
    } catch (error) {
      throw Object.assign(
        new Error(`第 ${index + 1} 页导出失败：${String(error)}`),
        { pageNumber: index + 1 },
      );
    }
  }
  const context = await browser.newContext({ serviceWorkers: "block" });
  const abort = () => void context.close();
  signal?.addEventListener("abort", abort, { once: true });
  try {
    if (signal?.aborted) throw new Error("EXPORT_ABORTED");
    await context.route("**/*", (route) => route.abort());
    const page = await context.newPage();
    await page.setContent(
      "<!doctype html><html><head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline'; img-src data: blob:; style-src 'unsafe-inline'\"></head><body></body></html>",
    );
    const bundle = join(
      dirname(createRequire(import.meta.url).resolve("pptxgenjs")),
      "pptxgen.bundle.js",
    );
    await page.addScriptTag({ content: await readFile(bundle, "utf8") });
    const encoded = await page.evaluate(serializePresentation, {
      title: input.title,
      slides,
    });
    const raw = Buffer.from(encoded, "base64");
    if (raw.length > 128 * 1024 * 1024) throw new Error("PPTX_OUTPUT_LIMIT");
    await writeFile(join(output, "presentation.pptx"), raw);
  } finally {
    signal?.removeEventListener("abort", abort);
    await context.close().catch(() => {});
  }
  if (signal?.aborted) throw new Error("EXPORT_ABORTED");
  const report = {
    inputHash: input.inputHash,
    pages: reports,
    warnings: [...new Set(warnings)],
  };
  await writeFile(join(output, "report.json"), JSON.stringify(report));
  return report;
}
