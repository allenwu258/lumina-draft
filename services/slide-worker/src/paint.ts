import type { GroupNode, SceneIR, SceneNode } from "@lumina/scene-core";

type Tree = { id: string; children: (Tree | string)[] };
/** Resolve stacking contexts, including z-index descendants of ordinary containers.
 * Overflow clips travel with promoted content; opacity remains a real isolated group.
 */
export function orderPaint(
  nodes: SceneNode[],
  ops: SceneIR["paintOps"],
  groups: { id: string; context: boolean; positioned: boolean; z: number }[],
): SceneIR["paintOps"] {
  const meta = new Map(groups.map((g) => [g.id, g])),
    byId = new Map(nodes.map((n) => [n.id, n]));
  const stack: Tree[] = [],
    roots: Tree[] = [];
  for (const op of ops) {
    if (op.op === "begin") {
      const t = { id: op.nodeId, children: [] };
      if (stack.length) stack.at(-1)!.children.push(t);
      else roots.push(t);
      stack.push(t);
    } else if (op.op === "end") stack.pop();
    else stack.at(-1)!.children.push(op.nodeId);
  }
  const result: SceneIR["paintOps"] = [];
  function wrap(t: Tree, ancestors: Tree[]): Tree {
    let wrapped = t;
    for (const ancestor of [...ancestors].reverse()) {
      const group = byId.get(ancestor.id) as GroupNode;
      if (group.clip) {
        const id = group.id + "_clip_for_" + t.id,
          node: GroupNode = {
            id,
            sourcePath: group.sourcePath,
            frame: group.frame,
            kind: "group",
            tagName: "clip",
            opacity: 1,
            clip: group.clip,
          };
        nodes.push(node);
        byId.set(id, node);
        wrapped = { id, children: [wrapped] };
      }
    }
    return wrapped;
  }
  function context(root: Tree) {
    const negative: { tree: Tree; z: number; order: number }[] = [],
      zero: { tree: Tree; order: number }[] = [],
      positive: { tree: Tree; z: number; order: number }[] = [];
    let sequence = 0;
    function collect(parent: Tree, ancestors: Tree[]): Tree {
      const kept: (Tree | string)[] = [];
      for (const child of parent.children) {
        if (typeof child === "string") {
          kept.push(child);
          continue;
        }
        const m = meta.get(child.id),
          order = sequence++;
        if (m?.context) {
          const entry = { tree: wrap(child, ancestors), z: m.z, order };
          if (m.z < 0) negative.push(entry);
          else if (m.z > 0) positive.push(entry);
          else zero.push(entry);
        } else {
          const normalized = collect(child, [...ancestors, child]);
          if (m?.positioned)
            zero.push({ tree: wrap(normalized, ancestors), order });
          else kept.push(normalized);
        }
      }
      return { id: parent.id, children: kept };
    }
    const normal = collect(root, []);
    const initial: string[] = [];
    while (
      typeof normal.children[0] === "string" &&
      byId.get(normal.children[0])?.sourcePath === byId.get(root.id)?.sourcePath
    )
      initial.push(normal.children.shift() as string);
    const sorted = (entries: { tree: Tree; z: number; order: number }[]) =>
      entries.sort((a, b) => a.z - b.z || a.order - b.order).map((e) => e.tree);
    plain(
      {
        id: root.id,
        children: [
          ...initial,
          ...sorted(negative),
          ...normal.children,
          ...zero.sort((a, b) => a.order - b.order).map((e) => e.tree),
          ...sorted(positive),
        ],
      },
      true,
    );
  }
  function plain(t: Tree, skipContext = false) {
    if (!skipContext && meta.get(t.id)?.context) {
      context(t);
      return;
    }
    result.push({ op: "begin", nodeId: t.id });
    for (const c of t.children)
      if (typeof c === "string") result.push({ op: "draw", nodeId: c });
      else plain(c);
    result.push({ op: "end", nodeId: t.id });
  }
  for (const root of roots) context(root);
  return result;
}
