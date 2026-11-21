import type {
  Rect,
  GroupNode,
  SceneIR,
  TextNode,
  TextStyle,
  TextAlign,
  WorkTextLayout,
} from '@lumina/scene-core';

export type { TextAlign };
type TextLayoutEntry = { align: TextAlign } & Partial<Omit<WorkTextLayout, 'align'>>;
export type WorkEditorState = {
  version: 1;
  text: Record<string, TextLayoutEntry>;
};
export type SelectionTarget = {
  nodeId: string;
  selectionRootId: string;
  textNodeId: string | null;
  locked: boolean;
};

const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

export function normalizeEditor(value: unknown): WorkEditorState {
  const source = value && typeof value === 'object' ? value as Partial<WorkEditorState> : {};
  const text: WorkEditorState['text'] = {};
  for (const [nodeId, item] of Object.entries(source.text || {})) {
    if (item?.align === 'left' || item?.align === 'center' || item?.align === 'right')
      text[nodeId] = item;
  }
  return { version: 1, text };
}

function nodeMap(scene: SceneIR) {
  return new Map(scene.nodes.map((node) => [node.id, node]));
}

function rootId(scene: SceneIR): string | null {
  return scene.paintOps.find((op) => op.op === 'begin')?.nodeId || null;
}

function drawEntries(scene: SceneIR) {
  const stack: string[] = [];
  const entries: { nodeId: string; ancestors: string[] }[] = [];
  for (const op of scene.paintOps) {
    if (op.op === 'begin') stack.push(op.nodeId);
    else if (op.op === 'end') stack.pop();
    else entries.push({ nodeId: op.nodeId, ancestors: [...stack] });
  }
  return entries;
}

function contains(frame: Rect, x: number, y: number, padding = 0) {
  return x >= frame.x - padding && y >= frame.y - padding &&
    x <= frame.x + frame.width + padding && y <= frame.y + frame.height + padding;
}

function textContains(node: TextNode, x: number, y: number, padding: number) {
  return node.text.lines.some((line) =>
    line.fragments.some((fragment) => contains(fragment.frame, x, y, padding)),
  ) || contains(node.frame, x, y, padding);
}

export function createHitTest(scene: SceneIR) {
  const nodes = nodeMap(scene);
  const root = rootId(scene);
  const entries = drawEntries(scene);
  const groupContents = new Map<string, string[]>();
  for (const { nodeId, ancestors } of entries) {
    const kind = nodes.get(nodeId)?.kind;
    if (kind !== 'text' && kind !== 'image') continue;
    for (const id of ancestors) {
      const contents = groupContents.get(id);
      if (contents) contents.push(nodeId);
      else groupContents.set(id, [nodeId]);
    }
  }
  const lockedAssets = new Set(scene.assets.filter(
    (asset) => asset.bakedEffects.includes('full-page-composite'),
  ).map((asset) => asset.id));
  const targets = entries.reverse().flatMap(({ nodeId, ancestors }) => {
    const node = nodes.get(nodeId);
    if (!node || node.kind === 'group' || node.kind === 'table') return [];
    const groups = ancestors.map((id) => nodes.get(id)).filter(
      (item): item is GroupNode => item?.kind === 'group' || item?.kind === 'table',
    );
    if (groups.some((group) => group.opacity === 0)) return [];
    const parentId = ancestors.at(-1);
    const owner = nodes.get(node.kind === 'text' ? node.text.ownerId : parentId || '');
    const contents = owner && groupContents.get(owner.id);
    const textId = contents?.length === 1 ? contents[0] : undefined;
    const grouped = owner?.kind === 'group' && owner.id !== root &&
      ancestors.includes(owner.id) && textId !== undefined && nodes.get(textId)?.kind === 'text' &&
      (node.kind === 'text' || (node.kind === 'shape' && owner.sourcePath === node.sourcePath));
    const target: SelectionTarget = {
      nodeId,
      selectionRootId: grouped ? owner.id : nodeId,
      textNodeId: grouped ? textId : node.kind === 'text' ? nodeId : null,
      locked: node.kind === 'image' && lockedAssets.has(node.assetId),
    };
    return [{ node, target, groups }];
  });

  return (x: number, y: number, scale: number): SelectionTarget | null => {
    if (x < 0 || y < 0 || x > scene.page.width || y > scene.page.height) return null;
    for (const { node, target, groups } of targets) {
      const hit = node.kind === 'text'
        ? textContains(node, x, y, 3 / scale)
        : contains(node.frame, x, y, node.kind === 'image' ? 0 : 2 / scale);
      if (hit && groups.every((group) => !group.clip || contains(group.clip.frame, x, y))) return target;
    }
    return null;
  };
}

