import { Ajv } from "ajv";
import schema from "../contracts/scene-ir/0.3.0.schema.json" with { type: "json" };
import type { SceneIR } from "./types.js";
export * from "./types.js";

const check = new Ajv({ allErrors: true, strict: false }).compile(schema);
export function assertScene(value: unknown): asserts value is SceneIR {
  if (!check(value))
    throw new Error(
      `SCENE_SCHEMA: ${JSON.stringify(check.errors)?.slice(0, 1200)}`,
    );
  const s = value as SceneIR;
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
  for (const a of s.assets)
    if (a.coveredNodeIds.some((id) => !nodes.has(id)))
      throw new Error("SCENE_ASSET_COVERAGE");
}
