import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { CanvasSize } from '@lumina/scene-core';
import { fitCanvas } from './scene/fitCanvas';
import { api } from './workbench-api';

export function SlideFrame({url, number, sceneId, canvas}: {
  url:string; number:number; sceneId?:string|null; canvas:CanvasSize;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(0);
  const [preview, setPreview] = useState({ request: '', url: '' });
  const { width, height } = canvas;
  const request = `${url}:${sceneId || ''}`;
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const resize = () => setScale(fitCanvas(element.clientWidth, element.clientHeight, { width, height }));
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    resize();
    return () => observer.disconnect();
  }, [width, height]);
  useEffect(() => {
    const controller = new AbortController();
    const id = url.split('/').at(-2);
    api<{preview_url:string}>(`/page-tasks/${id}/preview-tickets${sceneId?'?scene_id='+encodeURIComponent(sceneId):''}`,
      {method:'POST', signal:controller.signal})
      .then(result => { if (!controller.signal.aborted) setPreview({ request, url: result.preview_url }); })
      .catch(() => { if (!controller.signal.aborted) setPreview({ request, url: '' }); });
    return () => controller.abort();
  }, [request, url, sceneId]);
  return <div ref={ref} className="slide-viewport">
    {preview.request === request && (preview.url
      ? <iframe className="slide-frame" title={`第 ${number} 页预览`} src={preview.url} sandbox=""
          style={{ width, height, transform: `translate(-50%, -50%) scale(${scale})` }}/>
      : <div className="empty-canvas scene-message" role="status">无法加载 HTML 预览</div>)}
  </div>;
}
