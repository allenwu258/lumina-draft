import { useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { SceneRenderer, decodeAsset } from '@lumina/scene-renderer';
import type { SceneIR } from '@lumina/scene-core';
import { fitCanvas } from '../scene/fitCanvas';

type Props = { workId: string; pageId: string; scene: SceneIR };

export function SceneCanvas({ workId, pageId, scene }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [error, setError] = useState('');
  const ratio = scene.page.width / scene.page.height;

  useEffect(() => {
    if (!host.current || !canvas.current) return;
    const controller = new AbortController();
    const renderer = new SceneRenderer(canvas.current);
    const container = host.current;
    const resize = () => {
      const scale = fitCanvas(container.clientWidth, container.clientHeight, scene.page);
      const width = Math.max(1, scene.page.width * scale);
      const height = Math.max(1, scene.page.height * scale);
      const dpr = Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(8_294_400 / (width * height)));
      renderer.resize(width, height, dpr);
    };
    const observer = new ResizeObserver(resize);
    observer.observe(container);
    resize();
    setError('');
    renderer.load(
      scene,
      async (asset) => {
        const response = await fetch(
          `/api/v1/works/${workId}/pages/${pageId}/assets/${asset.id}`,
          { signal: controller.signal },
        );
        if (!response.ok) throw new Error('作品资源不可用');
        const bytes = await response.arrayBuffer();
        const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
          .map((value) => value.toString(16).padStart(2, '0'))
          .join('');
        if (hash !== asset.sha256) throw new Error('作品资源校验失败');
        return decodeAsset(bytes, asset.mediaType, controller.signal);
      },
      async (style, sample) => {
        const font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize}px ${style.fontFamily}`;
        await document.fonts.load(font, sample);
        if (!document.fonts.check(font, sample)) throw new Error(`字体不可用：${style.fontFamily}`);
      },
      controller.signal,
    ).then(() => renderer.render()).catch((cause) => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
    });
    return () => {
      controller.abort();
      observer.disconnect();
      renderer.dispose();
    };
  }, [workId, pageId, scene]);

  return (
    <div className="work-canvas-stage">
      <div className="work-canvas-host" ref={host} style={{ '--page-ratio': ratio } as CSSProperties}>
        <canvas ref={canvas} aria-label="作品页面预览" />
        {error && <div className="work-canvas-error" role="alert">{error}</div>}
      </div>
    </div>
  );
}
