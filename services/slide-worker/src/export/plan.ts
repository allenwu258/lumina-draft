import {
  assertScene,
  type SceneIR,
  type Rect,
  type SceneNode,
  type TextStyle,
} from "@lumina/scene-core";
import { resolveFont, type ResolvedFont } from "./fonts.js";

export const inch = (px: number) => px / 60;
export const point = (px: number) => px * 1.2;
export type RasterRegion = { frame: Rect; nodeIds: string[]; reason: string };
export type Element =
  | { kind: "shape"; node: Extract<SceneNode, { kind: "shape" }> }
  | { kind: "image"; node: Extract<SceneNode, { kind: "image" }> }
  | {
      kind: "text";
      nodeId: string;
      text: string;
      frame: Rect;
      baseline: number;
      style: TextStyle;
      font: ResolvedFont;
    }
  | { kind: "raster"; region: RasterRegion };
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
  const raster = (drawn: SceneNode[], reason: string) => {
    if (!drawn.length) return;
    let frame = drawn.map(paintBounds).reduce(union);
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
    coveredNodeIds.push(...nodeIds);
    if (frame.width === 0 || frame.height === 0) {
      invisibleNodeIds.push(...nodeIds);
      warnings.push("页面外的不可见元素未写入");
      return;
    }
    if (
      frame.width >= scene.page.width &&
      frame.height >= scene.page.height &&
      drawn.some((n) => n.kind === "text")
    )
      throw new Error(`FULL_SLIDE_RASTER_REQUIRED: ${reason}`);
    elements.push({ kind: "raster", region: { frame, nodeIds, reason } });
    warnings.push(`部分元素将作为图片导出（${reason}，${nodeIds.length} 项）`);
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
      if (n.opacity < 1) {
        raster(drawn, "透明组合成");
        i = end;
      } else ancestors.push(n);
      continue;
    }
    if (ancestors.some((a) => a.clip && !inside(n, a.clip))) {
      raster([n], "内容裁剪");
      continue;
    }
    if (n.kind === "shape") {
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
        raster([n], n.shadows.length ? "阴影" : "复杂圆角边框");
        continue;
      }
      elements.push({ kind: "shape", node: n });
    } else if (n.kind === "image") elements.push({ kind: "image", node: n });
    else if (n.kind === "text") {
      const runs = new Map(
        n.text.paragraphs.flatMap((p) => p.runs).map((r) => [r.id, r]),
      );
      const texts: Extract<Element, { kind: "text" }>[] = [];
      let mismatch = false;
      for (const line of n.text.lines)
        for (const fragment of line.fragments) {
          if (!fragment.displayText) continue;
          const style = runs.get(fragment.runId)!.style;
          let font: ResolvedFont;
          try {
            font = fontResolver(style, fragment.displayText);
          } catch (error) {
            if (!String(error).includes("FONT_UNAVAILABLE")) throw error;
            mismatch = true;
            continue;
          }
          if (
            Math.abs(font.width - fragment.measuredWidth) >
            Math.max(2, fragment.measuredWidth * 0.035)
          )
            mismatch = true;
          texts.push({
            kind: "text",
            nodeId: n.id,
            text: fragment.displayText,
            frame: fragment.frame,
            baseline: fragment.baseline,
            style,
            font,
          });
        }
      if (mismatch) {
        raster([n], "字体度量差异");
        continue;
      }
      elements.push(...texts);
    }
    coveredNodeIds.push(n.id);
  }
  return { elements, coveredNodeIds, invisibleNodeIds, warnings };
}
