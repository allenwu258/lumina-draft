import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, Check, Download, FileText, LoaderCircle, Save } from 'lucide-react';
import type { SceneIR } from '@lumina/scene-core';
import { sceneRequest } from '../scene/scene-api';
import { SceneCanvas } from './SceneCanvas';
import './work-editor.css';

type WorkPage = { id: string; position: number; title: string };
type Work = {
  id: string;
  name: string;
  type: string;
  revision: number;
  page_count: number;
  pages: WorkPage[];
  latest_export: ExportRecord | null;
};
type ExportRecord = {
  id: string;
  status: string;
  work_revision: number | null;
  error: string | null;
  download_url: string | null;
  filename: string;
};
type WorkPageDocument = { id: string; position: number; title: string; scene_hash: string; scene: SceneIR };

export function WorkEditor({ workId }: { workId: string }) {
  const [work, setWork] = useState<Work | null>(null);
  const [name, setName] = useState('');
  const [savedName, setSavedName] = useState('');
  const [page, setPage] = useState<WorkPageDocument | null>(null);
  const [activePageId, setActivePageId] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState('');
  const [exportError, setExportError] = useState('');
  const [exportRecord, setExportRecord] = useState<ExportRecord | null>(null);
  const savingRef = useRef(false);
  const dirty = name.trim() !== savedName;
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    sceneRequest<Work>(`/works/${workId}`, { signal: controller.signal })
      .then((result) => {
        if (controller.signal.aborted) return;
        setWork(result);
        setName(result.name);
        setSavedName(result.name);
        setActivePageId(result.pages[0]?.id || '');
        setExportRecord(result.latest_export);
        setError('');
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [workId]);

  useEffect(() => {
    if (!activePageId) return;
    const controller = new AbortController();
    setPage(null);
    sceneRequest<WorkPageDocument>(`/works/${workId}/pages/${activePageId}`, { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setPage(value);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
      });
    return () => controller.abort();
  }, [workId, activePageId]);

  const saveName = useCallback(async (): Promise<Work | null> => {
    if (!work || !dirtyRef.current) return work;
    if (savingRef.current) return null;
    savingRef.current = true;
    setSaving(true);
    setError('');
    try {
      const result = await sceneRequest<Work>(`/works/${workId}`, {
        method: 'PATCH',
        body: JSON.stringify({ name: name.trim(), base_revision: work.revision }),
      });
      setWork((current) => current ? { ...current, name: result.name, revision: result.revision } : current);
      setName(result.name);
      setSavedName(result.name);
      return result;
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
      throw cause;
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }, [name, work, workId]);

  const saveRef = useRef(saveName);
  saveRef.current = saveName;
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (dirtyRef.current && !savingRef.current) void saveRef.current().catch(() => undefined);
    }, 60_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);

  useEffect(() => {
    if (!exportRecord || !['PREPARING', 'QUEUED', 'RUNNING'].includes(exportRecord.status)) return;
    const controller = new AbortController();
    let timer = 0;
    const poll = async () => {
      try {
        const result = await sceneRequest<ExportRecord>(`/exports/${exportRecord.id}`, { signal: controller.signal });
        if (!controller.signal.aborted) {
          setExportRecord(result);
          if (result.status === 'FAILED') setExportError(result.error || 'PPTX 导出失败');
        }
      } catch (cause) {
        if (!controller.signal.aborted) setExportError(cause instanceof Error ? cause.message : String(cause));
      }
      if (!controller.signal.aborted) timer = window.setTimeout(poll, 1200);
    };
    timer = window.setTimeout(poll, 600);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [exportRecord?.id, exportRecord?.status]);

  async function startExport() {
    if (!work || exporting) return;
    setExporting(true);
    setExportError('');
    try {
      const saved = await saveName();
      if (!saved) return;
      const result = await sceneRequest<ExportRecord>(`/works/${workId}/exports`, {
        method: 'POST',
        body: JSON.stringify({ expected_revision: saved.revision, retry: exportRecord?.status === 'FAILED' }),
      });
      setExportRecord(result);
    } catch (cause) {
      setExportError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setExporting(false);
    }
  }

  const exportActive = !!exportRecord && ['PREPARING', 'QUEUED', 'RUNNING'].includes(exportRecord.status);

  if (loading) return <main className="work-editor-loading"><LoaderCircle className="spin" size={20} />正在打开作品</main>;
  if (!work) return <main className="work-editor-loading" role="alert">{error || '作品不存在'} <a href="/">返回</a></main>;

  return (
    <main className="work-editor">
      <header className="work-editor-header">
        <a className="work-editor-back" href="/works" aria-label="返回我的作品"><ArrowLeft size={17} /></a>
        <div className="work-editor-title">
          <input
            aria-label="作品名称"
            value={name}
            maxLength={200}
            disabled={saving}
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') void saveName().catch(() => undefined);
            }}
          />
          <span role="status">{saving ? '正在保存' : dirty ? '未保存' : `已保存 · v${work.revision}`}</span>
        </div>
        <div className="work-editor-actions">
          <button className="secondary-button" disabled={!dirty || saving} onClick={() => void saveName().catch(() => undefined)}>
            {saving ? <LoaderCircle className="spin" size={15} /> : dirty ? <Save size={15} /> : <Check size={15} />}
            保存
          </button>
          {exportRecord?.download_url && !dirty && exportRecord.work_revision === work.revision ? (
            <a className="primary-button" href={exportRecord.download_url} download={exportRecord.filename}>
              <Download size={15} />下载 PPTX
            </a>
          ) : (
            <button className="primary-button" disabled={exporting || saving || exportActive} onClick={() => void startExport()}>
              {exporting || exportActive ? <LoaderCircle className="spin" size={15} /> : <Download size={15} />}
              {exportActive ? '正在导出' : exportRecord?.status === 'FAILED' && exportRecord.work_revision === work.revision ? '重试导出' : '导出 PPTX'}
            </button>
          )}
        </div>
      </header>
      {(error || exportError) && <div className="work-editor-error" role="alert">{error || exportError}</div>}
      <div className="work-editor-body">
        <aside className="work-pages">
          <div className="work-pages-heading"><span>页面</span><span>{work.page_count}</span></div>
          <ol>
            {work.pages.map((item) => (
              <li key={item.id}>
                <button className={activePageId === item.id ? 'selected' : ''} onClick={() => setActivePageId(item.id)}>
                  <span>{String(item.position).padStart(2, '0')}</span>
                  <FileText size={15} />
                  <strong>{item.title || `第 ${item.position} 页`}</strong>
                </button>
              </li>
            ))}
          </ol>
        </aside>
        <section className="work-page-area" aria-label="页面预览">
          <div className="work-page-toolbar">
            <span>{page ? `第 ${page.position} 页 · ${page.title}` : '加载页面'}</span>
            {page && <span>{page.scene.page.width} × {page.scene.page.height} px</span>}
          </div>
          {page ? <SceneCanvas workId={workId} pageId={page.id} scene={page.scene} /> : <div className="work-page-placeholder"><LoaderCircle className="spin" size={20} /></div>}
          {page?.scene.diagnostics.length ? <div className="work-page-diagnostics">{page.scene.diagnostics.length} 项渲染提示</div> : null}
        </section>
      </div>
    </main>
  );
}
