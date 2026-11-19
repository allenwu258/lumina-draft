import { useEffect, useLayoutEffect, useRef, useState, type FormEvent } from 'react';
import { FileText, LoaderCircle, Send, X } from 'lucide-react';
import type { ArtifactSelection } from './workbench-api';

type Props = {
  activeTurn?: string | null;
  allowed: boolean;
  selection: ArtifactSelection | null;
  clearSelection: () => void;
  submit: (path: string, body: object) => Promise<unknown>;
  refresh: () => Promise<void>;
  onSent: () => void;
};

export function AgentComposer({ activeTurn, allowed, selection, clearSelection, submit, refresh, onSent }: Props) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const request = useRef({ payload: '', key: '' });
  const inputRef = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const input = inputRef.current;
    if (input) {
      input.style.height = 'auto';
      input.style.height = input.scrollHeight + 'px';
    }
  }, [text]);

  useEffect(() => {
    const viewport = window.visualViewport;
    const input = inputRef.current;
    const shell = input?.closest<HTMLElement>('.app-shell');
    if (!input || !shell || !viewport) return;
    const fitKeyboard = () => {
      if (document.activeElement === input && viewport.scale === 1) {
        shell.style.setProperty('--agent-viewport-height', viewport.height + 'px');
      } else {
        shell.style.removeProperty('--agent-viewport-height');
      }
    };
    input.addEventListener('focus', fitKeyboard);
    input.addEventListener('blur', fitKeyboard);
    viewport.addEventListener('resize', fitKeyboard);
    return () => {
      input.removeEventListener('focus', fitKeyboard);
      input.removeEventListener('blur', fitKeyboard);
      viewport.removeEventListener('resize', fitKeyboard);
      shell.style.removeProperty('--agent-viewport-height');
    };
  }, []);

  async function send(event: FormEvent) {
    event.preventDefault();
    if (busy || activeTurn || !allowed || !text.trim()) return;
    const data = selection
      ? { text: text.trim(), view_ref: { artifact_message_id: selection.artifact_message_id, slide_id: selection.slide_id, source_ref: selection.source_ref } }
      : { text: text.trim() };
    const payload = JSON.stringify(data);
    if (request.current.payload !== payload) request.current = { payload, key: crypto.randomUUID() };
    setBusy(true);
    setError('');
    try {
      await submit('', { ...data, client_request_id: request.current.key });
      onSent();
      setText('');
      request.current.payload = '';
    } catch (cause) {
      setError(String(cause));
    } finally {
      await refresh();
      setBusy(false);
    }
  }
  async function cancel() {
    setBusy(true);
    setError('');
    try {
      await submit('/' + activeTurn + '/cancel', {});
    } catch (cause) {
      setError(String(cause));
    } finally {
      await refresh();
      setBusy(false);
    }
  }
  return <form className="composer agent-composer" onSubmit={send}>
    {selection && <button className="agent-selection" type="button" aria-label={selection.label+' · 移除引用'} onClick={clearSelection}><FileText size={13}/>{selection.label}<X size={12}/></button>}
    <textarea ref={inputRef} rows={2} aria-label="继续对话" value={text} maxLength={20000} placeholder="补充需求，或询问当前作品…" onChange={event => setText(event.target.value)} onKeyDown={event => {
      if (!event.nativeEvent.isComposing && (event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        event.currentTarget.form?.requestSubmit();
      }
    }}/>
    {error && <p role="alert">{error}</p>}
    <div className="composer-bottom"><span role="status">{activeTurn ? '助手正在处理…' : !allowed ? '当前任务结束后可以继续对话' : ''}</span>
      {activeTurn ? <button type="button" className="secondary-button" disabled={busy} onClick={cancel}>停止本轮</button> : <button className="send-button" disabled={busy || !allowed || !text.trim()}>{busy ? <LoaderCircle size={15} className="spin"/> : <Send size={15}/>}发送</button>}
    </div>
  </form>;
}
