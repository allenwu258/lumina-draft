import {
  assertScene,
  type SceneIR,
  type Rect,
  type SceneNode,
  type TextStyle,
} from "@lumina/scene-core";
import { resolveFont, splitFontRuns, type ResolvedFont } from "./fonts.js";

export const inch = (px: number) => px / 60;
export const point = (px: number) => px * 1.2;
export type RasterRegion = {
  frame: Rect;
  nodeIds: string[];
  reason: string;
  reasonCode: string;
  fragmentKeys?: string[];
  shadowOnly?: boolean;
};
export type Element =
  | {
      kind: "shape";
      opacity: number;
      node: Extract<SceneNode, { kind: "shape" }>;
    }
  | {
      kind: "image";
      opacity: number;
      node: Extract<SceneNode, { kind: "image" }>;
    }
  | {
      kind: "text";
      opacity: number;
      nodeId: string;
      fragmentKey: string;
      text: string;
      frame: Rect;
      baseline: number;
      style: TextStyle;
      font: ResolvedFont;
    }
  | { kind: "raster"; region: RasterRegion }
  | { kind: "svg"; region: RasterRegion };
export type PagePlan = {
  elements: Element[];
  coveredNodeIds: string[];
  invisibleNodeIds: string[];
  warnings: string[];
};
const union = (a: Rect, b: Rect): Rect => ({
  x: Math.min(a.x, b.x),
  y: Math.min(a.y, b.y),
  width: Math.max(a.x + a.width, b.x + b.width) - Math.min(a.x, b.x),
  height: Math.max(a.y + a.height, b.y + b.height) - Math.min(a.y, b.y),
});
function paintBounds(n: SceneNode): Rect {
  if (n.kind !== "shape") return n.frame;
  let r = n.frame;
  for (const s of n.shadows) {
    const d = Math.max(0, s.spread + s.blur * 2 + 1);
    r = union(r, {
      x: n.frame.x + s.x - d,
      y: n.frame.y + s.y - d,
      width: n.frame.width + 2 * d,
      height: n.frame.height + 2 * d,
    });
  }
  return r;
}
function inside(
  n: SceneNode,
  clip: NonNullable<Extract<SceneNode, { kind: "group" | "table" }>["clip"]>,
): boolean {
  const f = paintBounds(n),
    c = clip.frame,
    eps = 0.05;
  if (
    f.x < c.x - eps ||
    f.y < c.y - eps ||
    f.x + f.width > c.x + c.width + eps ||
    f.y + f.height > c.y + c.height + eps
  )
    return false;
  if (clip.radii.every((v) => v === 0)) return true;
  if (
    n.kind === "shape" &&
    !n.shadows.length &&
    Object.keys(c).every(
      (k) => Math.abs(c[k as keyof Rect] - f[k as keyof Rect]) < eps,
    ) &&
    n.radii.every((v, i) => v >= clip.radii[i])
  )
    return true;
  const maxRadius = Math.max(...clip.radii);
  // Conservative inner cross; anything uncertain is locally composed.
  return (
    (f.x >= c.x + maxRadius && f.x + f.width <= c.x + c.width - maxRadius) ||
    (f.y >= c.y + maxRadius && f.y + f.height <= c.y + c.height - maxRadius)
  );
}