export function sameSelection(left: SelectionTarget | null, right: SelectionTarget | null) {
  return !!left && !!right && left.selectionRootId === right.selectionRootId;
}

export function hitsSelectionEdge(frame: Rect, x: number, y: number, tolerance: number) {
  return contains(frame, x, y, tolerance) && !contains(frame, x, y, -tolerance);
}

export function selectionBounds(scene: SceneIR, target: SelectionTarget, editor: WorkEditorState): Rect | null {
  const node = scene.nodes.find((item) => item.id === target.selectionRootId) ||
    scene.nodes.find((item) => item.id === target.nodeId);
  if (!node) return null;
  if (node.kind === 'text') return textLayout(scene, node, editor).box;
  return node.frame;
}

function descendants(scene: SceneIR, root: string) {
  const children = new Map<string, string[]>();
  const stack: string[] = [];
  for (const op of scene.paintOps) {
    if (op.op === 'begin') {
      if (stack.length) children.set(stack.at(-1)!, [...(children.get(stack.at(-1)!) || []), op.nodeId]);
      stack.push(op.nodeId);
    } else if (op.op === 'end') stack.pop();
    else if (stack.length) children.set(stack.at(-1)!, [...(children.get(stack.at(-1)!) || []), op.nodeId]);
  }
  const result = new Set<string>();
  const visit = (id: string) => {
    if (result.has(id)) return;
    result.add(id);
    for (const child of children.get(id) || []) visit(child);
  };
  visit(root);
  return result;
}

function moveRect(rect: Rect, dx: number, dy: number): Rect {
  return { ...rect, x: rect.x + dx, y: rect.y + dy };
}

export function translateScene(scene: SceneIR, root: string, dx: number, dy: number): SceneIR {
  if (!dx && !dy) return scene;
  const ids = descendants(scene, root);
  const nodes = scene.nodes.map((node) => {
    if (!ids.has(node.id)) return node;
    const frame = moveRect(node.frame, dx, dy);
    if (node.kind === 'group' || node.kind === 'table') {
      return {
        ...node,
        frame,
        clip: node.clip && { ...node.clip, frame: moveRect(node.clip.frame, dx, dy) },
        ...(node.cells && { cells: node.cells.map((cell) => ({ ...cell, frame: moveRect(cell.frame, dx, dy) })) }),
      };
    }
    if (node.kind !== 'text') return { ...node, frame };
    const lines = node.text.lines.map((line) => ({
      ...line,
      frame: moveRect(line.frame, dx, dy),
      baseline: line.baseline + dy,
      fragments: line.fragments.map((fragment) => ({
        ...fragment,
        frame: moveRect(fragment.frame, dx, dy),
        baseline: fragment.baseline + dy,
      })),
    }));
    return { ...node, frame, text: { ...node.text, lines } };
  });
  return { ...scene, nodes };
}

function fontString(style: TextStyle) {
  return `${style.fontStyle} ${style.fontWeight} ${style.fontSize}px ${style.fontFamily}`;
}

function firstStyle(node: TextNode): TextStyle {
  return node.text.paragraphs[0]?.runs[0]?.style || {
    fontFamily: 'Arial', fontSize: 24, fontWeight: '400', fontStyle: 'normal',
    color: '#000000', letterSpacing: 0, decoration: 'none',
  };
}

export function textOffsetAtPoint(node: TextNode, x: number, y: number): number {
  const distance = (position: number, start: number, size: number) =>
    Math.max(start - position, position - start - size, 0);
  let nearest: TextNode['text']['lines'][number]['fragments'][number] | undefined;
  let smallestY = Infinity;
  let smallestX = Infinity;
  for (const line of node.text.lines) {
    const dy = distance(y, line.frame.y, line.frame.height);
    if (dy > smallestY) continue;
    for (const fragment of line.fragments) {
      const dx = distance(x, fragment.frame.x, fragment.frame.width);
      if (dy === smallestY && dx >= smallestX) continue;
      nearest = fragment;
      smallestX = dx;
      smallestY = dy;
    }
  }
  if (!nearest) return node.text.sourceText.length;
  if (x <= nearest.frame.x) return nearest.sourceStart;
  if (x >= nearest.frame.x + nearest.frame.width) return nearest.sourceEnd;
  const style = firstStyle(node);
  const measure = document.createElement('canvas').getContext('2d')!;
  measure.font = fontString(style);
  measure.letterSpacing = `${style.letterSpacing}px`;
  const text = node.text.sourceText.slice(nearest.sourceStart, nearest.sourceEnd);
  const position = (x - nearest.frame.x) / nearest.frame.width * measure.measureText(text).width;
  const segments = new Intl.Segmenter(undefined, { granularity: 'grapheme' });
  let previousWidth = 0;
  for (const { index, segment } of segments.segment(text)) {
    const width = measure.measureText(text.slice(0, index + segment.length)).width;
    if (position < (previousWidth + width) / 2) return nearest.sourceStart + index;
    previousWidth = width;
  }
  return nearest.sourceEnd;
}

