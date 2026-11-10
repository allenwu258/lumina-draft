import { useEffect, useRef, useState } from 'react';
import { SceneRenderer } from '@lumina/scene-renderer';
import { ensureScene, fetchScene, type SceneSummary } from './scene-api';
import './scene.css';

type Props = {
  pageId: string;
  htmlHash: string;
  scene?: SceneSummary;
  number: number;
  onScene: (scene: SceneSummary) => void;
};
export function SceneViewport({ pageId, htmlHash, scene, number, onScene }: Props) {
  const host = useRef<HTMLDivElement>(null),
    canvas = useRef<HTMLCanvasElement>(null);
  const callback = useRef(onScene);
  callback.current = onScene;
  const [phase, setPhase] = useState('loading'),
    [error, setError] = useState(''),
    [retrying, setRetrying] = useState(false);
  const [renderAttempt, setRenderAttempt] = useState(0);
  const request = useRef(0);
  useEffect(() => {
    if (scene?.status && scene.status !== 'NOT_REQUESTED') return;
    const controller = new AbortController();
    ensureScene(pageId, htmlHash, false, controller.signal)
      .then((s) => callback.current(s))
      .catch((e) => {
        if (!controller.signal.aborted) setError(String(e));
      });
    return () => controller.abort();
  }, [pageId, htmlHash, scene?.status]);
  useEffect(() => {
    if (!scene?.scene_id || scene.status !== 'SUCCEEDED' || !canvas.current || !host.current)
      return;
    const controller = new AbortController(),
      token = ++request.current;
    const renderer = new SceneRenderer(canvas.current),
      container = host.current;
    setPhase('loading');
    setError('');
    const resize = () => {
      const scale = Math.min(container.clientWidth / 800, container.clientHeight / 450);
      renderer.resize(800 * scale, 450 * scale, window.devicePixelRatio || 1);
    };
    const observer = new ResizeObserver(resize);
    observer.observe(container);
    window.addEventListener('resize', resize);
    resize();
    fetchScene(scene.scene_id, controller.signal)
      .then(async (data) => {
        await renderer.load(
          data,
          async (asset) => {
            const response = await fetch(`/api/v1/scenes/${scene.scene_id}/assets/${asset.id}`, {
              signal: controller.signal,
            });
            if (!response.ok) throw new Error('场景资源加载失败');
            const bytes = await response.arrayBuffer();
            const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
              .map((v) => v.toString(16).padStart(2, '0'))
              .join('');
            if (hash !== asset.sha256) throw new Error('场景资源校验失败');
            return createImageBitmap(new Blob([bytes], { type: asset.mediaType }));
          },
          async (style, sample) => {
            const font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize}px ${style.fontFamily}`;
            await document.fonts.load(font, sample);
            if (!document.fonts.check(font, sample))
              throw new Error(`字体不可用：${style.fontFamily}`);
          },
          controller.signal,
        );
        if (token !== request.current || controller.signal.aborted) return;
        renderer.render();
        setPhase('ready');
      })
      .catch((e) => {
        if (!controller.signal.aborted && token === request.current) {
          setPhase('failed');
          setError(String(e));
        }
      });
    return () => {
      request.current++;
      controller.abort();
      observer.disconnect();
      window.removeEventListener('resize', resize);
      renderer.dispose();
    };
  }, [scene?.scene_id, scene?.status, renderAttempt]);
  async function retry() {
    if (scene?.status === 'SUCCEEDED') {
      setRenderAttempt((n) => n + 1);
      return;
    }
    setRetrying(true);
    setError('');
    try {
      callback.current(await ensureScene(pageId, htmlHash, true));
    } catch (e) {
      setError(String(e));
    } finally {
      setRetrying(false);
    }
  }
  const failed = scene?.status === 'FAILED' || scene?.status === 'CANCELLED';
  const ready = phase === 'ready' && scene?.status === 'SUCCEEDED';
  const message = failed
    ? '这一页转换失败'
    : scene?.status === 'RUNNING'
      ? '正在转换页面'
      : scene?.status === 'QUEUED'
        ? '等待页面转换'
        : scene?.status === 'SUCCEEDED'
          ? '正在加载场景'
          : '正在准备页面转换';
  return (
    <div
      ref={host}
      className="scene-viewport"
      data-scene-id={scene?.scene_id || ''}
      data-render-status={ready ? 'ready' : error || failed ? 'failed' : 'loading'}
    >
      <canvas
        ref={canvas}
        className="scene-canvas"
        aria-label={`第 ${number} 页新版预览`}
        style={{ visibility: ready ? 'visible' : 'hidden' }}
      />
      {!ready && (
        <div className="empty-canvas scene-message" role="status">
          <span>{error ? '新版预览加载失败' : message}</span>
          {(error || scene?.error) && <small>{error || scene?.error}</small>}
          {(failed || error) && (
            <button className="secondary-button" onClick={retry} disabled={retrying}>
              {retrying ? '正在重试…' : scene?.status === 'SUCCEEDED' ? '重试渲染' : '重试转换'}
            </button>
          )}
          <small>可切换 HTML 预览或源码查看原始页面</small>
        </div>
      )}
      {ready && !!scene?.warning_count && (
        <span className="scene-warning" title="部分 CSS 效果由 Canvas 近似绘制">
          {scene.warning_count} 项渲染提示
        </span>
      )}
    </div>
  );
}
