import { useEffect, useRef, useState } from 'react';
import { FilePlus2, LoaderCircle, RefreshCw } from 'lucide-react';
import { sceneRequest } from '../scene/scene-api';

type Props = { presentationId: string; batchId?: string | null; sourceHash: string };
type Creation = {
  status: 'PREPARING' | 'NEEDS_RETRY' | 'SUCCEEDED';
  ready_pages: number;
  total_pages: number;
  pages?: { position: number; status: string; error: string | null }[];
  work?: { id: string };
};

export function CreateWorkButton({ presentationId, batchId, sourceHash }: Props) {
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(false);
  const [progress, setProgress] = useState('');
  const [error, setError] = useState('');
  const requestKey = useRef('');
  const controller = useRef<AbortController | null>(null);

  useEffect(() => {
    requestKey.current = '';
    controller.current?.abort();
    setBusy(false);
    setRetry(false);
    setProgress('');
    setError('');
  }, [presentationId, batchId, sourceHash]);

  useEffect(() => () => {
    controller.current?.abort();
  }, []);

  async function create(retryConversion = false) {
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    requestKey.current ||= crypto.randomUUID();
    setBusy(true);
    setRetry(false);
    setError('');
    try {
      while (!request.signal.aborted) {
        const result = await sceneRequest<Creation>(`/presentations/${presentationId}/works`, {
          method: 'POST',
          headers: { 'Idempotency-Key': requestKey.current },
          body: JSON.stringify({
            expected_source_hash: sourceHash,
            ...(batchId ? { batch_id: batchId } : {}),
            retry: retryConversion,
          }),
          signal: request.signal,
        });
        retryConversion = false;
        if (result.status === 'SUCCEEDED' && result.work) {
          window.location.assign(`/edit/${result.work.id}`);
          return;
        }
        const failed = result.pages?.some((page) => ['FAILED', 'CANCELLED'].includes(page.status));
        setProgress(`场景准备 ${result.ready_pages}/${result.total_pages}`);
        if (failed || result.status === 'NEEDS_RETRY') {
          setRetry(true);
          return;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1200));
      }
    } catch (cause) {
      if (!request.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (!request.signal.aborted) setBusy(false);
    }
  }

  return (
    <div className="create-work-action">
      <button className="secondary-button" disabled={busy} onClick={() => void create(retry)}>
        {busy ? <LoaderCircle className="spin" size={15} /> : retry ? <RefreshCw size={15} /> : <FilePlus2 size={15} />}
        {busy ? progress || '正在准备作品' : retry ? '重试页面转换' : '创建作品'}
      </button>
      {error && <small role="alert">{error}</small>}
    </div>
  );
}
