import type { SceneIR, Rect, ShapeNode } from "@lumina/scene-core";
import { color } from "./color.js";

const escape = (s: string) =>
  s.replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;");
const paint = (value: string, name = "fill") => {
  const c = color(value);
  return `${name}="#${c.color}" ${name}-opacity="${1 - c.transparency / 100}"`;
};
function rounded(r: Rect, radii: number[]) {
  const { x, y, width: w, height: h } = r;
  const [a, b, c, d] = radii.map((v) => Math.max(0, Math.min(v, w / 2, h / 2)));
  return `M${x + a} ${y}H${x + w - b}${b ? `A${b} ${b} 0 0 1 ${x + w} ${y + b}` : ""}V${y + h - c}${c ? `A${c} ${c} 0 0 1 ${x + w - c} ${y + h}` : ""}H${x + d}${d ? `A${d} ${d} 0 0 1 ${x} ${y + h - d}` : ""}V${y + a}${a ? `A${a} ${a} 0 0 1 ${x + a} ${y}` : ""}Z`;
}
function shape(n: ShapeNode, id: string) {
  const path = rounded(n.frame, n.radii),
    b = n.borders[0];
  let result = `<path d="${path}" ${paint(n.fill)}/>`;
  const visible = (v: typeof b) =>
    v.width > 0 && v.style !== "none" && v.style !== "hidden";
  if (
    n.borders.every(
      (v) => v.width === b.width && v.color === b.color && v.style === b.style,
    )
  ) {
    if (visible(b)) {
      const r = {
        x: n.frame.x + b.width / 2,
        y: n.frame.y + b.width / 2,
        width: Math.max(0, n.frame.width - b.width),
        height: Math.max(0, n.frame.height - b.width),
      };
      result += `<path d="${rounded(
        r,
        n.radii.map((v) => Math.max(0, v - b.width / 2)),
      )}" fill="none" ${paint(b.color, "stroke")} stroke-width="${b.width}"/>`;
    }
  } else {
    const f = n.frame,
      [t, r, bot, l] = n.borders;
    const frames = [
      [f.x, f.y, f.width, t.width],
      [f.x + f.width - r.width, f.y, r.width, f.height],
      [f.x, f.y + f.height - bot.width, f.width, bot.width],
      [f.x, f.y, l.width, f.height],
    ];
    result += `<defs><clipPath id="${id}"><path d="${path}"/></clipPath></defs><g clip-path="url(#${id})">`;
    n.borders.forEach((border, i) => {
      if (visible(border)) {
        const [x, y, w, h] = frames[i];
        result += `<rect x="${x}" y="${y}" width="${w}" height="${h}" ${paint(border.color)}/>`;
      }
    });
    result += "</g>";
  }
  return result;
}

/** Small Scene-only geometry emitter. Selection is a contiguous draw range chosen by the planner. */
export function sceneToSvg(
  scene: SceneIR,
  nodeIds: string[],
  bounds: Rect,
): string {
  const selected = new Set(nodeIds),
    nodes = new Map(scene.nodes.map((n) => [n.id, n]));
  let body = "",
    counter = 0;
  for (const op of scene.paintOps) {
    const n = nodes.get(op.nodeId)!;
    if (op.op === "begin" && (n.kind === "group" || n.kind === "table")) {
      const id = `clip${counter++}`;
      if (n.clip)
        body += `<defs><clipPath id="${id}"><path d="${rounded(n.clip.frame, n.clip.radii)}"/></clipPath></defs>`;
      body += `<g opacity="${n.opacity}"${n.clip ? ` clip-path="url(#${id})"` : ""}>`;
    } else if (op.op === "end") body += "</g>";
    else if (selected.has(n.id)) {
      if (n.kind !== "shape") throw new Error("SVG_GEOMETRY_UNSUPPORTED");
      body += shape(n, `border${counter++}`);
    }
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${bounds.width}" height="${bounds.height}" viewBox="${bounds.x} ${bounds.y} ${bounds.width} ${bounds.height}"><title>${escape("Scene geometry")}</title>${body}</svg>`;
}
