import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type {
  CSSProperties,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
} from 'react';
import { SceneRenderer, decodeAsset } from '@lumina/scene-renderer';
import type { SceneIR, TextNode } from '@lumina/scene-core';
import { fitCanvas } from '../scene/fitCanvas';
import {
  createHitTest,
  editableText,
  hitsSelectionEdge,
  inputTop,
  inputTypography,
  sameSelection,
  selectionBounds,
  textOffsetAtPoint,
  textLayout,
  translateScene,
  type SelectionTarget,
  type WorkEditorState,
} from './scene-edit';

type Props = {
  workId: string;
  pageId: string;
  scene: SceneIR;
  editor: WorkEditorState;
  selected: SelectionTarget | null;
  editingNodeId: string | null;
  onSelect: (target: SelectionTarget | null) => void;
  onEditStart: (nodeId: string) => void;
  onEditEnd: () => void;
  onMove: (rootId: string, dx: number, dy: number) => void;
  onTextChange: (nodeId: string, sourceText: string) => void;
};

export function SceneCanvas({
  workId,
  pageId,
  scene,
  editor,
  selected,
  editingNodeId,
  onSelect,
  onEditStart,
  onEditEnd,
  onMove,
  onTextChange,
}: Props) {
  const host = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const renderer = useRef<SceneRenderer | null>(null);
  const loadedPage = useRef<string | null>(null);
  const sceneRef = useRef(scene);
  const press = useRef<{
    pointerId: number;
    target: SelectionTarget;
    startClientX: number;
    startClientY: number;
    scale: number;
    threshold: number;
    moved: boolean;
    editOnRelease: boolean;
  } | null>(null);
  const textInput = useRef<HTMLTextAreaElement>(null);
  const caretOffset = useRef<number | null>(null);
  const [scale, setScale] = useState(1);
  const [error, setError] = useState('');
  const [hovered, setHovered] = useState<SelectionTarget | null>(null);
  const [pressed, setPressed] = useState(false);
  const [preview, setPreview] = useState<{ rootId: string; dx: number; dy: number } | null>(null);
  const hitTest = useMemo(() => createHitTest(scene), [scene]);
  const renderedScene = useMemo(() => preview
    ? translateScene(scene, preview.rootId, preview.dx, preview.dy)
    : scene, [scene, preview]);
  const ratio = scene.page.width / scene.page.height;
  sceneRef.current = scene;

  const resize = () => {
    if (!host.current || !renderer.current) return;
    const container = host.current;
    const nextScale = fitCanvas(container.clientWidth, container.clientHeight, sceneRef.current.page);
    const width = Math.max(1, sceneRef.current.page.width * nextScale);
    const height = Math.max(1, sceneRef.current.page.height * nextScale);
    const dpr = Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(8_294_400 / (width * height)));
    setScale(nextScale);
    renderer.current.resize(width, height, dpr);
  };

  useEffect(() => {
    if (!host.current || !canvas.current) return;
    const controller = new AbortController();
    const nextRenderer = new SceneRenderer(canvas.current);
    renderer.current = nextRenderer;
    loadedPage.current = null;
    const observer = new ResizeObserver(resize);
    observer.observe(host.current);
    setError('');
    nextRenderer.load(
      scene,
      async (asset) => {
        const response = await fetch(`/api/v1/works/${workId}/pages/${pageId}/assets/${asset.id}`, { signal: controller.signal });
        if (!response.ok) throw new Error('作品资源不可用');
        const bytes = await response.arrayBuffer();
        const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
          .map((value) => value.toString(16).padStart(2, '0')).join('');
        if (hash !== asset.sha256) throw new Error('作品资源校验失败');
        return decodeAsset(bytes, asset.mediaType, controller.signal);
      },
      async (style, sample) => {
        const font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize}px ${style.fontFamily}`;
        await document.fonts.load(font, sample);
        if (!document.fonts.check(font, sample)) throw new Error(`字体不可用：${style.fontFamily}`);
      },
      controller.signal,
    ).then(() => {
      if (renderer.current !== nextRenderer) return;
      loadedPage.current = pageId;
      nextRenderer.updateScene(sceneRef.current);
      resize();
    }).catch((cause) => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
    });
    return () => {
      controller.abort();
      observer.disconnect();
      if (renderer.current === nextRenderer) {
        renderer.current = null;
        loadedPage.current = null;
      }
      nextRenderer.dispose();
    };
  }, [workId, pageId]);

  useLayoutEffect(() => {
    if (loadedPage.current !== pageId || !renderer.current) return;
    try {
      renderer.current.updateScene(renderedScene);
      setError('');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, [pageId, renderedScene]);

  useLayoutEffect(() => {
    renderer.current?.setHiddenNodeIds(editingNodeId ? [editingNodeId] : []);
    const input = textInput.current;
    if (editingNodeId && input) {
      input.focus({ preventScroll: true });
      const offset = caretOffset.current ?? input.value.length;
      input.setSelectionRange(offset, offset);
    }
    caretOffset.current = null;
  }, [editingNodeId]);

  const clearPress = useCallback(() => {
    const active = press.current;
    if (!active) return;
    press.current = null;
    const element = host.current;
    if (element?.hasPointerCapture(active.pointerId)) element.releasePointerCapture(active.pointerId);
    setPressed(false);
    setPreview(null);
  }, []);

  useEffect(() => {
    window.addEventListener('blur', clearPress);
    return () => window.removeEventListener('blur', clearPress);
  }, [clearPress]);

  function point(event: ReactPointerEvent<HTMLDivElement>) {
    const bounds = canvas.current!.getBoundingClientRect();
    return { x: (event.clientX - bounds.left) / scale, y: (event.clientY - bounds.top) / scale };
  }

  function targetAt(position: { x: number; y: number }) {
    const { x, y } = position;
    const target = hitTest(x, y, scale);
    if (!editingNodeId && selected && selectionFrame &&
      (!target || sameSelection(target, selected)) && hitsSelectionEdge(selectionFrame, x, y, 4 / scale)) {
      return { target: selected, onEdge: true };
    }
    return { target, onEdge: false };
  }

  function updateHovered(target: SelectionTarget | null) {
    setHovered((current) => current?.nodeId === target?.nodeId &&
      current?.selectionRootId === target?.selectionRootId ? current : target);
  }

  function pointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.button !== 0 || !event.isPrimary || press.current || loadedPage.current !== pageId) return;
    const { target, onEdge } = targetAt(point(event));
    event.preventDefault();
    event.currentTarget.focus({ preventScroll: true });
    if (editingNodeId) onEditEnd();
    updateHovered(event.pointerType === 'touch' ? null : target);
    onSelect(target);
    if (!target || target.locked) return;
    press.current = {
      pointerId: event.pointerId,
      target,
      startClientX: event.clientX,
      startClientY: event.clientY,
      scale,
      threshold: event.pointerType === 'touch' ? 8 : 5,
      moved: false,
      editOnRelease: !onEdge && !editingNodeId && target.textNodeId === target.nodeId &&
        sameSelection(target, selected),
    };
    setPressed(true);
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function pointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    if (!press.current) {
      if (event.pointerType !== 'touch' && !editingNodeId && loadedPage.current === pageId) {
        updateHovered(targetAt(point(event)).target);
      }
      return;
    }
    const active = press.current;
    if (active.pointerId !== event.pointerId) return;
    const dx = event.clientX - active.startClientX;
    const dy = event.clientY - active.startClientY;
    if (!active.moved && Math.hypot(dx, dy) < active.threshold) return;
    active.moved = true;
    setPreview({ rootId: active.target.selectionRootId, dx: dx / active.scale, dy: dy / active.scale });
  }

  function pointerUp(event: ReactPointerEvent<HTMLDivElement>) {
    const active = press.current;
    if (active?.pointerId !== event.pointerId) return;
    clearPress();
    const dx = event.clientX - active.startClientX;
    const dy = event.clientY - active.startClientY;
    const position = point(event);
    if (active.moved || Math.hypot(dx, dy) >= active.threshold) {
      if (dx || dy) onMove(active.target.selectionRootId, dx / active.scale, dy / active.scale);
      const withinPage = position.x >= 0 && position.y >= 0 &&
        position.x <= scene.page.width && position.y <= scene.page.height;
      updateHovered(withinPage && event.pointerType !== 'touch' ? active.target : null);
    } else if (active.editOnRelease && active.target.textNodeId &&
      hitTest(position.x, position.y, scale)?.nodeId === active.target.nodeId) {
      const node = scene.nodes.find((item): item is TextNode => item.id === active.target.textNodeId && item.kind === 'text');
      caretOffset.current = node ? textOffsetAtPoint(node, position.x, position.y) : null;
      onEditStart(active.target.textNodeId);
    }
  }

  function pointerCancel(event: ReactPointerEvent<HTMLDivElement>) {
    if (press.current?.pointerId !== event.pointerId) return;
    clearPress();
    setHovered(null);
  }

  function pointerLeave() {
    if (!press.current) setHovered(null);
  }

  function keyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (editingNodeId || event.target !== event.currentTarget) return;
    if (event.key === 'Escape') {
      if (press.current) {
        event.preventDefault();
        clearPress();
        return;
      }
      if (selected) {
        event.preventDefault();
        onSelect(null);
        setHovered(null);
      }
      return;
    }
    if (!selected || selected.locked || press.current || event.ctrlKey || event.metaKey || event.altKey) return;
    if ((event.key === 'Enter' || event.key === 'F2') && selected.textNodeId) {
      event.preventDefault();
      onEditStart(selected.textNodeId);
      return;
    }
    const distance = event.shiftKey ? 10 : 1;
    const delta = event.key === 'ArrowLeft'
      ? { x: -distance, y: 0 }
      : event.key === 'ArrowRight'
        ? { x: distance, y: 0 }
        : event.key === 'ArrowUp'
          ? { x: 0, y: -distance }
          : event.key === 'ArrowDown'
            ? { x: 0, y: distance }
            : null;
    if (!delta) return;
    event.preventDefault();
    onMove(selected.selectionRootId, delta.x, delta.y);
  }

  const editingNode = editingNodeId
    ? scene.nodes.find((item): item is TextNode => item.id === editingNodeId && item.kind === 'text')
    : null;
  const editingText = editingNode ? editableText(editingNode) : '';
  const layout = editingNode && textLayout(scene, editingNode, editor);
  const style = editingNode?.text.paragraphs[0]?.runs[0]?.style;
  const editingTop = layout && style ? inputTop(style, layout) : 0;
  const selectionFrame = selected ? selectionBounds(scene, selected, editor) : null;
  const hoverFrame = hovered && !pressed && !editingNode && !sameSelection(hovered, selected)
    ? selectionBounds(scene, hovered, editor)
    : null;
  const cursor = editingNode
    ? 'default'
    : pressed
      ? 'grabbing'
      : hovered?.locked
        ? 'not-allowed'
        : hovered
          ? 'grab'
          : 'default';

  return (
    <div className="work-canvas-stage" onPointerDown={(event) => {
      if (event.target !== event.currentTarget || event.button !== 0 || !event.isPrimary) return;
      if (editingNodeId) onEditEnd();
      onSelect(null);
      setHovered(null);
      host.current?.focus({ preventScroll: true });
    }}>
      <div
        className="work-canvas-host"
        ref={host}
        tabIndex={0}
        aria-label="作品编辑画布"
        role="group"
        data-gesture={preview ? 'dragging' : pressed ? 'pressed' : undefined}
        style={{ '--page-ratio': ratio, cursor } as CSSProperties}
        onPointerDown={pointerDown}
        onPointerMove={pointerMove}
        onPointerUp={pointerUp}
        onPointerCancel={pointerCancel}
        onLostPointerCapture={pointerCancel}
        onPointerLeave={pointerLeave}
        onKeyDown={keyDown}
      >
        <canvas ref={canvas} aria-label="作品页面预览" />
        {hoverFrame && (
          <div
            className={`scene-hover ${hovered?.locked ? 'locked' : ''}`}
            data-testid="scene-hover"
            style={{ left: hoverFrame.x * scale, top: hoverFrame.y * scale, width: hoverFrame.width * scale, height: hoverFrame.height * scale }}
          />
        )}
        {selectionFrame && !editingNode && (
          <div
            className={`scene-selection ${selected?.locked ? 'locked' : ''}`}
            data-testid="scene-selection"
            style={{
              left: (selectionFrame.x + (preview?.dx ?? 0)) * scale,
              top: (selectionFrame.y + (preview?.dy ?? 0)) * scale,
              width: selectionFrame.width * scale,
              height: selectionFrame.height * scale,
            }}
          >
            {selected?.locked && <span className="scene-lock-label" aria-hidden="true">合成内容 · 无法编辑</span>}
          </div>
        )}
        <span className="scene-selection-status" role="status">
          {editingNode ? '正在编辑文本' : selected?.locked ? '已选中合成内容，无法编辑' :
            selected?.textNodeId ? '已选中文本框，按 Enter 编辑' : selected ? '已选中元素' : ''}
        </span>
        {editingNode && layout && style && (
          <textarea
            ref={textInput}
            className="scene-text-editor"
            aria-label="文字内容"
            value={editingText}
            onChange={(event) => onTextChange(editingNode.id, event.target.value)}
            onKeyDown={(event) => {
              if (event.nativeEvent.isComposing) return;
              if (event.key === 'Escape' || (event.key === 'Enter' && (event.ctrlKey || event.metaKey))) {
                event.preventDefault();
                event.stopPropagation();
                onEditEnd();
                host.current?.focus({ preventScroll: true });
              }
            }}
            onPointerDown={(event) => event.stopPropagation()}
            style={{
              ...inputTypography(style, layout),
              left: layout.box.x * scale,
              top: editingTop * scale,
              width: layout.box.width,
              height: Math.max(layout.lineHeight * Math.max(1, editingNode.text.lines.length), layout.box.y + layout.box.height - editingTop,
                editingNode.frame.y + editingNode.frame.height - editingTop),
              transform: `scale(${scale})`,
              color: style.color,
            }}
          />
        )}
        {error && <div className="work-canvas-error" role="alert">{error}</div>}
      </div>
    </div>
  );
}
