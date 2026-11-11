import { Ajv } from "ajv";
import schema from "../contracts/scene-ir/0.3.0.schema.json" with { type: "json" };
import schema4 from "../contracts/scene-ir/0.4.0.schema.json" with { type: "json" };
import schema5 from "../contracts/scene-ir/0.5.0.schema.json" with { type: "json" };
import { DESIGN_SCENE_VERSION, isDesignCanvas, type SceneIR } from "./types.js";
export * from "./types.js";

const ajv = new Ajv({ allErrors: true, strict: false });
const checks = { "0.3.0": ajv.compile(schema), "0.4.0": ajv.compile(schema4), "0.5.0": ajv.compile(schema5) };
export function assertScene(value: unknown): asserts value is SceneIR {
  const version = (value as SceneIR)?.schemaVersion;
  const check = checks[version];
  if (!check) throw new Error("SCENE_UNSUPPORTED_VERSION");
  if (!check(value))
    throw new Error(
      `SCENE_SCHEMA: ${JSON.stringify(check.errors)?.slice(0, 1200)}`,
    );
  const s = value as SceneIR;
  if (s.schemaVersion === DESIGN_SCENE_VERSION && !isDesignCanvas(s.page))
    throw new Error("SCENE_CANVAS_LIMIT");
  const nodes = new Map(s.nodes.map((n) => [n.id, n]));
  if (nodes.size !== s.nodes.length) throw new Error("SCENE_DUPLICATE_ID");
  const assets = new Map(s.assets.map((a) => [a.id, a]));
  if (assets.size !== s.assets.length) throw new Error("SCENE_DUPLICATE_ASSET");
  const stack: string[] = [];
  const drawn = new Set<string>();
  for (const p of s.paintOps) {
    const n = nodes.get(p.nodeId);
    if (!n) throw new Error("SCENE_DANGLING_PAINT");
    if (p.op === "begin") {
      if (n.kind !== "group" && n.kind !== "table")
        throw new Error("SCENE_INVALID_GROUP");
      if (drawn.has(n.id)) throw new Error("SCENE_DUPLICATE_PAINT");
      drawn.add(n.id);
      stack.push(n.id);
    } else if (p.op === "end") {
      if (stack.pop() !== n.id) throw new Error("SCENE_UNBALANCED_GROUP");
    } else {
      if (n.kind === "group" || n.kind === "table" || drawn.has(n.id))
        throw new Error("SCENE_DUPLICATE_PAINT");
      drawn.add(n.id);
    }
    if (n.kind === "image" && !assets.has(n.assetId))
      throw new Error("SCENE_MISSING_ASSET");
    if (n.kind === "text") {
      const runs = new Map(
        n.text.paragraphs.flatMap((p) => p.runs).map((r) => [r.id, r]),
      );
      if (!nodes.has(n.text.ownerId)) throw new Error("SCENE_MISSING_OWNER");
      for (const l of n.text.lines)
        for (const f of l.fragments) {
          if (
            !runs.has(f.runId) ||
            f.sourceStart < 0 ||
            f.sourceEnd > n.text.sourceText.length ||
            f.sourceEnd <= f.sourceStart
          )
            throw new Error("SCENE_TEXT_RANGE");
        }
    }
  }
  if (stack.length || drawn.size !== nodes.size)
    throw new Error("SCENE_UNPAINTED_NODE");
  const inventory = new Map(s.textInventory.map((t) => [t.sourcePath, t]));
  if (inventory.size !== s.textInventory.length)
    throw new Error("SCENE_DUPLICATE_SOURCE");
  for (const t of s.textInventory)
    if (t.nodeId && !nodes.has(t.nodeId))
      throw new Error("SCENE_INVENTORY_REF");
  for (const n of s.nodes) {
    if (n.kind === "text") {
      const source = inventory.get(n.sourcePath);
      if (source?.nodeId !== n.id || source.sourceText !== n.text.sourceText)
        throw new Error("SCENE_TEXT_PROVENANCE");
    }
    if (n.kind === "table" && n.cells?.some((c) => !nodes.has(c.nodeId)))
      throw new Error("SCENE_TABLE_REFERENCE");
  }
  for (const a of s.assets) {
    if (a.coveredNodeIds.some((id) => !nodes.has(id)))
      throw new Error("SCENE_ASSET_COVERAGE");
    if (a.mediaType === "image/svg+xml") {
      const fallback = assets.get(a.fallbackAssetId!);
      if (
        !fallback ||
        fallback.mediaType !== "image/png" ||
        fallback.fallbackAssetId ||
        JSON.stringify([...a.coveredNodeIds].sort()) !==
          JSON.stringify([...fallback.coveredNodeIds].sort()) ||
        Math.abs(a.width / a.height - fallback.width / fallback.height) >
          (0.02 * a.width) / a.height
      )
        throw new Error("SCENE_ASSET_FALLBACK");
    }
  }
  for (const n of s.nodes)
    if (
      n.kind === "image" &&
      !assets.get(n.assetId)?.coveredNodeIds.includes(n.id)
    )
      throw new Error("SCENE_ASSET_OWNERSHIP");
  if (s.schemaVersion !== "0.3.0")
    for (const item of s.textInventory) {
      const t = item.target!;
      if (t.kind === "hidden") {
        if (item.nodeId !== null || item.reason !== t.reason)
          throw new Error("SCENE_INVENTORY_TARGET");
      } else {
        const n = nodes.get(t.nodeId);
        if (
          item.nodeId !== t.nodeId ||
          item.reason !== (t.kind === "asset" ? "svg-asset" : null)
        )
          throw new Error("SCENE_INVENTORY_TARGET");
        if (
          t.kind === "text"
            ? n?.kind !== "text" ||
              n.sourcePath !== item.sourcePath ||
              n.text.sourceText !== item.sourceText
            : n?.kind !== "image" ||
              n.assetId !== t.assetId ||
              assets.get(t.assetId)?.mediaType !== "image/svg+xml"
        )
          throw new Error("SCENE_INVENTORY_TARGET");
        if (
          t.kind === "asset" &&
          !item.sourcePath.startsWith(n!.sourcePath + "/")
        )
          throw new Error("SCENE_INVENTORY_TARGET");
      }
    }
}

/** Read-only coverage adapter. Historical PNG assets remain PNG; no invented vectors. */
export function textTarget(
  scene: SceneIR,
  item: SceneIR["textInventory"][number],
): NonNullable<typeof item.target> {
  if (item.target) return item.target;
  if (item.nodeId) return { kind: "text", nodeId: item.nodeId };
  if (item.reason === "svg-asset") {
    const image = scene.nodes.find(
      (n) =>
        n.kind === "image" && item.sourcePath.startsWith(n.sourcePath + "/"),
    );
    if (image?.kind === "image")
      return { kind: "asset", nodeId: image.id, assetId: image.assetId };
  }
  return { kind: "hidden", reason: item.reason || "legacy-hidden" };
}
