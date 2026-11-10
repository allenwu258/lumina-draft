import {
  assertScene,
  type SceneIR,
  type SceneNode,
  type TextStyle,
  type Rect,
} from "@lumina/scene-core";

type Context = CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D;
export type AssetResolver = (
  asset: SceneIR["assets"][number],
  signal?: AbortSignal,
) => Promise<ImageBitmap>;
export type FontResolver = (
  style: TextStyle,
  sample: string,
  signal?: AbortSignal,
) => Promise<void>;
const font = (s: TextStyle) =>
  `${s.fontStyle} ${s.fontWeight} ${s.fontSize}px ${s.fontFamily}`;
function rounded(ctx: Context, r: Rect, radii: number[]) {
  ctx.beginPath();
  ctx.roundRect(r.x, r.y, r.width, r.height, radii);
}
function draw(ctx: Context, n: SceneNode, images: Map<string, ImageBitmap>) {
  if (n.kind === "shape") {
    for (const shadow of n.shadows) {
      ctx.save();
      ctx.shadowColor = shadow.color;
      ctx.shadowBlur = shadow.blur / 2;
      ctx.shadowOffsetX = shadow.x;
      ctx.shadowOffsetY = shadow.y;
      // Offset the caster outside the clip; keep only its shadow, including spread.
      const r = {
        x: n.frame.x - shadow.spread,
        y: n.frame.y - shadow.spread,
        width: Math.max(0, n.frame.width + 2 * shadow.spread),
        height: Math.max(0, n.frame.height + 2 * shadow.spread),
      };
      ctx.shadowOffsetX += 10000;
      ctx.translate(-10000, 0);
      rounded(
        ctx,
        r,
        n.radii.map((v) => Math.max(0, v + shadow.spread)),
      );
      ctx.fillStyle = "black";
      ctx.fill();
      ctx.restore();
    }
    rounded(ctx, n.frame, n.radii);
    ctx.fillStyle = n.fill;
    ctx.fill();
    const [t, r, b, l] = n.borders;
    if (
      n.borders.every(
        (v) =>
          v.width === t.width && v.color === t.color && v.style === t.style,
      )
    ) {
      if (t.width > 0 && t.style !== "none" && t.style !== "hidden") {
        ctx.strokeStyle = t.color;
        ctx.lineWidth = t.width;
        rounded(
          ctx,
          {
            x: n.frame.x + t.width / 2,
            y: n.frame.y + t.width / 2,
            width: Math.max(0, n.frame.width - t.width),
            height: Math.max(0, n.frame.height - t.width),
          },
          n.radii.map((v) => Math.max(0, v - t.width / 2)),
        );
        ctx.stroke();
      }
    } else {
      const f = n.frame,
        parts = [
          { x: f.x, y: f.y, width: f.width, height: t.width },
          {
            x: f.x + f.width - r.width,
            y: f.y,
            width: r.width,
            height: f.height,
          },
          {
            x: f.x,
            y: f.y + f.height - b.width,
            width: f.width,
            height: b.width,
          },
          { x: f.x, y: f.y, width: l.width, height: f.height },
        ];
      ctx.save();
      rounded(ctx, f, n.radii);
      ctx.clip();
      n.borders.forEach((border, i) => {
        if (
          border.width &&
          border.style !== "none" &&
          border.style !== "hidden"
        ) {
          ctx.fillStyle = border.color;
          const p = parts[i];
          ctx.fillRect(p.x, p.y, p.width, p.height);
        }
      });
      ctx.restore();
    }
  } else if (n.kind === "text") {
    const runs = new Map(
      n.text.paragraphs.flatMap((p) => p.runs).map((r) => [r.id, r]),
    );
    ctx.textBaseline = "alphabetic";
    ctx.textAlign = "left";
    for (const line of n.text.lines)
      for (const f of line.fragments) {
        const s = runs.get(f.runId)!.style;
        ctx.font = font(s);
        ctx.fillStyle = s.color;
        if (s.letterSpacing && !("letterSpacing" in ctx))
          throw new Error("当前浏览器不支持 Canvas 字距");
        ctx.letterSpacing = `${s.letterSpacing}px`;
        ctx.fillText(f.displayText, f.frame.x, f.baseline);
        if (s.decoration.includes("underline"))
          ctx.fillRect(
            f.frame.x,
            f.baseline + s.fontSize * 0.1,
            f.frame.width,
            Math.max(1, s.fontSize / 16),
          );
        if (s.decoration.includes("line-through"))
          ctx.fillRect(
            f.frame.x,
            f.baseline - s.fontSize * 0.3,
            f.frame.width,
            Math.max(1, s.fontSize / 16),
          );
      }
  } else if (n.kind === "image") {
    const image = images.get(n.assetId);
    if (!image) throw new Error("场景图片尚未就绪");
    ctx.drawImage(image, n.frame.x, n.frame.y, n.frame.width, n.frame.height);
  }
}

