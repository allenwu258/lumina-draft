import { useLayoutEffect, useRef, useState } from 'react';
import { Activity, Bot, Check, LoaderCircle, UserRound } from 'lucide-react';
import type { CanvasSize } from '@lumina/scene-core';
import { AgentComposer } from './AgentComposer';
import { AgentDeckCard, type DeckDetails } from './AgentDeckCard';
import { OutlineVersionCard } from './OutlineVersionCard';
import { api, type ArtifactSelection, type CardAction, type ConversationMessage, type OutlineVersion } from './workbench-api';
import './agent-artifacts.css';

type Props = {
  presentationId: string;
  title: string;
  canvas: CanvasSize;
  currentOutline: OutlineVersion | null;
  legacy: DeckDetails;
  messages: ConversationMessage[];
  activeTurn?: string | null;
  allowed: boolean;
  refresh: () => Promise<void>;
  runUpdates: Record<string, number>;
  error: string;
};

export function AgentConversation({ presentationId, title, canvas, currentOutline, legacy, messages, activeTurn, allowed, refresh, runUpdates, error }: Props) {
  const [selection, setSelection] = useState<ArtifactSelection | null>(null);
  const scroller = useRef<HTMLElement>(null);
  const feed = useRef<HTMLDivElement>(null);
  const followTail = useRef(true);
  const submit = (suffix: string, body: object) => api('/presentations/' + presentationId + '/turns' + suffix, {
    method: 'POST', body: JSON.stringify(body),
  });
  const sent = () => { followTail.current = true; };
  const reference = (value: ArtifactSelection) => { setSelection(value); };

  async function submitAction(text: string, action: CardAction, requestId: string) {
    await submit('', { text, action, client_request_id: requestId });
    sent();
    await refresh();
  }
  async function cancel(runId: string) {
    await api('/runs/' + runId + '/cancel', { method: 'POST' });
    await refresh();
  }

  const lastMessageId = messages.at(-1)?.id;
  useLayoutEffect(() => {
    const viewport = scroller.current;
    const timeline = feed.current;
    if (!viewport || !timeline) return;
    const scrollToTail = () => {
      if (followTail.current) viewport.scrollTop = viewport.scrollHeight;
    };
    scrollToTail();
    const observer = new ResizeObserver(scrollToTail);
    observer.observe(timeline);
    return () => observer.disconnect();
  }, [lastMessageId]);

  return <>
    <section ref={scroller} className="workbench agent-workbench" onScroll={() => {
      const viewport = scroller.current;
      if (viewport) followTail.current = viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop < 48;
    }}>
      <header className="agent-session-title"><h1>{title}</h1></header>
      {error && <p role="alert" className="inline-error">{error}</p>}
      <div ref={feed} className="conversation-feed" aria-label="对话记录">
        {messages.map(message => {
          const payload = message.payload;
          const runId = typeof payload.run_id === 'string' ? payload.run_id : undefined;
          if (message.kind === 'outline_card' && typeof payload.outline_id === 'string') {
            return <OutlineVersionCard key={message.id} presentationId={presentationId} messageId={message.id}
              outlineId={payload.outline_id} revision={Number(payload.revision)} current={currentOutline}
              canSubmit={allowed} submit={submitAction} refresh={refresh} reference={reference}/>;
          }
          if (message.kind === 'generation_card') {
            const batchId = typeof payload.batch_id === 'string' ? payload.batch_id : undefined;
            const label = typeof payload.revision === 'number' ? 'PPT · 大纲 V' + payload.revision : 'PPT';
            return <AgentDeckCard key={message.id} presentationId={presentationId} messageId={message.id} batchId={batchId} runId={runId}
              label={label} status={typeof payload.status === 'string' ? payload.status : message.status}
              pageCount={Number(payload.page_count) || 0} canvas={canvas} update={runUpdates[runId || ''] || 0}
              legacy={!batchId && legacy.pages.some(page => page.run_id === runId) ? legacy : undefined}
              canSubmit={allowed} submit={submitAction} cancel={cancel} reference={reference}/>;
          }
          if (message.kind === 'inspection') return <details className="agent-observation" key={message.id}>
            <summary>查阅作品 · {payload.view === 'pages' ? '页面' : payload.view === 'outline' ? '大纲' : '概览'}</summary>
            <pre>{JSON.stringify(payload.result, null, 2)}</pre>
          </details>;
          if (message.kind === 'activity') return <ActivityMessage key={message.id} message={message} runId={runId} cancel={cancel}/>;
          if (message.kind === 'action' && !message.body_text) return <div className="conversation-action" key={message.id}>
            <Check size={13}/>{payload.action === 'confirm_outline' ? '你已确认大纲' : payload.action === 'retry_run' ? '你发起了重新生成' : '已执行操作'}
          </div>;
          return message.body_text ? <ChatMessage key={message.id} message={message}/> : null;
        })}
      </div>
    </section>
    <footer className="agent-dock">
      <AgentComposer activeTurn={activeTurn} allowed={allowed} selection={selection} clearSelection={() => setSelection(null)}
        submit={submit} refresh={refresh} onSent={sent}/>
    </footer>
  </>;
}

function ActivityMessage({ message, runId, cancel }: {message: ConversationMessage; runId?: string; cancel: (runId: string) => Promise<void>}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const active = message.status === 'STREAMING';
  async function stop(id: string) {
    setBusy(true);
    setError('');
    try { await cancel(id); }
    catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  }
  return <div className={'conversation-activity ' + message.status.toLowerCase()}>
    <div className="conversation-activity-icon"><Activity size={14}/></div>
    <div><strong>{typeof message.payload.label === 'string' ? message.payload.label : '正在处理你的需求'}</strong>
      <small>{active ? '进行中' : message.status === 'FAILED' ? '失败' : message.status === 'CANCELLED' ? '已取消' : '已完成'}</small>
      {(error || typeof message.payload.error === 'string') && <p role="alert">{error || String(message.payload.error)}</p>}
    </div>
    {active && <LoaderCircle className="spin" size={14}/>}
    {active && runId && <button className="secondary-button" disabled={busy} onClick={() => void stop(runId)}>取消规划</button>}
  </div>;
}

function ChatMessage({message}: {message: ConversationMessage}) {
  const user = message.role === 'user';
  const Icon = user ? UserRound : Bot;
  const label = user ? '你' : '助手';
  const note = ({suggest:'下一步建议', error:'回复失败', cancelled:'本轮已停止'} as Record<string,string>)[message.kind];
  return <article className={'chat-message ' + (user ? 'from-user' : 'from-assistant')} aria-label={label}>
    <div className="chat-message-avatar"><Icon size={15} aria-hidden="true" /></div>
    <div className="chat-message-copy"><header><strong>{label}</strong>{note && <span> · {note}</span>}</header>
      <p>{message.body_text}</p>
    </div>
  </article>;
}
