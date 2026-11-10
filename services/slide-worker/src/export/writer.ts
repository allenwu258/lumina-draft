import { createRequire } from "node:module";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { Browser } from "playwright-core";
import type { SceneIR, Rect } from "@lumina/scene-core";
import { planPptxPage, inch, point, type PagePlan } from "./plan.js";
import { color } from "./color.js";
import { rasterRegions, readSceneAssets } from "./raster.js";

export type ExportInput = {
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
  const PptxGenJS = createRequire(import.meta.url)(
    "pptxgenjs",
  ) as new () => import("pptxgenjs").default;
  const pptx = new PptxGenJS();
  pptx.defineLayout({ name: "SCENE_16_9", width: 800 / 60, height: 450 / 60 });
  pptx.layout = "SCENE_16_9";
  pptx.title = input.title;
  pptx.subject = "SceneIR export";
  pptx.author = "Lumina Draft";
  pptx.company = "Lumina Draft";
  const reports: {
      sceneId: string;
      nativeTexts: string[];
      coveredNodeIds: string[];
      rasterNodeIds: string[];
      invisibleNodeIds: string[];
      rasterRegions: number;
      fonts: string[];
    }[] = [],
    warnings: string[] = [];
  for (const [index, page] of input.pages.entries()) {
    try {
      if (signal?.aborted) throw new Error("EXPORT_ABORTED");
      const plan: PagePlan = planPptxPage(page.scene);
      const assets = await readSceneAssets(page.scene, page.assets, within);
      const regions = plan.elements.flatMap((e) =>
        e.kind === "raster" ? [e.region] : [],
      );
      if (regions.length && !browser)
        throw new Error("RASTER_BROWSER_REQUIRED");
      if (
        regions.reduce(
          (sum, r) => sum + r.frame.width * r.frame.height * 9,
          0,
        ) > 32_000_000
      )
        throw new Error("RASTER_PAGE_PIXEL_LIMIT");
      const rasters = regions.length
        ? await rasterRegions(browser!, page.scene, assets, regions, signal)
        : [];
      const slide = pptx.addSlide();
      slide.background = color(page.scene.page.background);
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
        if (e.kind === "image") {
          slide.addImage({
            data: "image/png;base64," + assets[e.node.assetId],
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
            bold: Number(e.style.fontWeight) >= 600,
            italic: e.style.fontStyle === "italic",
            ...color(e.style.color),
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
        slide.addShape(
          radius ? pptx.ShapeType.roundRect : pptx.ShapeType.rect,
          {
            ...geometry(frame),
            rectRadius: inch(Math.max(0, radius - borderWidth / 2)),
            fill: color(n.fill),
            line:
              uniform && b.width && b.style !== "none" && b.style !== "hidden"
                ? { ...color(b.color), width: point(b.width) }
                : { color: "000000", transparency: 100 },
            objectName: `shape:${n.id}`,
          },
        );
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
              slide.addShape(pptx.ShapeType.rect, {
                ...geometry(frames[i]),
                fill: color(border.color),
                line: { transparency: 100 },
                objectName: `border:${n.id}:${i}`,
              });
          });
        }
      }
      warnings.push(...plan.warnings.map((w) => `第 ${index + 1} 页：${w}`));
      reports.push({
        sceneId: page.sceneId,
        nativeTexts,
        coveredNodeIds: plan.coveredNodeIds,
        rasterNodeIds: regions.flatMap((r) => r.nodeIds),
        invisibleNodeIds: plan.invisibleNodeIds,
        rasterRegions: regions.length,
        fonts: [...fonts],
      });
    } catch (error) {
      throw Object.assign(
        new Error(`第 ${index + 1} 页导出失败：${String(error)}`),
        { pageNumber: index + 1 },
      );
    }
  }
  await pptx.writeFile({
    fileName: join(output, "presentation.pptx"),
    compression: true,
  });
  if (signal?.aborted) throw new Error("EXPORT_ABORTED");
  const report = {
    inputHash: input.inputHash,
    pages: reports,
    warnings: [...new Set(warnings)],
  };
  await writeFile(join(output, "report.json"), JSON.stringify(report));
  return report;
}