export function textLayout(scene: SceneIR, node: TextNode, editor: WorkEditorState): WorkTextLayout {
  const saved = editor.text[node.id];
  if (saved?.box && saved.baseline !== undefined && saved.lineHeight)
    return { align: saved.align, box: saved.box, baseline: saved.baseline, lineHeight: saved.lineHeight };

  const owner = scene.nodes.find((item) => item.id === node.text.ownerId);
  const sharesOwner = scene.nodes.some(item => item.kind === 'text' &&
    item.id !== node.id && item.text.ownerId === node.text.ownerId);
  const container = owner?.kind === 'group' && owner.id !== rootId(scene) && !sharesOwner
    ? owner.frame : node.frame;
  const line = node.text.lines[0];
  const style = firstStyle(node);
  const lines = node.text.lines.filter(item => item.frame.width > 0);
  const left = lines.length ? Math.min(...lines.map(item => item.frame.x)) : node.frame.x;
  const center = container.x + container.width / 2;
  const right = container.x + container.width;
  const align = saved?.align ?? (
    lines.length > 1 && lines.every(item => Math.abs(item.frame.x + item.frame.width / 2 - center) < 1)
      ? 'center'
      : lines.every(item => Math.abs(item.frame.x + item.frame.width - right) < 1) && left > container.x + 1
        ? 'right' : 'left'
  );
  const boxX = align === 'left' ? left : container.x;
  const lineHeight = node.text.lines[1] && line
    ? node.text.lines[1].baseline - line.baseline
    : Math.min(container.height || Infinity, style.fontSize * 1.35);
  return {
    align,
    box: { x: boxX, y: container.y, width: Math.max(1, right - boxX, node.frame.width), height: container.height },
    baseline: line?.baseline ?? node.frame.y + style.fontSize,
    lineHeight: lineHeight > 0 ? lineHeight : style.fontSize * 1.25,
  };
}

export function translateTextLayouts(editor: WorkEditorState, before: SceneIR, after: SceneIR): WorkEditorState {
  const oldNodes = nodeMap(before);
  const text = { ...editor.text };
  for (const node of after.nodes) {
    const entry = text[node.id];
    const previous = oldNodes.get(node.id);
    if (!entry?.box || entry.baseline === undefined || !previous) continue;
    const dx = node.frame.x - previous.frame.x;
    const dy = node.frame.y - previous.frame.y;
    if (dx || dy) text[node.id] = { ...entry, box: moveRect(entry.box, dx, dy), baseline: entry.baseline + dy };
  }
  return { ...editor, text };
}

export function inputTypography(style: TextStyle, layout: WorkTextLayout) {
  return {
    fontFamily: style.fontFamily,
    fontSize: `${style.fontSize}px`,
    fontWeight: style.fontWeight,
    fontStyle: style.fontStyle,
    letterSpacing: `${style.letterSpacing}px`,
    lineHeight: `${layout.lineHeight}px`,
    textAlign: layout.align,
    whiteSpace: 'pre-wrap',
    overflowWrap: 'break-word',
  } as const;
}

export function inputTop(style: TextStyle, layout: WorkTextLayout) {
  const measure = document.createElement('canvas').getContext('2d')!;
  measure.font = fontString(style);
  const { fontBoundingBoxAscent: ascent, fontBoundingBoxDescent: descent } = measure.measureText('汉Ag');
  return layout.baseline - ascent - (layout.lineHeight - ascent - descent) / 2;
}

function round(value: number) {
  return Math.round(value * 100) / 100;
}

