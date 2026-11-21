import { useEffect, useRef, useState } from 'react';
import type { CSSProperties, PointerEvent as ReactPointerEvent } from 'react';
import { SceneRenderer, decodeAsset } from '@lumina/scene-renderer';
import type { SceneIR, TextNode } from '@lumina/scene-core';
import { fitCanvas } from '../scene/fitCanvas';
import {
  hitTest,
  inputTop,
  inputTypography,
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
  onSceneChange: (scene: SceneIR) => void;
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
  onSceneChange,
  onTextChange,
}: Props) {
  const host = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const renderer = useRef<SceneRenderer | null>(null);
  const loadedPage = useRef<string | null>(null);
  const sceneRef = useRef(scene);
  const drag = useRef<{ target: SelectionTarget; startX: number; startY: number; base: SceneIR; moved: boolean } | null>(null);
  const [scale, setScale] = useState(1);
  const [error, setError] = useState('');
  const [editingValue, setEditingValue] = useState('');
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
      nextRenderer.setHiddenNodeIds(editingNodeId ? [editingNodeId] : []);
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

  useEffect(() => {
    if (loadedPage.current !== pageId || !renderer.current) return;
    try {
      renderer.current.updateScene(scene);
      setError('');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, [pageId, scene]);

  useEffect(() => {
    renderer.current?.setHiddenNodeIds(editingNodeId ? [editingNodeId] : []);
    if (editingNodeId) {
      const node = scene.nodes.find((item): item is TextNode => item.id === editingNodeId && item.kind === 'text');
      setEditingValue(node?.text.sourceText || '');
    }
  }, [editingNodeId]);

  function point(event: ReactPointerEvent<HTMLDivElement>) {
    const bounds = host.current!.getBoundingClientRect();
    return { x: (event.clientX - bounds.left) / scale, y: (event.clientY - bounds.top) / scale };
  }

  function pointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (editingNodeId) return;
    const position = point(event);
    const target = hitTest(sceneRef.current, position.x, position.y);
    onSelect(target);
    if (!target || target.locked) return;
    drag.current = { target, startX: position.x, startY: position.y, base: sceneRef.current, moved: false };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function pointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    if (!drag.current) return;
    const position = point(event);
    const dx = position.x - drag.current.startX;
    const dy = position.y - drag.current.startY;
    if (!drag.current.moved && Math.hypot(dx, dy) * scale < 3) return;
    drag.current.moved = true;
    onSceneChange(translateScene(drag.current.base, drag.current.target.moveRootId, dx, dy));
  }

  function pointerUp(event: ReactPointerEvent<HTMLDivElement>) {
    if (drag.current && event.currentTarget.hasPointerCapture(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId);
    drag.current = null;
  }

  function doubleClick(event: ReactPointerEvent<HTMLDivElement>) {
    const position = point(event);
    const target = hitTest(sceneRef.current, position.x, position.y);
    if (target?.textNodeId && !target.locked) {
      onSelect(target);
      onEditStart(target.textNodeId);
    }
  }

  const selectionFrame = scene.nodes.find((item) => item.id === selected?.nodeId)?.frame;
  const editingNode = editingNodeId
    ? scene.nodes.find((item): item is TextNode => item.id === editingNodeId && item.kind === 'text')
    : null;
  const layout = editingNode && textLayout(scene, editingNode, editor);
  const style = editingNode?.text.paragraphs[0]?.runs[0]?.style;
  const editingTop = layout && style ? inputTop(style, layout) : 0;

  return (
    <div className="work-canvas-stage">
      <div
        className="work-canvas-host"
        ref={host}
        style={{ '--page-ratio': ratio } as CSSProperties}
        onPointerDown={pointerDown}
        onPointerMove={pointerMove}
        onPointerUp={pointerUp}
        onPointerCancel={pointerUp}
        onDoubleClick={doubleClick}
      >
        <canvas ref={canvas} aria-label="作品页面预览" />
        {selectionFrame && !editingNode && (
          <div
            className={`scene-selection ${selected?.locked ? 'locked' : ''}`}
            data-testid="scene-selection"
            style={{ left: selectionFrame.x * scale, top: selectionFrame.y * scale, width: selectionFrame.width * scale, height: selectionFrame.height * scale }}
          />
        )}
        {editingNode && layout && style && (
          <textarea
            className="scene-text-editor"
            aria-label="文字内容"
            value={editingValue}
            autoFocus
            onChange={(event) => {
              setEditingValue(event.target.value);
              onTextChange(editingNode.id, event.target.value);
            }}
            onKeyDown={(event) => {
              if (event.nativeEvent.isComposing) return;
              if (event.key === 'Escape') {
                event.preventDefault();
                onEditEnd();
              }
              if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
                event.preventDefault();
                onEditEnd();
              }
            }}
            onPointerDown={(event) => event.stopPropagation()}
            style={{
              ...inputTypography(style, layout),
              left: layout.box.x * scale,
              top: editingTop * scale,
              width: layout.box.width,
              height: Math.max(layout.lineHeight, layout.box.y + layout.box.height - editingTop,
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