/** Disposable drawing cache; SceneIR remains the only page model. No HTML/DOM dependency. */
export class SceneRenderer {
  private scene?: SceneIR;
  private images = new Map<string, ImageBitmap>();
  private generation = 0;
  private disposed = false;
  private width = 800;
  private height = 450;
  private dpr = 1;
  constructor(private canvas: HTMLCanvasElement) {}
  async load(
    scene: SceneIR,
    assetResolver: AssetResolver,
    fontResolver: FontResolver,
    signal?: AbortSignal,
  ) {
    assertScene(scene);
    const token = ++this.generation;
    this.scene = undefined;
    this.images.forEach((i) => i.close());
    this.images.clear();
    const decoded = new Map<string, ImageBitmap>();
    const fonts = new Map<string, { style: TextStyle; sample: string }>();
    for (const n of scene.nodes)
      if (n.kind === "text")
        for (const p of n.text.paragraphs)
          for (const r of p.runs) {
            const key = JSON.stringify(r.style);
            if (!fonts.has(key))
              fonts.set(key, {
                style: r.style,
                sample: n.text.lines
                  .flatMap((l) => l.fragments)
                  .map((f) => f.displayText)
                  .join("")
                  .slice(0, 120),
              });
          }
    try {
      const results = await Promise.allSettled([
        ...scene.assets.map(async (a) => {
          const image = await assetResolver(a, signal);
          decoded.set(a.id, image);
        }),
        ...[...fonts.values()].map((f) =>
          fontResolver(f.style, f.sample, signal),
        ),
      ]);
      const failure = results.find((r) => r.status === "rejected");
      if (failure?.status === "rejected") throw failure.reason;
      if (this.disposed || token !== this.generation || signal?.aborted)
        throw new DOMException("Aborted", "AbortError");
      // Verify layout fonts before displaying the first frame. Never rewrap/compress text.
      const ctx = this.canvas.getContext("2d")!;
      for (const n of scene.nodes)
        if (n.kind === "text") {
          const runs = new Map(
            n.text.paragraphs.flatMap((p) => p.runs).map((r) => [r.id, r]),
          );
          for (const l of n.text.lines)
            for (const f of l.fragments) {
              const s = runs.get(f.runId)!.style;
              ctx.font = font(s);
              ctx.letterSpacing = `${s.letterSpacing}px`;
              if (
                Math.abs(
                  ctx.measureText(f.displayText).width - f.measuredWidth,
                ) > Math.max(1, f.measuredWidth * 0.01)
              )
                throw new Error(`字体测量不一致：${s.fontFamily}`);
            }
        }
      this.images = decoded;
      this.scene = scene;
    } catch (error) {
      decoded.forEach((i) => i.close());
      throw error;
    }
  }
  resize(width: number, height: number, dpr = 1) {
    if (this.disposed) return;
    this.width = Math.max(1, width);
    this.height = Math.max(1, height);
    this.dpr = Math.max(1, dpr);
    this.canvas.width = Math.round(this.width * this.dpr);
    this.canvas.height = Math.round(this.height * this.dpr);
    this.canvas.style.width = `${this.width}px`;
    this.canvas.style.height = `${this.height}px`;
    if (this.scene) this.render();
  }
  render() {
    if (!this.scene || this.disposed) return;
    const scale =
      Math.min(
        this.width / this.scene.page.width,
        this.height / this.scene.page.height,
      ) * this.dpr;
    this.paint(
      this.canvas,
      {
        x: 0,
        y: 0,
        width: this.scene.page.width,
        height: this.scene.page.height,
      },
      scale,
      undefined,
      true,
    );
  }
  /** Transparent Scene-only composition. Ancestor clip/opacity is applied exactly once. */
  renderRegion(bounds: Rect, nodeIds: string[], scale = 3): HTMLCanvasElement {
    if (!this.scene || this.disposed) throw new Error("SCENE_NOT_LOADED");
    const target = document.createElement("canvas");
    target.width = Math.max(1, Math.ceil(bounds.width * scale));
    target.height = Math.max(1, Math.ceil(bounds.height * scale));
    if (target.width * target.height > 16_000_000)
      throw new Error("RASTER_PIXEL_LIMIT");
    this.paint(target, bounds, scale, new Set(nodeIds), false);
    return target;
  }
  private paint(
    target: HTMLCanvasElement,
    bounds: Rect,
    scale: number,
    selected: Set<string> | undefined,
    background: boolean,
  ) {
    if (!this.scene) return;
    const scene = this.scene,
      nodes = new Map(scene.nodes.map((n) => [n.id, n]));
    const main = target.getContext("2d")!;
    main.resetTransform();
    main.clearRect(0, 0, target.width, target.height);
    main.setTransform(scale, 0, 0, scale, -bounds.x * scale, -bounds.y * scale);
    if (background) {
      main.fillStyle = scene.page.background;
      main.fillRect(0, 0, scene.page.width, scene.page.height);
    }
    main.beginPath();
    main.rect(0, 0, scene.page.width, scene.page.height);
    main.clip();
    let ctx: Context = main;
    const stack: {
      parent: Context;
      layer: OffscreenCanvas | null;
      opacity: number;
    }[] = [];
    const clips: { frame: Rect; radii: number[] }[] = [];
    const clipCounts: number[] = [];
    for (const op of scene.paintOps) {
      const n = nodes.get(op.nodeId)!;
      if (op.op === "begin" && (n.kind === "group" || n.kind === "table")) {
        ctx.save();
        const parent = ctx;
        let layer: OffscreenCanvas | null = null;
        if (n.opacity < 1) {
          layer = new OffscreenCanvas(target.width, target.height);
          ctx = layer.getContext("2d")!;
          ctx.setTransform(parent.getTransform());
          for (const clip of clips) {
            rounded(ctx, clip.frame, clip.radii);
            ctx.clip();
          }
        }
        stack.push({ parent, layer, opacity: n.opacity });
        clipCounts.push(clips.length);
        if (n.clip) {
          clips.push(n.clip);
          rounded(ctx, n.clip.frame, n.clip.radii);
          ctx.clip();
        }
      } else if (op.op === "end") {
        const entry = stack.pop()!;
        clips.length = clipCounts.pop()!;
        if (entry.layer) {
          ctx = entry.parent;
          ctx.save();
          ctx.resetTransform();
          ctx.globalAlpha = entry.opacity;
          ctx.drawImage(entry.layer, 0, 0);
          ctx.restore();
          entry.layer.width = 1;
          entry.layer.height = 1;
        }
        ctx = entry.parent;
        ctx.restore();
      } else if (!selected || selected.has(n.id)) draw(ctx, n, this.images);
    }
  }
  dispose() {
    this.disposed = true;
    this.generation++;
    this.scene = undefined;
    this.images.forEach((i) => i.close());
    this.images.clear();
    this.canvas.width = 1;
    this.canvas.height = 1;
  }
}
