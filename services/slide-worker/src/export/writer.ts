import { createRequire } from "node:module";
import { writeFile, readFile } from "node:fs/promises";
import { join, dirname } from "node:path";
import type { Browser, Page } from "playwright-core";
import { textTarget, type SceneIR, type Rect, type TextAlign, type WorkTextLayout } from "@lumina/scene-core";
import {
  planPptxPage,
  inch,
  point,
  type Element,
  type PagePlan,
} from "./plan.js";
import { sceneToSvg } from "./svg.js";
import { hash } from "../capture.js";
import { serializePresentation, type SlidePacket } from "./serialize.js";
import { color } from "./color.js";
import { rasterRegions, readSceneAssets } from "./raster.js";
import { FONT_MAP_VERSION, measureSemiboldRuns } from "./fonts.js";

export type ExportInput = {
  warnings?: string[];
  title: string;
  inputHash: string;
  pages: {
    scene: SceneIR;
    editor?: { text?: Record<string, Partial<WorkTextLayout>> };
    assets: Record<string, string>;
    sceneId: string;
  }[];
};
const geometry = (r: Rect) => ({
  x: inch(r.x),
  y: inch(r.y),
  w: inch(Math.max(r.width, 0.05)),
  h: inch(Math.max(r.height, 0.05)),
});
type NativeTextElement = Extract<Element, { kind: "text" }>;

function mergeableTextGroup(
  elements: Element[],
  start: number,
  nodeHasOneParagraph: boolean,
  nodeHasRasterText: boolean,
  alignment: TextAlign,
  wholeBox: boolean,
): NativeTextElement[] | undefined {
  const first = elements[start];
  if (first?.kind !== "text" || !nodeHasOneParagraph || nodeHasRasterText)
    return;

  const group: NativeTextElement[] = [];
  for (let i = start; i < elements.length; i++) {
    const element = elements[i];
    if (element.kind !== "text" || element.nodeId !== first.nodeId) break;
    group.push(element);
  }
  const lines = new Map<number, NativeTextElement[]>();
  for (const element of group) {
    const line = lines.get(element.lineIndex) || [];
    line.push(element);
    lines.set(element.lineIndex, line);
  }
  const lineIndices = [...lines.keys()];
  if (lineIndices.length < 2 && (!wholeBox || group.length < 2)) return;

  const firstFont = group[0];
  const firstAscent =
    (firstFont.style.fontSize * firstFont.font.ascent) /
    firstFont.font.unitsPerEm;
  if (
    group.some((element) => {
      const ascent =
        (element.style.fontSize * element.font.ascent) /
        element.font.unitsPerEm;
      return (
        element.style.fontSize !== firstFont.style.fontSize ||
        Math.abs(ascent - firstAscent) > 0.5
      );
    })
  )
    return;

  const lineGroups = lineIndices.map((index) => lines.get(index)!);
  const startX = lineGroups[0][0].frame.x;
  const baselineStep = lineGroups.length > 1
    ? lineGroups[1][0].baseline - lineGroups[0][0].baseline
    : 0;
  if (lineGroups.length > 1 && baselineStep <= 0) return;

  for (let lineIndex = 0; lineIndex < lineGroups.length; lineIndex++) {
    const line = lineGroups[lineIndex];
    if (
      lineIndices[lineIndex] !== lineIndices[0] + lineIndex ||
      (alignment === "left" && Math.abs(line[0].frame.x - startX) > 0.75) ||
      Math.abs(
        line[0].baseline - lineGroups[0][0].baseline - baselineStep * lineIndex,
      ) > 0.75
    )
      return;
    for (let fragmentIndex = 1; fragmentIndex < line.length; fragmentIndex++) {
      const previous = line[fragmentIndex - 1];
      const current = line[fragmentIndex];
      if (
        Math.abs(current.frame.x - previous.frame.x - previous.frame.width) >
        0.75
      )
        return;
    }
  }
  return group;
}

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
  const context = await browser.newContext({
    serviceWorkers: "block",
    locale: "zh-CN",
  });
  const abort = () => void context.close();
  signal?.addEventListener("abort", abort, { once: true });
  try {
    if (signal?.aborted) throw new Error("EXPORT_ABORTED");
    await context.route("**/*", (route) => route.abort());
    const page = await context.newPage();
    await page.setContent(
      "<!doctype html><html><head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline'; img-src data: blob:; style-src 'unsafe-inline'\"></head><body></body></html>",
    );
    return await writePptxWithPage(input, output, within, browser, page, signal);
  } finally {
    signal?.removeEventListener("abort", abort);
    await context.close().catch(() => {});
  }
}