/** Pure Scene→PPTX mapping. Raster requests are contiguous, disjoint paint ranges. */
export function planPptxPage(
  scene: SceneIR,
  fontResolver = resolveFont,
): PagePlan {
  assertScene(scene);
  const nodes = new Map(scene.nodes.map((n) => [n.id, n]));
  const ends = new Map<number, number>(),
    stack: number[] = [];
  scene.paintOps.forEach((op, i) => {
    if (op.op === "begin") stack.push(i);
    if (op.op === "end") ends.set(stack.pop()!, i);
  });
  const elements: Element[] = [],
    coveredNodeIds: string[] = [],
    warnings: string[] = [];
  const invisibleNodeIds: string[] = [];
  const ancestors: Extract<SceneNode, { kind: "group" | "table" }>[] = [];
  const raster = (
    drawn: SceneNode[],
    reason: string,
    reasonCode: string,
    vector = false,
    options: {
      frame?: Rect;
      fragmentKeys?: string[];
      shadowOnly?: boolean;
      cover?: boolean;
    } = {},
  ) => {
    if (!drawn.length) return;
    let frame = options.frame || drawn.map(paintBounds).reduce(union);
    const x = Math.max(0, Math.floor(frame.x)),
      y = Math.max(0, Math.floor(frame.y));
    frame = {
      x,
      y,
      width: Math.max(
        0,
        Math.min(scene.page.width, Math.ceil(frame.x + frame.width)) - x,
      ),
      height: Math.max(
        0,
        Math.min(scene.page.height, Math.ceil(frame.y + frame.height)) - y,
      ),
    };
    const nodeIds = drawn.map((n) => n.id);
    if (options.cover !== false) coveredNodeIds.push(...nodeIds);
    if (frame.width === 0 || frame.height === 0) {
      if (options.cover !== false) invisibleNodeIds.push(...nodeIds);
      warnings.push("页面外的不可见元素未写入");
      return;
    }
    if (
      frame.width >= scene.page.width &&
      frame.height >= scene.page.height &&
      !options.fragmentKeys &&
      drawn.some((n) => n.kind === "text")
    )
      throw new Error(`FULL_SLIDE_RASTER_REQUIRED: ${reason}`);
    elements.push({
      kind: vector ? "svg" : "raster",
      region: {
        frame,
        nodeIds,
        reason,
        reasonCode,
        ...(options.fragmentKeys ? { fragmentKeys: options.fragmentKeys } : {}),
        ...(options.shadowOnly ? { shadowOnly: true } : {}),
      },
    });
    if (!vector)
      warnings.push(
        `部分元素将作为图片导出（${reason}，${nodeIds.length} 项）`,
      );
  };
  for (let i = 0; i < scene.paintOps.length; i++) {
    const op = scene.paintOps[i],
      n = nodes.get(op.nodeId)!;
    if (op.op === "end") {
      ancestors.pop();
      continue;
    }
    if (op.op === "begin" && (n.kind === "group" || n.kind === "table")) {
      const end = ends.get(i)!;
      const drawn = scene.paintOps
        .slice(i + 1, end)
        .filter((o) => o.op === "draw")
        .map((o) => nodes.get(o.nodeId)!);
      if (n.opacity < 1 && drawn.length > 1) {
        const vector = drawn.every(
          (n) => n.kind === "shape" && !n.shadows.length,
        );
        raster(drawn, "透明组合成", "GROUP_OPACITY", vector);
        i = end;
      } else ancestors.push(n);
      continue;
    }
    const opacity = ancestors.reduce(
      (value, group) => value * group.opacity,
      1,
    );
    const bounds = paintBounds(n);
    if (
      opacity === 0 ||
      bounds.x >= scene.page.width ||
      bounds.y >= scene.page.height ||
      bounds.x + bounds.width <= 0 ||
      bounds.y + bounds.height <= 0
    ) {
      coveredNodeIds.push(n.id);
      invisibleNodeIds.push(n.id);
      continue;
    }
    const pageClip = (clip: NonNullable<(typeof ancestors)[number]["clip"]>) =>
      clip.radii.every((r) => r === 0) &&
      clip.frame.x <= 0 &&
      clip.frame.y <= 0 &&
      clip.frame.x + clip.frame.width >= scene.page.width &&
      clip.frame.y + clip.frame.height >= scene.page.height;
    if (
      ancestors.some((a) => a.clip && !pageClip(a.clip) && !inside(n, a.clip))
    ) {
      raster(
        [n],
        "内容裁剪",
        "INTERNAL_CLIP",
        n.kind === "shape" && !n.shadows.length,
      );
      continue;
    }
    if (n.kind === "shape") {
      if (
        opacity < 1 &&
        n.borders.some(
          (b) => b.width > 0 && b.style !== "none" && b.style !== "hidden",
        )
      ) {
        raster([n], "透明组合成", "GROUP_OPACITY", !n.shadows.length);
        continue;
      }
      if (n.shadows.length && opacity === 1) {
        // Shadow is the only blurred content. Preserve its foreground as native geometry.
        raster([n], "阴影", "SHADOW", false, {
          shadowOnly: true,
          cover: false,
        });
        const body = { ...n, shadows: [] };
        const complex =
          body.radii.some((v) => v !== body.radii[0]) ||
          (body.radii[0] > 0 &&
            body.borders.some(
              (b) =>
                b.width &&
                (b.width !== body.borders[0].width ||
                  b.color !== body.borders[0].color),
            ));
        if (complex) raster([body], "复杂圆角边框", "COMPLEX_GEOMETRY", true);
        else {
          elements.push({ kind: "shape", node: body, opacity });
          coveredNodeIds.push(n.id);
        }
        continue;
      }
      if (
        n.shadows.length ||
        n.radii.some((v) => v !== n.radii[0]) ||
        (n.radii[0] > 0 &&
          n.borders.some(
            (b) =>
              b.width &&
              (b.width !== n.borders[0].width ||
                b.color !== n.borders[0].color),
          ))
      ) {
        raster(
          [n],
          n.shadows.length ? "阴影" : "复杂圆角边框",
          n.shadows.length ? "SHADOW" : "COMPLEX_GEOMETRY",
          !n.shadows.length,
        );
        continue;
      }
      elements.push({ kind: "shape", node: n, opacity });
    } else if (n.kind === "image")
      elements.push({ kind: "image", node: n, opacity });
    else if (n.kind === "text") {
      const runs = new Map(
        n.text.paragraphs.flatMap((p) => p.runs).map((r) => [r.id, r]),
      );
      for (const [lineIndex, line] of n.text.lines.entries())
        for (const [fragmentIndex, fragment] of line.fragments.entries()) {
          if (!fragment.displayText) continue;
          const style = runs.get(fragment.runId)!.style,
            fragmentKey = `${n.id}/${lineIndex}/${fragmentIndex}`;
          try {
            let parts: { text: string; font: ResolvedFont }[];
            try {
              parts = [
                {
                  text: fragment.displayText,
                  font: fontResolver(style, fragment.displayText),
                },
              ];
            } catch (error) {
              if (!String(error).includes("FONT_UNAVAILABLE")) throw error;
              parts = splitFontRuns(style, fragment.displayText, fontResolver);
            }
            let width = parts.reduce((sum, part) => sum + part.font.width, 0);
            if (
              Math.abs(width - fragment.measuredWidth) >
              Math.max(2, fragment.measuredWidth * 0.035)
            ) {
              parts = splitFontRuns(style, fragment.displayText, fontResolver);
              width = parts.reduce((sum, part) => sum + part.font.width, 0);
            }
            if (
              Math.abs(width - fragment.measuredWidth) >
              Math.max(2, fragment.measuredWidth * 0.035)
            )
              throw new Error("FONT_UNAVAILABLE: metrics");
            let x = fragment.frame.x;
            for (const part of parts) {
              elements.push({
                kind: "text",
                nodeId: n.id,
                fragmentKey,
                text: part.text,
                frame: { ...fragment.frame, x, width: part.font.width },
                baseline: fragment.baseline,
                style,
                font: part.font,
                opacity,
              });
              x += part.font.width;
            }
          } catch (error) {
            if (!String(error).includes("FONT_UNAVAILABLE")) throw error;
            raster([n], "字体度量差异", "FONT_METRICS", false, {
              frame: fragment.frame,
              fragmentKeys: [fragmentKey],
              cover: false,
            });
          }
        }
    }
    coveredNodeIds.push(n.id);
  }
  return { elements, coveredNodeIds, invisibleNodeIds, warnings };
}
