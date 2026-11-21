import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AlignCenter, AlignLeft, AlignRight, ArrowLeft, Check, Download,
  FileText, LoaderCircle, Save,
} from 'lucide-react';
import type { SceneIR, TextNode, TextStyle } from '@lumina/scene-core';
import { sceneRequest } from '../scene/scene-api';
import { SceneCanvas } from './SceneCanvas';
import {
  normalizeEditor,
  reflowTextNode,
  selectedTextStyle,
  textLayout,
  translateTextLayouts,
  type SelectionTarget,
  type TextAlign,
  type WorkEditorState,
} from './scene-edit';
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
type WorkPageDocument = {
  id: string;
  position: number;
  title: string;
  scene_hash: string;
  scene: SceneIR;
  editor: WorkEditorState;
};
type SavePageResult = { work_revision: number; page_id: string; scene_hash: string; editor: WorkEditorState; saved_at: string };

const FONT_OPTIONS = ['Arial', 'Microsoft YaHei', 'SimSun', 'SimHei', 'KaiTi'];
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const equal = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const points = (pixels: number) => Math.round(pixels * 1.2 * 10) / 10;
const pixels = (pointsValue: number) => Math.round((pointsValue / 1.2) * 100) / 100;

export function WorkEditor({ workId }: { workId: string }) {
  const [work, setWork] = useState<Work | null>(null);
  const [name, setName] = useState('');
  const [savedName, setSavedName] = useState('');
  const [page, setPage] = useState<WorkPageDocument | null>(null);
  const [savedPage, setSavedPage] = useState<{ scene: SceneIR; editor: WorkEditorState } | null>(null);
  const [activePageId, setActivePageId] = useState('');
  const [selected, setSelected] = useState<SelectionTarget | null>(null);
  const [editingNodeId, setEditingNodeId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState('');
  const [exportError, setExportError] = useState('');
  const [exportRecord, setExportRecord] = useState<ExportRecord | null>(null);
  const savingRef = useRef(false);
  const downloadingExportRef = useRef<string | null>(null);
  const workRef = useRef<Work | null>(null);
  const pageRef = useRef<WorkPageDocument | null>(null);
  const savedPageRef = useRef<{ scene: SceneIR; editor: WorkEditorState } | null>(null);
  const nameRef = useRef('');
  const savedNameRef = useRef('');
  const saveRef = useRef<() => Promise<Work | null>>(() => Promise.resolve(null));

  workRef.current = work;
  pageRef.current = page;
  savedPageRef.current = savedPage;
  nameRef.current = name;
  savedNameRef.current = savedName;

  const pageDirty = !!page && !!savedPage && (!equal(page.scene, savedPage.scene) || !equal(page.editor, savedPage.editor));
  const dirty = name.trim() !== savedName || pageDirty;
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
    setSavedPage(null);
    setSelected(null);
    setEditingNodeId(null);
    sceneRequest<WorkPageDocument>(`/works/${workId}/pages/${activePageId}`, { signal: controller.signal, cache: 'no-cache' })
      .then((value) => {
        if (controller.signal.aborted) return;
        const normalized = { ...value, editor: normalizeEditor(value.editor) };
        setPage(normalized);
        setSavedPage({ scene: clone(normalized.scene), editor: clone(normalized.editor) });
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
      });
    return () => controller.abort();
  }, [workId, activePageId]);

  const saveChanges = useCallback(async (): Promise<Work | null> => {
    const currentWork = workRef.current;
    const currentPage = pageRef.current;
    const currentSavedPage = savedPageRef.current;
    const nameChanged = nameRef.current.trim() !== savedNameRef.current;
    const pageChanged = !!currentPage && !!currentSavedPage &&
      (!equal(currentPage.scene, currentSavedPage.scene) || !equal(currentPage.editor, currentSavedPage.editor));
    if (!currentWork || (!nameChanged && !pageChanged)) return currentWork;
    if (savingRef.current) return null;
    savingRef.current = true;
    setSaving(true);
    setError('');
    try {
      let revision = currentWork.revision;
      let nextWork = currentWork;
      if (pageChanged && currentPage) {
        const saved = await sceneRequest<SavePageResult>(`/works/${workId}/pages/${currentPage.id}`, {
          method: 'PUT',
          body: JSON.stringify({ base_revision: revision, scene: currentPage.scene, editor: currentPage.editor }),
        });
        revision = saved.work_revision;
        nextWork = { ...nextWork, revision };
        const savedDocument = { scene: clone(currentPage.scene), editor: clone(saved.editor) };
        setPage((value) => value?.id === currentPage.id ? { ...value, scene_hash: saved.scene_hash } : value);
        setSavedPage(savedDocument);
        savedPageRef.current = savedDocument;
        workRef.current = nextWork;
        setWork(nextWork);
      }
      if (nameChanged) {
        nextWork = await sceneRequest<Work>(`/works/${workId}`, {
          method: 'PATCH',
          body: JSON.stringify({ name: nameRef.current.trim(), base_revision: revision }),
        });
        setName(nextWork.name);
        setSavedName(nextWork.name);
        savedNameRef.current = nextWork.name;
      }
      setWork(nextWork);
      workRef.current = nextWork;
      return nextWork;
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
      throw cause;
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }, [workId]);

  saveRef.current = saveChanges;

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
          if (result.status === 'SUCCEEDED') void downloadExport(result).catch((cause) => {
            if (!controller.signal.aborted) setExportError(cause instanceof Error ? cause.message : String(cause));
          });
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

  async function downloadExport(record: ExportRecord, allowDirty = false) {
    const currentWork = workRef.current;
    if (
      record.status !== 'SUCCEEDED' ||
      !record.download_url ||
      !currentWork ||
      record.work_revision !== currentWork.revision ||
      (!allowDirty && dirtyRef.current)
    ) return;
    if (downloadingExportRef.current === record.id) return;
    downloadingExportRef.current = record.id;
    try {
      const response = await fetch(record.download_url, { cache: 'no-cache' });
      if (!response.ok) {
        let message = '导出文件下载失败';
        try {
          const body = await response.json() as { detail?: { message?: string } };
          message = body.detail?.message || message;
        } catch {
          // Keep the stable download error when the server does not return JSON.
        }
        throw new Error(message);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = record.filename;
      anchor.style.display = 'none';
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
    } finally {
      downloadingExportRef.current = null;
    }
  }

  async function startExport() {
    if (!work || exporting) return;
    setExporting(true);
    setExportError('');
    try {
      const saved = await saveChanges();
      const current = saved || workRef.current;
      if (!current) return;
      const result = await sceneRequest<ExportRecord>(`/works/${workId}/exports`, {
        method: 'POST',
        body: JSON.stringify({ expected_revision: current.revision, retry: exportRecord?.status === 'FAILED' }),
      });
      setExportRecord(result);
      if (result.status === 'SUCCEEDED') await downloadExport(result, true);
    } catch (cause) {
      setExportError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setExporting(false);
    }
  }

  function updateScene(nextScene: SceneIR) {
    setPage((value) => value ? {
      ...value, scene: nextScene, editor: translateTextLayouts(value.editor, value.scene, nextScene),
    } : value);
  }

  function updateText(nodeId: string, change: { sourceText?: string; align?: TextAlign; style?: Partial<TextStyle> }) {
    const current = pageRef.current;
    if (!current) return;
    const node = current.scene.nodes.find((item): item is TextNode => item.id === nodeId && item.kind === 'text');
    if (!node) return;
    const layout = textLayout(current.scene, node, current.editor);
    if (change.align === layout.align || change.sourceText === node.text.sourceText ||
        (change.style && Object.entries(change.style).every(([key, value]) =>
          node.text.paragraphs[0]?.runs[0]?.style[key as keyof TextStyle] === value))) return;
    if (change.align) layout.align = change.align;
    setPage({
      ...current,
      editor: { ...current.editor, text: { ...current.editor.text, [nodeId]: layout } },
      scene: reflowTextNode(current.scene, nodeId, change.sourceText ?? node.text.sourceText, layout, change.style),
    });
  }

  function updateAlignment(align: TextAlign) {
    if (selected?.textNodeId) updateText(selected.textNodeId, { align });
  }

  function updateStyle(style: Partial<TextStyle>) {
    if (selected?.textNodeId) updateText(selected.textNodeId, { style });
  }

  async function selectPage(pageId: string) {
    if (pageId === activePageId) return;
    if (pageDirty) {
      try {
        const saved = await saveChanges();
        if (!saved) return;
      } catch {
        return;
      }
    }
    setActivePageId(pageId);
  }

  const exportActive = !!exportRecord && ['PREPARING', 'QUEUED', 'RUNNING'].includes(exportRecord.status);
  const selectedStyle = page && selected?.textNodeId ? selectedTextStyle(page.scene, selected.textNodeId) : null;
  const selectedNode = page?.scene.nodes.find((item): item is TextNode => item.id === selected?.textNodeId && item.kind === 'text');
  const selectedAlign = page && selectedNode ? textLayout(page.scene, selectedNode, page.editor).align : 'left';

  if (loading) return <main className="work-editor-loading"><LoaderCircle className="spin" size={20} />正在打开作品</main>;
  if (!work) return <main className="work-editor-loading" role="alert">{error || '作品不存在'} <a href="/">返回</a></main>;

  return (
    <main className="work-editor">
      <header className="work-editor-header">
        <a className="work-editor-back" href="/works" aria-label="返回我的作品"><ArrowLeft size={17} /></a>
        <div className="work-editor-title">
          <input aria-label="作品名称" value={name} maxLength={200} disabled={saving} onChange={(event) => setName(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void saveChanges().catch(() => undefined); }} />
          <span role="status">{saving ? '正在保存' : dirty ? '未保存' : `已保存 · v${work.revision}`}</span>
        </div>
        {selectedStyle && (
          <div className="work-editor-toolbar" aria-label="文字格式工具栏">
            <select aria-label="字体" value={selectedStyle.fontFamily.replace(/["']/g, '').split(',')[0].trim()} onChange={(event) => updateStyle({ fontFamily: event.target.value })}>
              {[...new Set([selectedStyle.fontFamily.replace(/["']/g, '').split(',')[0].trim(), ...FONT_OPTIONS])].map((font) => <option key={font} value={font}>{font}</option>)}
            </select>
            <input aria-label="字号" type="number" min={8} max={96} step={0.1} value={points(selectedStyle.fontSize)} onChange={(event) => updateStyle({ fontSize: pixels(Number(event.target.value) || 8) })} />
            <button className={Number(selectedStyle.fontWeight) >= 600 ? 'toolbar-button active' : 'toolbar-button'} aria-label="加粗" title="加粗" onClick={() => updateStyle({ fontWeight: Number(selectedStyle.fontWeight) >= 600 ? '400' : '700' })}><strong>B</strong></button>
            <span className="toolbar-divider" />
            <button className={selectedAlign === 'left' ? 'toolbar-button active' : 'toolbar-button'} aria-label="左对齐" title="左对齐" onClick={() => updateAlignment('left')}><AlignLeft size={16} /></button>
            <button className={selectedAlign === 'center' ? 'toolbar-button active' : 'toolbar-button'} aria-label="居中对齐" title="居中对齐" onClick={() => updateAlignment('center')}><AlignCenter size={16} /></button>
            <button className={selectedAlign === 'right' ? 'toolbar-button active' : 'toolbar-button'} aria-label="右对齐" title="右对齐" onClick={() => updateAlignment('right')}><AlignRight size={16} /></button>
          </div>
        )}
        <div className="work-editor-actions">
          <button className="secondary-button" disabled={!dirty || saving} onClick={() => void saveChanges().catch(() => undefined)}>{saving ? <LoaderCircle className="spin" size={15} /> : dirty ? <Save size={15} /> : <Check size={15} />}保存</button>
          <button className="primary-button" disabled={exporting || saving || exportActive} onClick={() => void startExport()}>{exporting || exportActive ? <LoaderCircle className="spin" size={15} /> : <Download size={15} />}{exportActive ? '正在导出' : exportRecord?.status === 'FAILED' && exportRecord.work_revision === work.revision ? '重试并下载 PPTX' : '导出并下载 PPTX'}</button>
        </div>
      </header>
      {(error || exportError) && <div className="work-editor-error" role="alert">{error || exportError}</div>}
      <div className="work-editor-body">
        <aside className="work-pages">
          <div className="work-pages-heading"><span>页面</span><span>{work.page_count}</span></div>
          <ol>{work.pages.map((item) => <li key={item.id}><button className={activePageId === item.id ? 'selected' : ''} onClick={() => void selectPage(item.id)}><span>{String(item.position).padStart(2, '0')}</span><FileText size={15} /><strong>{item.title || `第 ${item.position} 页`}</strong></button></li>)}</ol>
        </aside>
        <section className="work-page-area" aria-label="页面预览">
          <div className="work-page-toolbar"><span>{page ? `第 ${page.position} 页 · ${page.title}` : '加载页面'}</span>{page && <span>{page.scene.page.width} × {page.scene.page.height} px</span>}</div>
          {page ? <SceneCanvas workId={workId} pageId={page.id} scene={page.scene} editor={page.editor} selected={selected} editingNodeId={editingNodeId} onSelect={setSelected} onEditStart={setEditingNodeId} onEditEnd={() => setEditingNodeId(null)} onSceneChange={updateScene} onTextChange={(nodeId, sourceText) => updateText(nodeId, { sourceText })} /> : <div className="work-page-placeholder"><LoaderCircle className="spin" size={20} /></div>}
          {page?.scene.diagnostics.length ? <div className="work-page-diagnostics">{page.scene.diagnostics.length} 项渲染提示</div> : null}
        </section>
      </div>
    </main>
  );
}
