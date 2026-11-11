/** Scene coordinates are CSS px. This contract contains everything needed to draw a page. */
export type Rect = { x: number; y: number; width: number; height: number };
export type Clip = { frame: Rect; radii: number[] };
export type Border = { width: number; color: string; style: string };
export type TextStyle = {
  fontFamily: string;
  fontSize: number;
  fontWeight: string;
  fontStyle: string;
  color: string;
  letterSpacing: number;
  decoration: string;
};
export type Fragment = {
  runId: string;
  sourceStart: number;
  sourceEnd: number;
  displayText: string;
  frame: Rect;
  baseline: number;
  measuredWidth: number;
};
export type TextContent = {
  ownerId: string;
  sourceText: string;
  paragraphs: {
    runs: {
      id: string;
      sourceStart: number;
      sourceEnd: number;
      style: TextStyle;
    }[];
  }[];
  lines: { frame: Rect; baseline: number; fragments: Fragment[] }[];
};
type BaseNode = { id: string; sourcePath: string; frame: Rect };
export type GroupNode = BaseNode & {
  kind: "group" | "table";
  tagName: string;
  opacity: number;
  clip: Clip | null;
  cells?: {
    row: number;
    column: number;
    rowSpan: number;
    columnSpan: number;
    nodeId: string;
    frame: Rect;
  }[];
};
export type ShapeNode = BaseNode & {
  kind: "shape";
  fill: string;
  radii: number[];
  borders: Border[];
  shadows: {
    x: number;
    y: number;
    blur: number;
    spread: number;
    color: string;
  }[];
};
export type TextNode = BaseNode & { kind: "text"; text: TextContent };
export type ImageNode = BaseNode & { kind: "image"; assetId: string };
export type SceneNode = GroupNode | ShapeNode | TextNode | ImageNode;
export type CanvasSize = { width: number; height: number };
export const PPT_CANVAS: Readonly<CanvasSize> = { width: 800, height: 450 };
export const DESIGN_CANVAS_LIMITS = { minSide: 320, maxSide: 1920, maxPixels: 2_073_600 };
export function isDesignCanvas(value: unknown): value is CanvasSize {
  if (!value || typeof value !== "object") return false;
  const { width, height } = value as CanvasSize;
  const { minSide, maxSide, maxPixels } = DESIGN_CANVAS_LIMITS;
  return Number.isInteger(width) && Number.isInteger(height) &&
    width >= minSide && width <= maxSide && height >= minSide &&
    height <= maxSide && width * height <= maxPixels;
}
export type SceneIR = {
  schemaVersion: "0.3.0" | "0.4.0" | "0.5.0";
  source: { htmlHash: string; cssHash: string; converterVersion: string };
  environment: {
    chromium: string;
    platform: string;
    locale: string;
    timezone: string;
    dpr: number;
    fontSignature: string;
  };
  page: { width: number; height: number; background: string };
  nodes: SceneNode[];
  paintOps: { op: "begin" | "draw" | "end"; nodeId: string }[];
  assets: {
    id: string;
    mediaType: "image/png" | "image/svg+xml";
    fallbackAssetId?: string;
    sha256: string;
    byteLength: number;
    width: number;
    height: number;
    file: string;
    coveredNodeIds: string[];
    bakedEffects: string[];
  }[];
  textInventory: {
    sourcePath: string;
    sourceText: string;
    nodeId: string | null;
    reason: string | null;
    target?:
      | { kind: "text"; nodeId: string }
      | { kind: "asset"; nodeId: string; assetId: string }
      | { kind: "hidden"; reason: string };
  }[];
  diagnostics: {
    code: string;
    message: string;
    sourcePath: string;
    nodeId?: string;
    severity?: "info" | "warning" | "error";
    details?: Record<string, string | number | boolean | null>;
  }[];
};
export const SCENE_VERSION = "0.4.0" as const;
export const CONVERTER_VERSION = "dom-canvas-0.4.0";
export const DESIGN_SCENE_VERSION = "0.5.0" as const;
export const DESIGN_CONVERTER_VERSION = "dom-canvas-0.5.0";
export const EMU_PER_SCENE_PX = 15240;