async function writePptxWithPage(
  input: ExportInput,
  output: string,
  within: (path: string) => string,
  browser: Browser,
  exportPage: Page,
  signal?: AbortSignal,
) {
  const slides: SlidePacket[] = [];
  const alpha = (value: ReturnType<typeof color>, opacity: number) => ({
    ...value,
    transparency: 100 - (100 - value.transparency) * opacity,
  });
  const characters = (value: string) => [...value].length;
  const reports: {
      sceneId: string;
      nativeTexts: string[];
      nativeTextBoxes: {
        nodeId: string;
        text: string;
        paragraphCount: number;
        softBreakCount: number;
      }[];
      nativeFragments: {
        nodeId: string;
        fragmentKey: string;
        text: string;
        baseline: number;
        requestedFontFamily: string;
        fontFamily: string;
        postscriptName: string;
        fontFileSha256: string;
        fontSize: number;
        letterSpacing: number;
        requestedWeight: number;
        faceWeight: number;
        bold: boolean;
        italic: boolean;
        ascent: number;
        descent: number;
        unitsPerEm: number;
        measuredWidth: number;
      }[];
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
      fontMapVersion: string;
      fontFaces: {
        family: string;
        postscriptName: string;
        fontFileSha256: string;
        requestedWeight: number;
        faceWeight: number;
        italic: boolean;
        uses: number;
      }[];
      nativeTextCharacterCount: number;
      svgTextCharacterCount: number;
      rasterTextCharacterCount: number;
      nativeShapeCount: number;
      nativeImageCount: number;
      sourceSvgCount: number;
      generatedVectorRegionCount: number;
      localRasterRegionCount: number;
      fullPageRasterCount: number;
    }[] = [],
    warnings: string[] = [...(input.warnings || [])];
  for (const [index, page] of input.pages.entries()) {
    try {
      if (signal?.aborted) throw new Error("EXPORT_ABORTED");
      const semiboldRuns = await measureSemiboldRuns(page.scene, exportPage);
      const plan: PagePlan = planPptxPage(page.scene, undefined, semiboldRuns);
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
          text: string | import("pptxgenjs").default.TextProps[],
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
      const nativeTextBoxes: (typeof reports)[number]["nativeTextBoxes"] = [];
      const nativeTexts: string[] = [],
        fonts = new Set<string>(),
        fontFaces = new Map<string, (typeof reports)[number]["fontFaces"][number]>();
      const textNodes = new Map(
        page.scene.nodes
          .filter((node) => node.kind === "text")
          .map((node) => [node.id, node]),
      );
      const nodesWithRasterText = new Set(
        regions
          .filter((region) => region.fragmentKeys?.length)
          .flatMap((region) => region.nodeIds),
      );
      const textStyleOptions = (e: NativeTextElement) => ({
        fontFace: e.font.family,
        fontSize: point(e.style.fontSize),
        bold: e.font.bold,
        italic: e.style.fontStyle === "italic",
        ...alpha(color(e.style.color), e.opacity),
        charSpacing: point(e.style.letterSpacing),
        underline: e.style.decoration.includes("underline")
          ? ({ style: "sng" } as const)
          : undefined,
        strike: e.style.decoration.includes("line-through")
          ? ("sngStrike" as const)
          : undefined,
        lang: "zh-CN",
      });
      const recordNativeText = (e: NativeTextElement) => {
        nativeTexts.push(e.text);
        nativeFragments.push({
          nodeId: e.nodeId,
          fragmentKey: e.fragmentKey,
          text: e.text,
          baseline: e.baseline,
          requestedFontFamily: e.style.fontFamily,
          fontFamily: e.font.family,
          postscriptName: e.font.postscript,
          fontFileSha256: e.font.fileSha256,
          fontSize: e.style.fontSize,
          letterSpacing: e.style.letterSpacing,
          requestedWeight: e.font.requestedWeight,
          faceWeight: e.font.faceWeight,
          bold: e.font.bold,
          italic: e.font.italic,
          ascent: e.font.ascent,
          descent: e.font.descent,
          unitsPerEm: e.font.unitsPerEm,
          measuredWidth: e.font.width,
        });
        fonts.add(e.font.family);
        const faceKey = `${e.font.postscript}:${e.font.fileSha256}:${e.font.requestedWeight}:${e.font.italic}`;
        const face = fontFaces.get(faceKey);
        if (face) face.uses++;
        else
          fontFaces.set(faceKey, {
            family: e.font.family,
            postscriptName: e.font.postscript,
            fontFileSha256: e.font.fileSha256,
            requestedWeight: e.font.requestedWeight,
            faceWeight: e.font.faceWeight,
            italic: e.font.italic,
            uses: 1,
          });
      };
      const writeNativeText = (elements: NativeTextElement[], wholeBox: boolean) => {
        const first = elements[0];
        const node = textNodes.get(first.nodeId);
        const layout = wholeBox ? page.editor?.text?.[first.nodeId] : undefined;
        const alignment = layout?.align || "left";
        const lines = [...new Set(elements.map((element) => element.lineIndex))];
        const merged = lines.length > 1;
        const boxes = elements.map((element) => {
          const ascent =
            (element.style.fontSize * element.font.ascent) /
            element.font.unitsPerEm;
          return {
            x: element.frame.x,
            y: element.baseline - ascent,
            width: Math.max(element.frame.width, element.font.width) + 0.5,
            height: Math.max(
              element.frame.height,
              element.style.fontSize * 1.5,
            ),
          };
        });
        const left = Math.min(...boxes.map((box) => box.x));
        const right = Math.max(...boxes.map((box) => box.x + box.width));
        const legacyWidth = right - left - 0.5;
        const frame = {
          x: layout?.box?.x ?? left,
          y: boxes[0].y,
          width: layout?.box?.width ?? (alignment === "left" ? right - left : legacyWidth),
          height: Math.max(...boxes.map((box) => box.y + box.height)) - boxes[0].y,
        };
        const lineStep = merged
          ? elements.find((element) => element.lineIndex !== first.lineIndex)!
              .baseline - first.baseline
          : undefined;
        const options = {
          ...geometry(frame),
          ...textStyleOptions(first),
          margin: 0,
          fit: "none" as const,
          wrap: false,
          align: alignment,
          valign: "top" as const,
          paraSpaceAfter: 0,
          paraSpaceBefore: 0,
          ...(lineStep === undefined
            ? { lineSpacingMultiple: 1 }
            : { lineSpacing: point(lineStep) }),
          objectName: `text:${first.nodeId}`,
        };
        if (elements.length > 1) {
          let previousLine = first.lineIndex;
          slide.addText(
            elements.map((element) => {
              const softBreakBefore = element.lineIndex !== previousLine;
              previousLine = element.lineIndex;
              return {
                text: element.text,
                options: {
                  ...textStyleOptions(element),
                  ...(softBreakBefore ? { softBreakBefore: true } : {}),
                },
              };
            }),
            options,
          );
        } else slide.addText(first.text, options);
        nativeTextBoxes.push({
          nodeId: first.nodeId,
          text: elements.map((element) => element.text).join(""),
          paragraphCount: 1,
          softBreakCount: merged ? lines.length - 1 : 0,
        });
        elements.forEach(recordNativeText);
      };
      let ri = 0;
      for (
        let elementIndex = 0;
        elementIndex < plan.elements.length;
        elementIndex++
      ) {
        const e = plan.elements[elementIndex];
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
          const node = textNodes.get(e.nodeId);
          const layout = page.editor?.text?.[e.nodeId];
          const group = mergeableTextGroup(
            plan.elements,
            elementIndex,
            node?.text.paragraphs.length === 1,
            nodesWithRasterText.has(e.nodeId),
            layout?.align || "left",
            !!layout?.box,
          );
          const textGroup = group || [e];
          const nativeWidth = textGroup.reduce((amount, run) => amount + run.text.length, 0);
          const sourceWidth = node?.text.lines.reduce((amount, line) => amount +
            line.fragments.reduce((sum, fragment) => sum + fragment.displayText.length, 0), 0);
          writeNativeText(textGroup, nativeWidth === sourceWidth && !nodesWithRasterText.has(e.nodeId));
          elementIndex += textGroup.length - 1;
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
              ? {
                  ...alpha(color(b.color), e.opacity),
                  width: point(b.width),
                  ...(b.style === "dashed" ? { dashType: "dash" as const } : {}),
                  ...(b.style === "dotted" ? { dashType: "sysDot" as const } : {}),
                }
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
      const rasterFragmentKeys = new Set(
        regions.flatMap((region) => region.fragmentKeys || []),
      );
      const wholeRasterNodeIds = new Set(
        regions
          .filter((region) => !region.fragmentKeys?.length)
          .flatMap((region) => region.nodeIds),
      );
      const rasterTextFragments = new Map<string, string>();
      for (const node of page.scene.nodes) {
        if (node.kind !== "text") continue;
        for (const [lineIndex, line] of node.text.lines.entries())
          for (const [fragmentIndex, fragment] of line.fragments.entries()) {
            const key = `${node.id}/${lineIndex}/${fragmentIndex}`;
            if (
              wholeRasterNodeIds.has(node.id) ||
              rasterFragmentKeys.has(key)
            )
              rasterTextFragments.set(key, fragment.displayText);
          }
      }
      const svgTextCharacters = page.scene.textInventory
        .filter((item) => item.reason === "svg-asset")
        .reduce((sum, item) => sum + characters(item.sourceText), 0);
      const pageRasterCharacters = page.scene.textInventory
        .filter((item) => item.reason === "page-raster")
        .reduce((sum, item) => sum + characters(item.sourceText), 0);
      const fullPageRasterCount = page.scene.assets.filter((asset) =>
        asset.bakedEffects.includes("full-page-composite"),
      ).length;
      reports.push({
        sceneId: page.sceneId,
        nativeTexts,
        nativeTextBoxes,
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
        fontMapVersion: FONT_MAP_VERSION,
        fontFaces: [...fontFaces.values()],
        nativeTextCharacterCount: nativeTexts.reduce(
          (sum, text) => sum + characters(text),
          0,
        ),
        svgTextCharacterCount: svgTextCharacters,
        rasterTextCharacterCount:
          [...rasterTextFragments.values()].reduce(
            (sum, text) => sum + characters(text),
            0,
          ) + pageRasterCharacters,
        nativeShapeCount: plan.elements.filter((e) => e.kind === "shape").length,
        nativeImageCount: plan.elements.filter((e) => e.kind === "image").length,
        sourceSvgCount: svgRegions.filter((region) => region.reasonCode === "SOURCE_SVG").length,
        generatedVectorRegionCount: svgRegions.filter((region) => region.reasonCode !== "SOURCE_SVG").length,
        localRasterRegionCount: regions.filter((region) => !region.shadowOnly).length,
        fullPageRasterCount,
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
  if (signal?.aborted) throw new Error("EXPORT_ABORTED");
  const bundle = join(
    dirname(createRequire(import.meta.url).resolve("pptxgenjs")),
    "pptxgen.bundle.js",
  );
  await exportPage.addScriptTag({ content: await readFile(bundle, "utf8") });
  const encoded = await exportPage.evaluate(serializePresentation, {
    title: input.title,
    slides,
  });
  const raw = Buffer.from(encoded, "base64");
  if (raw.length > 128 * 1024 * 1024) throw new Error("PPTX_OUTPUT_LIMIT");
  await writeFile(join(output, "presentation.pptx"), raw);
  if (signal?.aborted) throw new Error("EXPORT_ABORTED");
  const report = {
    inputHash: input.inputHash,
    pages: reports,
    warnings: [...new Set(warnings)],
  };
  await writeFile(join(output, "report.json"), JSON.stringify(report));
  return report;
}