export function reflowTextNode(
  scene: SceneIR,
  nodeId: string,
  sourceText: string,
  layout: WorkTextLayout,
  styleOverride?: Partial<TextStyle>,
): SceneIR {
  const result = clone(scene);
  const node = result.nodes.find((item): item is TextNode => item.id === nodeId && item.kind === 'text');
  if (!node) return scene;
  const style = { ...firstStyle(node), ...styleOverride };
  const { box, baseline: firstBaseline, lineHeight } = layout;
  const measure = document.createElement('canvas').getContext('2d')!;
  measure.font = fontString(style);
  measure.letterSpacing = `${style.letterSpacing}px`;
  const mirror = document.createElement('div');
  Object.assign(mirror.style, inputTypography(style, layout), {
    position: 'fixed', top: '0', left: '0', visibility: 'hidden',
    width: `${box.width}px`, margin: '0', border: '0', padding: '0',
  });
  const source = document.createTextNode(sourceText);
  mirror.append(source);
  document.body.append(mirror);
  const segmenter = new Intl.Segmenter(undefined, { granularity: 'grapheme' });
  const lines: TextNode['text']['lines'] = [];
  const runId = node.id + '_layout_run';
  const byRow = new Map<number, { start: number; end: number; rect: DOMRect }>();
  const range = document.createRange();
  const top = inputTop(style, layout);
  try {
    const origin = mirror.getBoundingClientRect();
    const probe = document.createElement('span');
    probe.style.cssText = 'display:inline-block;width:0;height:0;vertical-align:baseline';
    const firstLine = mirror.cloneNode(false) as HTMLDivElement;
    firstLine.append('汉', probe);
    document.body.append(firstLine);
    const ascent = probe.getBoundingClientRect().top - firstLine.getBoundingClientRect().top;
    firstLine.remove();
    const { fontBoundingBoxAscent } = measure.measureText('汉Ag');
    const firstGlyphTop = ascent - fontBoundingBoxAscent;
    for (const { segment, index } of segmenter.segment(sourceText)) {
      if (segment === '\n') continue;
      range.setStart(source, index);
      range.setEnd(source, index + segment.length);
      const rect = range.getBoundingClientRect();
      if (rect.width <= 0) continue;
      const row = Math.round((rect.top - origin.top - firstGlyphTop) / lineHeight);
      const existing = byRow.get(row);
      if (existing) existing.end = index + segment.length;
      else byRow.set(row, { start: index, end: index + segment.length, rect });
    }
    for (const [row, item] of byRow) {
      const displayText = sourceText.slice(item.start, item.end);
      const measuredWidth = round(measure.measureText(displayText).width);
      const baseline = round(firstBaseline + row * lineHeight);
      const frame = {
        x: round(box.x + item.rect.left - origin.left),
        y: round(top + item.rect.top - origin.top),
        width: measuredWidth,
        height: round(item.rect.height),
      };
      lines.push({
        frame, baseline,
        fragments: [{ runId, sourceStart: item.start, sourceEnd: item.end, displayText, frame, baseline, measuredWidth }],
      });
    }
  } finally {
    mirror.remove();
  }
  const left = lines.length ? Math.min(...lines.map(line => line.frame.x)) : box.x;
  const upper = lines.length ? Math.min(...lines.map(line => line.frame.y)) : top;
  const right = lines.length ? Math.max(...lines.map(line => line.frame.x + line.frame.width)) : box.x + 1;
  const lower = lines.length ? Math.max(...lines.map(line => line.frame.y + line.frame.height)) : top + lineHeight;
  const overflow = lower > box.y + box.height + 1 || right > box.x + box.width + 1;
  node.frame = { x: left, y: upper, width: right - left, height: lower - upper };
  node.text = {
    ownerId: node.text.ownerId,
    sourceText,
    paragraphs: [{ runs: [{ id: runId, sourceStart: 0, sourceEnd: sourceText.length, style }] }],
    lines,
  };
  result.textInventory = result.textInventory.map((item) =>
    item.nodeId === nodeId ? { ...item, sourceText } : item,
  );
  result.diagnostics = result.diagnostics.filter((item) => item.nodeId !== nodeId || item.code !== 'EDITOR_TEXT_OVERFLOW');
  if (overflow) {
    result.diagnostics.push({
      code: 'EDITOR_TEXT_OVERFLOW',
      message: '文字超出文本框范围',
      sourcePath: node.sourcePath,
      nodeId,
      severity: 'warning',
      details: { lineCount: lines.length, boxHeight: box.height },
    });
  }
  return result;
}

export function selectedTextStyle(scene: SceneIR, nodeId: string | null): TextStyle | null {
  const node = scene.nodes.find((item): item is TextNode => item.id === nodeId && item.kind === 'text');
  return node ? firstStyle(node) : null;
}
