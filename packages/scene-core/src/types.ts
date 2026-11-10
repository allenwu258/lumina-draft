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
export type SceneIR = {
  schemaVersion: "0.3.0";
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
    mediaType: "image/png";
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
  }[];
  diagnostics: { code: string; message: string; sourcePath: string }[];
};
export const SCENE_VERSION = "0.3.0" as const;
export const CONVERTER_VERSION = "dom-canvas-0.3.0";
export const EMU_PER_SCENE_PX = 15240;
