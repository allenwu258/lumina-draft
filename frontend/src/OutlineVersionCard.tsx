import { useEffect, useRef, useState, type ReactNode } from 'react';
import { FileText, LayoutTemplate } from 'lucide-react';
import { api, type ArtifactSelection, type CardAction, type OutlineDocument, type OutlineVersion } from './workbench-api';

type Props = {
  presentationId: string;
  messageId: string;
  outlineId: string;
  revision: number;
  current: OutlineVersion | null;
  canSubmit: boolean;
  submit: (text: string, action: CardAction, requestId: string) => Promise<void>;
  refresh: () => Promise<void>;
  reference: (selection: ArtifactSelection) => void;
};

function Field({ label, value, change, multiline }: {label: string; value: string; change?: (value: string) => void; multiline?: boolean}) {
  let content: ReactNode = <p>{value || '—'}</p>;
  if (change) {
    content = multiline
      ? <textarea rows={3} value={value} onChange={event => change(event.target.value)} />
      : <input value={value} onChange={event => change(event.target.value)} />;
    return <label className="agent-outline-field"><strong>{label}</strong>{content}</label>;
  }
  return <div className="agent-outline-field"><strong>{label}</strong>{content}</div>;
}

export function OutlineVersionCard({ presentationId, messageId, outlineId, revision, current, canSubmit, submit, refresh, reference }: Props) {
  const [version, setVersion] = useState<OutlineVersion | null>(null);
  const [draft, setDraft] = useState<OutlineDocument | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const request = useRef({ key: '', id: '' });
  const submitted = useRef<{ version: OutlineVersion; messageId: string } | null>(null);
  const latest = current?.id === outlineId ? current : null;
  const editable = Boolean(latest && !latest.confirmed);
  const saved = latest || version;
  const editing = editable && draft !== null;
  const document = editing ? draft : saved?.document;
  const label = '大纲 V' + revision;

  useEffect(() => {
    if (latest || version) return;
    const controller = new AbortController();
    api<OutlineVersion>(`/presentations/${presentationId}/outlines/${outlineId}`, { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setVersion(result); })
      .catch(cause => { if (!controller.signal.aborted) setError(String(cause)); });
    return () => controller.abort();
  }, [presentationId, outlineId, latest?.id, version, reload]);

  async function commit(generate: boolean) {
    if (busy || !latest || (generate && !canSubmit)) return;
    setBusy(true);
    setError('');
    try {
      let target = submitted.current || { version: latest, messageId };
      if (!submitted.current && editing && JSON.stringify(draft) !== JSON.stringify(latest.document)) {
        const fresh = await api<OutlineVersion>(`/presentations/${presentationId}/outline`, {
          method: 'PUT', body: JSON.stringify({ outline_id: latest.id, content_hash: latest.content_hash, document: draft }),
        });
        if (!fresh.message_id) throw new Error('已保存，请在新大纲卡上继续。');
        target = { version: fresh, messageId: fresh.message_id };
        submitted.current = target;
      }
      if (generate) {
        if (request.current.key !== target.version.id) request.current = { key: target.version.id, id: crypto.randomUUID() };
        await submit('确认大纲 V' + target.version.revision + ' 并生成 PPT', {
          type: 'compose_deck', artifact_message_id: target.messageId, expected_content_hash: target.version.content_hash,
        }, request.current.id);
        request.current = { key: '', id: '' };
      }
      setDraft(null);
    } catch (cause) {
      setError(String(cause));
    } finally {
      await refresh();
      submitted.current = null;
      setBusy(false);
    }
  }

  function changeSlide(index: number, changes: Partial<OutlineDocument['slides'][number]>) {
    setDraft(value => value && ({ ...value, slides: value.slides.map((slide, position) => position === index ? { ...slide, ...changes } : slide) }));
  }
  function changeBrief(field: 'title' | 'audience' | 'tone' | 'purpose', value: string) {
    setDraft(previous => previous && ({ ...previous, brief: { ...previous.brief, [field]: value } }));
  }
  return <article className="agent-artifact outline-version-card" aria-label={label}>
    <header className="agent-artifact-header">
      <span className="special-card-icon"><LayoutTemplate size={16}/></span>
      <div><h2>{label}</h2><p>{editable ? '当前草稿' : '只读快照'}{document ? ' · ' + document.slides.length + ' 页' : ''}</p></div>
      <button type="button" className="secondary-button" onClick={() => reference({ artifact_message_id: messageId, label })}><FileText size={14}/>引用到对话</button>
    </header>
    {document ? <div className="agent-outline-body">
      <ol className="agent-outline-slides" aria-label="叙事结构">
        {document.slides.map((slide, index) => <li key={slide.slide_id}>
          <div className="agent-slide-meta"><span>第 {slide.page_number} 页</span><button type="button" className="secondary-button" onClick={() => saved && reference({
            artifact_message_id: messageId, slide_id: slide.slide_id, source_ref: saved.content_hash, label: label + ' · 第 ' + slide.page_number + ' 页',
          })}>引用此页</button></div>
          {editing ? <Field label="页面标题" value={slide.title} change={value => changeSlide(index, {title: value})}/> : <h3>{slide.title}</h3>}
          <Field label="核心观点" value={slide.key_message} change={editing ? value => changeSlide(index, {key_message: value}) : undefined} multiline/>
          {editing ? <Field label="内容要点（每行一条）" value={slide.content_outline.join('\n')} change={value => changeSlide(index, {content_outline: value.split('\n')})} multiline/>
            : <ul>{slide.content_outline.map((point, position) => <li key={position}>{point}</li>)}</ul>}
          {slide.summary && <Field label="页面摘要" value={slide.summary}/>}
          {slide.design_notes && <Field label="设计说明" value={slide.design_notes}/>}
        </li>)}
      </ol>
      <aside className="agent-outline-brief">
        <h3>作品 Brief</h3>
        <Field label="作品标题" value={document.brief.title} change={editing ? value => changeBrief('title', value) : undefined}/>
        <Field label="面向对象" value={document.brief.audience} change={editing ? value => changeBrief('audience', value) : undefined}/>
        <Field label="内容语气" value={document.brief.tone} change={editing ? value => changeBrief('tone', value) : undefined}/>
        <Field label="制作目的" value={document.brief.purpose} change={editing ? value => changeBrief('purpose', value) : undefined} multiline/>
        <Field label="语言" value={document.brief.language}/>
        {document.brief.assumptions.length > 0 && <><strong>前提与说明</strong><ul>{document.brief.assumptions.map((item, index) => <li key={index}>{item}</li>)}</ul></>}
        <h3>全局视觉规范</h3>
        <p>{document.style_spec.description}</p>
        <div className="agent-outline-swatches">{Object.entries(document.style_spec.colors).map(([name, value]) => <span key={name} title={name + ' ' + value} style={{backgroundColor: value}}/>)}</div>
        <Field label="字体" value={document.style_spec.typography.font_family + ' · ' + [document.style_spec.typography.title_size_px, document.style_spec.typography.body_size_px, document.style_spec.typography.caption_size_px].join(' / ') + ' px'}/>
        <ul>{document.style_spec.layout_rules.map((rule, index) => <li key={index}>{rule}</li>)}</ul>
      </aside>
    </div> : <p className="agent-card-status">{error ? <button className="secondary-button" onClick={() => { setError(''); setReload(value => value + 1); }}>重读大纲</button> : '正在读取大纲…'}</p>}
    {error && <p className="agent-card-status" role="alert">{error}</p>}
    {editable && document && <footer className="agent-artifact-actions">
      {editing ? <>
        <button className="secondary-button" disabled={busy} onClick={() => setDraft(null)}>取消编辑</button>
        <button className="secondary-button" disabled={busy} onClick={() => void commit(false)}>保存修改</button>
      </> : <button className="secondary-button" disabled={busy} onClick={() => setDraft(document)}>编辑大纲</button>}
      <button className="primary-button" disabled={busy || !canSubmit} onClick={() => void commit(true)}>{editing ? '保存并生成 PPT' : '确认并生成 PPT'}</button>
    </footer>}
  </article>;
}
