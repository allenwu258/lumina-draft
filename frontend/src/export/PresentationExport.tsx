import { useEffect, useState } from 'react';
import { Download, LoaderCircle, RefreshCw } from 'lucide-react';
import { sceneRequest } from '../scene/scene-api';
import './export.css';

export type ExportSource = {
  source_hash: string | null;
  can_export: boolean;
  reason: string | null;
};
export type ExportSummary = {
  id: string;
  source_hash: string;
  status: string;
  ready_pages: number;
  total_pages: number;
  error: string | null;
  failed_page: number | null;
  warnings: string[];
  download_url: string | null;
};
type Props = { presentationId: string; source?: ExportSource; latest?: ExportSummary | null };

export function PresentationExport({ presentationId, source, latest }: Props) {
  const [record, setRecord] = useState(latest || null);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    if (latest) setRecord(latest);
  }, [latest]);
  const active = record && ['PREPARING', 'QUEUED', 'RUNNING'].includes(record.status);
  useEffect(() => {
    if (!active || !record) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await sceneRequest<ExportSummary>(`/exports/${record.id}`, {
          signal: controller.signal,
        });
        if (!controller.signal.aborted) {
          setRecord(value);
          setError('');
        }
      } catch (e) {
        if (!controller.signal.aborted) setError(String(e));
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 1500);
    };
    timer = setTimeout(poll, 500);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [active, record?.id]);
  async function start() {
    if (!source?.source_hash || sending) return;
    setSending(true);
    setError('');
    try {
      setRecord(
        await sceneRequest<ExportSummary>(`/presentations/${presentationId}/exports`, {
          method: 'POST',
          body: JSON.stringify({
            format: 'pptx',
            expected_source_hash: source.source_hash,
            retry: record?.status === 'FAILED',
          }),
        }),
      );
    } catch (e) {
      setError(String(e));
    } finally {
      setSending(false);
    }
  }
  const failed = record?.status === 'FAILED';
  const message =
    record?.status === 'PREPARING'
      ? `准备页面 ${record.ready_pages}/${record.total_pages}`
      : record?.status === 'QUEUED'
        ? '等待导出'
        : record?.status === 'RUNNING'
          ? '正在导出 PPTX'
          : '';
  return (
    <div className="presentation-export">
      {record?.download_url ? (
        <a className="secondary-button" href={record.download_url} download>
          <Download size={15} />
          下载 PPTX
        </a>
      ) : (
        <button
          className="secondary-button"
          disabled={!source?.can_export || sending || Boolean(active)}
          title={source?.reason || undefined}
          onClick={start}
        >
          {active || sending ? (
            <LoaderCircle size={15} className="export-spinner" />
          ) : failed ? (
            <RefreshCw size={15} />
          ) : (
            <Download size={15} />
          )}
          {message || (sending ? '正在准备' : failed ? '重试导出' : '导出 PPTX')}
        </button>
      )}
      {(error || record?.error) && (
        <small className="export-error" role="alert">
          {error || record?.error}
        </small>
      )}
      {!!record?.warnings?.length && (
        <small className="export-warning" title={record.warnings.join('\n')}>
          部分元素已作为图片导出
        </small>
      )}
    </div>
  );
}
