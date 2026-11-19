import { useEffect, useRef, useState } from 'react';
import { Layers3 } from 'lucide-react';
import type { CanvasSize } from '@lumina/scene-core';
import { PresentationExport, type ExportSource, type ExportSummary } from './export/PresentationExport';
import { PageStatusIcon, pageStatusLabel } from './PageStatus';
import { SlideFrame } from './SlideFrame';
import { api, type ArtifactSelection, type CardAction, type Page } from './workbench-api';

export type DeckDetails = {
  batch_id?: string | null;
  batch_run?: {id: string; status: string; error?: string | null} | null;
  pages: Page[];
  export_source?: ExportSource;
  latest_export?: ExportSummary | null;
};

type Props = {
  presentationId: string;
  messageId: string;
  batchId?: string;
  runId?: string;
  label: string;
  status: string;
  pageCount: number;
  canvas: CanvasSize;
  update: number;
  legacy?: DeckDetails;
  canSubmit: boolean;
  submit: (text: string, action: CardAction, requestId: string) => Promise<void>;
  cancel: (runId: string) => Promise<void>;
  reference: (selection: ArtifactSelection) => void;
};

function PagePreview({ page, canvas }: {page: Page; canvas: CanvasSize}) {
  const host = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    if (page.status !== 'SUCCEEDED' || !host.current) return;
    const observer = new IntersectionObserver(([entry]) => setVisible(entry.isIntersecting), { rootMargin: '220px 0px' });
    observer.observe(host.current);
    return () => observer.disconnect();
  }, [page.status]);
  const ready = page.status === 'SUCCEEDED' && page.html_hash;
  return <div ref={host} className="agent-page-preview" style={{ aspectRatio: canvas.width / canvas.height }}>
    {ready && visible && <SlideFrame key={page.html_hash} canvas={canvas} url={'/api/v1/page-tasks/' + page.id + '/html'} number={page.page_number} sceneId={page.scene?.scene_id}/>}
    {!ready && <div className="agent-page-placeholder"><PageStatusIcon status={page.status}/><span>{pageStatusLabel(page.status)}</span></div>}
  </div>;
}

export function AgentDeckCard({ presentationId, messageId, batchId, runId, label, status, pageCount, canvas, update, legacy, canSubmit, submit, cancel, reference }: Props) {
  const [details, setDetails] = useState<DeckDetails>();
  const [readError, setReadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const requestId = useRef<string | undefined>(undefined);
  const current = batchId ? details : legacy;
  const attempt = current?.batch_run;
  const effectiveRun = runId || attempt?.id;
  const pages = current?.pages || [];
  const complete = pages.filter(page => page.status === 'SUCCEEDED').length;
  const state = status || attempt?.status || '';
  const generating = ['STREAMING', 'QUEUED', 'RUNNING', 'CANCEL_REQUESTED'].includes(state);
  const failed = ['FAILED', 'PARTIAL_FAILED', 'CANCELLED'].includes(state);
  useEffect(() => {
    if (!batchId) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      api<DeckDetails>('/presentations/' + presentationId + '/batches/' + batchId + '?compact=true', { signal: controller.signal })
        .then(result => {
          if (controller.signal.aborted) return;
          setDetails(result);
          setReadError('');
        })
        .catch(cause => { if (!controller.signal.aborted) setReadError(String(cause)); });
    }, 100);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [presentationId, batchId, runId, status, update, reload]);

  async function runAction(retry: boolean) {
    if (busy) return;
    setBusy(true);
    setActionError('');
    try {
      if (retry) {
        requestId.current ??= crypto.randomUUID();
        await submit('重试' + label + '的失败页面', {type:'retry_deck', artifact_message_id:messageId}, requestId.current);
        requestId.current = undefined;
      } else if (effectiveRun) await cancel(effectiveRun);
    } catch (cause) {
      setActionError(String(cause));
    } finally {
      setBusy(false);
    }
  }

  return <article className="agent-artifact agent-deck-card" aria-label={label}>
    <header className="agent-artifact-header">
      <span className="special-card-icon"><Layers3 size={16}/></span>
      <div><h2>{label}</h2><p role="status">{complete}/{pageCount || pages.length} 页 · {generating ? '正在生成页面' : state === 'CANCELLED' ? '已取消' : failed ? '有页面生成失败' : '生成已完成'}</p></div>
      {batchId && <button className="secondary-button" onClick={() => reference({artifact_message_id:messageId, label})}>引用到对话</button>}
    </header>
    {pages.length > 0 ? <ol className="agent-deck-pages" aria-label="PPT 页面">
      {pages.map(page => <li key={page.id}>
        <div className="agent-slide-meta">
          <h3>{page.page_number}. {page.title}</h3>
          {batchId && page.html_hash && <button className="secondary-button" onClick={() => reference({
            artifact_message_id:messageId, slide_id:page.slide_id, source_ref:page.html_hash!, label:'第 ' + page.page_number + ' 页',
          })}>引用此页</button>}
        </div>
        <PagePreview page={page} canvas={canvas}/>
        {page.error && <p className="agent-card-status" role="alert">{page.error}</p>}
      </li>)}
    </ol> : <p className="agent-card-status">{batchId ? '正在读取页面…' : '此历史记录没有可关联的页面。'}</p>}
    {readError && <div className="agent-card-status" role="alert">{readError} <button className="secondary-button" onClick={() => setReload(value => value + 1)}>重读页面</button></div>}
    {actionError && <p className="agent-card-status" role="alert">{actionError}</p>}
    {attempt?.error && failed && <p className="agent-card-status">{attempt.error}</p>}
    <footer className="agent-artifact-actions">
      {generating && effectiveRun && <button className="secondary-button" disabled={busy || state === 'CANCEL_REQUESTED'} onClick={() => void runAction(false)}>取消生成</button>}
      {failed && batchId && <button className="secondary-button" disabled={busy || !canSubmit} onClick={() => void runAction(true)}>重试失败页</button>}
      {current?.export_source && <PresentationExport key={batchId + '/' + current.export_source.source_hash}
        presentationId={presentationId} batchId={batchId} source={current.export_source} latest={current.latest_export}/>}
    </footer>
  </article>;
}
