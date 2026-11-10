import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Activity, ArrowDown, ArrowLeft, ArrowRight, Bot, Check, CheckCircle2, ChevronDown, ChevronUp, CircleHelp, FileText, Image, Layers3, LayoutTemplate, LoaderCircle, PanelLeftClose, PanelRightClose, PanelRightOpen, Palette, Plus, RefreshCw, Send, Settings2, ShieldCheck, Sparkles, X } from 'lucide-react';
import './styles.css';
import './generation-settings.css';
import './chat-ui.css';
import { ProviderSettings, type SettingsTab } from './ProviderSettings';
import { GenerationProgress, PageStatusIcon, pageStatusLabel } from './PageStatus';
import { SceneViewport } from './scene/SceneViewport';
import type { SceneSummary } from './scene/scene-api';

type Slide = { slide_id: string; page_number: number; page_type: string; title: string; key_message: string; content_outline: string[]; design_notes: string; summary: string };
type Document = { schema_version: string; brief: { title: string; purpose: string; audience: string; language: string; tone: string; assumptions: string[] }; style_spec: { description: string; colors: Record<string,string>; typography: {font_family:string;title_size_px:number;body_size_px:number;caption_size_px:number}; layout_rules:string[] }; slides: Slide[] };
type Page = { id:string; run_id:string; page_number:number; title:string; status:string; html_hash:string|null; error:string|null; error_code:string|null; scene?:SceneSummary };
type JobState = { id:string; status:string; queue_reason:string|null; queue_duration_ms:number|null; error_code:string|null; error:string|null };
type DirectorStream = {reasoning:string;output:string};
type EntryMode = 'agent' | 'ppt' | 'design' | 'image';
type ConversationMessage = {id:string;seq:number;role:string;kind:string;status:string;body_text:string|null;payload:Record<string,unknown>;created_at:string};
type Snapshot = { presentation:{id:string;user_request:string;status:string;entry_mode:EntryMode;workflow_code:string;conversation_id:string|null;created_at:string}; conversation?:{id:string|null;entry_mode:EntryMode;interaction_mode:string;state:string;messages:ConversationMessage[];allowed_actions:Record<string,boolean>;allowed_views:{outline:boolean;generation:boolean}}; latest_outline:{id:string;revision:number;content_hash:string;confirmed:boolean;document:Document}|null; pages:Page[]; active_run:{id:string;kind:string;status:string;error:string|null;error_code:string|null}|null; active_job:JobState|null; latest_run:{id:string;kind:string;status:string;error:string|null;error_code:string|null}|null; latest_job:JobState|null; active_streams:Record<string,string>; director_stream:DirectorStream|null; last_event_seq:number };
type SessionUsage = {presentation_id:string;call_count:number;input_tokens:number|null;output_tokens:number|null;total_tokens:number|null;input_unreported_calls:number;output_unreported_calls:number;total_unreported_calls:number};
type Provider = {name:string;base_url:string;model:string;enabled:boolean;revision:number;credential_configured:boolean};
const api = async <T,>(path:string, init?:RequestInit):Promise<T> => { const response=await fetch(`/api/v1${path}`,{...init,headers:{'Content-Type':'application/json',...(init?.headers||{})}}); const data=await response.json(); if(!response.ok) throw new Error(data?.detail?.message||data?.detail||'请求失败'); return data as T; };

const ENTRY_MODES: Array<{code:EntryMode; label:string; description:string; enabled:boolean; icon:typeof Bot}> = [
  {code:'agent', label:'Agent 模式', description:'对话式创作，展示完整工作过程', enabled:true, icon:Bot},
  {code:'ppt', label:'PPT 生成', description:'生成大纲并预览演示页面', enabled:true, icon:LayoutTemplate},
  {code:'design', label:'设计生成', description:'工作流尚未开放', enabled:false, icon:Palette},
  {code:'image', label:'图片生成', description:'工作流尚未开放', enabled:false, icon:Image},
];

function EntryModeMenu({value,onChange}:{value:EntryMode;onChange:(value:EntryMode)=>void}){
  const [open,setOpen]=useState(false);
  const [focused,setFocused]=useState(0);
  const rootRef=useRef<HTMLDivElement>(null);
  const selected=ENTRY_MODES.find(item=>item.code===value) || ENTRY_MODES[1];
  useEffect(()=>{const close=(event:MouseEvent)=>{if(rootRef.current&&!rootRef.current.contains(event.target as Node))setOpen(false)};document.addEventListener('mousedown',close);return()=>document.removeEventListener('mousedown',close)},[]);
  const onKeyDown=(event:React.KeyboardEvent)=>{if(event.key==='Escape'){setOpen(false);return}if(!open&&(event.key==='Enter'||event.key===' '||event.key==='ArrowDown')){event.preventDefault();setOpen(true);setFocused(Math.max(0,ENTRY_MODES.findIndex(item=>item.code===value)));return}if(!open)return;if(event.key==='ArrowDown'){event.preventDefault();setFocused(index=>(index+1)%ENTRY_MODES.length)}else if(event.key==='ArrowUp'){event.preventDefault();setFocused(index=>(index-1+ENTRY_MODES.length)%ENTRY_MODES.length)}else if(event.key==='Enter'){event.preventDefault();const item=ENTRY_MODES[focused];if(item.enabled){onChange(item.code);setOpen(false)}}};
  const Icon=selected.icon;
  return <div className="entry-mode-menu" ref={rootRef} onKeyDown={onKeyDown}>
    <button type="button" className="entry-mode-trigger" aria-haspopup="menu" aria-expanded={open} onClick={()=>setOpen(value=>!value)}><Icon size={15}/><span>{selected.label}</span>{open?<ChevronUp size={14}/>:<ChevronDown size={14}/>}</button>
    {open&&<div className="entry-mode-popover" role="menu" aria-label="选择创作模式">{ENTRY_MODES.map((item,index)=>{const ItemIcon=item.icon;return <button type="button" key={item.code} role="menuitem" aria-disabled={!item.enabled} disabled={!item.enabled} className={`entry-mode-option ${item.code===value?'selected':''} ${!item.enabled?'disabled':''} ${focused===index?'focused':''}`} onMouseEnter={()=>setFocused(index)} onClick={()=>{if(item.enabled){onChange(item.code);setOpen(false)}}}><span className="entry-mode-option-icon"><ItemIcon size={16}/></span><span className="entry-mode-option-copy"><strong>{item.label}</strong><small>{item.description}</small></span>{!item.enabled&&<em>即将上线</em>}</button>})}</div>}
  </div>;
}

function OutlineBulletsInput({ value, onChange, readOnly = false }: { value: string; onChange: (value: string) => void; readOnly?: boolean }) {
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useLayoutEffect(() => {
    const input = inputRef.current;
    const container = input?.parentElement;
    if (!input || !container) return;

    const fitContent = () => {
      input.style.height = 'auto';
      input.style.height = `${input.scrollHeight}px`;
    };
    fitContent();

    let width = container.getBoundingClientRect().width;
    const observer = new ResizeObserver(([entry]) => {
      if (Math.abs(entry.contentRect.width - width) < 1) return;
      width = entry.contentRect.width;
      fitContent();
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, [value]);

  return (
    <textarea
      ref={inputRef}
      className="outline-bullets"
      aria-label="页面要点"
      rows={1}
      value={value}
      readOnly={readOnly}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

function App(){
  const [presentations,setPresentations]=useState<any[]>([]); const [activeId,setActiveId]=useState<string|null>(()=>sessionStorage.getItem('lumina.activePresentation')); const [snapshot,setSnapshot]=useState<Snapshot|null>(null);
  const [prompt,setPrompt]=useState(''); const [loading,setLoading]=useState(false); const [error,setError]=useState('');
  const [entryMode,setEntryMode]=useState<EntryMode>('ppt');
  const [provider,setProvider]=useState<Provider|null>(null);
  const [settingsPage,setSettingsPage]=useState<SettingsTab|null>(()=>settingsTabFromPath());
  const navigateSettings=useCallback((tab:SettingsTab|null)=>{window.history.pushState({},'',tab ? '/settings/'+tab : '/');setSettingsPage(tab);},[]);
  const refreshProvider=useCallback(()=>{api<Provider>('/settings/llm/default').then(setProvider).catch(()=>{});},[]);
  useEffect(()=>{const onPop=()=>setSettingsPage(settingsTabFromPath());window.addEventListener('popstate',onPop);return()=>window.removeEventListener('popstate',onPop);},[]);
  const [doc,setDoc]=useState<Document|null>(null); const [selectedPage,setSelectedPage]=useState(0); const [view,setView]=useState<'scene'|'preview'|'source'>('scene'); const [workspaceView,setWorkspaceView]=useState<'outline'|'generation'>('outline'); const [previewOpen,setPreviewOpen]=useState(true); const [streamHtml,setStreamHtml]=useState<Record<string,string>>({}); const [directorStream,setDirectorStream]=useState<DirectorStream>({reasoning:'',output:''}); const [sourceHtml,setSourceHtml]=useState('');
  const loadedPresentationRef=useRef<string|null>(null);
  const [pageRailWidth,setPageRailWidth]=useState(220); const railResizeRef=useRef<{startX:number;startWidth:number}|null>(null);
  const sourceRef=useRef<EventSource|null>(null); const workbenchRef=useRef<HTMLElement|null>(null); const followAgentTail=useRef(true); const [sidebarOpen,setSidebarOpen]=useState(true); const [usage,setUsage]=useState<SessionUsage|null>(null); const usageRequest=useRef(0);
  const refreshList=useCallback(async()=>{try{const result=await api<{items:any[]}>('/presentations');setPresentations(result.items)}catch{}},[]);
  const loadSnapshot=useCallback(async(id:string)=>{try{const s=await api<Snapshot>(`/presentations/${id}`);const firstLoad=loadedPresentationRef.current!==id;setSnapshot(s);setEntryMode(s.presentation.entry_mode||'ppt');setDoc(s.latest_outline?.document||null);setStreamHtml(s.active_streams||{});setDirectorStream(s.director_stream||{reasoning:'',output:''});setSelectedPage(n=>Math.min(n,Math.max(0,s.pages.length-1)));if(firstLoad)setWorkspaceView(s.latest_outline?.confirmed&&s.pages.length?'generation':'outline');loadedPresentationRef.current=id;}catch(e){setError(String(e))}},[]);
  const loadUsage=useCallback(async(id:string)=>{const request=++usageRequest.current;try{const result=await api<SessionUsage>(`/presentations/${id}/usage`);if(request===usageRequest.current)setUsage(result)}catch{if(request===usageRequest.current)setUsage(null)}},[]);
  useEffect(()=>{refreshList();refreshProvider();},[refreshList,refreshProvider]);
  useEffect(()=>{if(activeId)sessionStorage.setItem('lumina.activePresentation',activeId);else sessionStorage.removeItem('lumina.activePresentation');},[activeId]);
  useEffect(()=>{if(activeId)loadSnapshot(activeId);},[activeId,loadSnapshot]);
  useEffect(()=>{setUsage(null);if(activeId)loadUsage(activeId);return()=>{usageRequest.current++}},[activeId,loadUsage]);
  useEffect(()=>{ if(!activeId||snapshot?.presentation.id!==activeId)return; sourceRef.current?.close(); const source=new EventSource(`/api/v1/presentations/${activeId}/events?after=${snapshot.last_event_seq||0}`); sourceRef.current=source;
    let refreshTimer:ReturnType<typeof setTimeout>|undefined;
    const scheduleRefresh=()=>{if(refreshTimer)return;refreshTimer=setTimeout(()=>{refreshTimer=undefined;void loadSnapshot(activeId);void refreshList();},150)};
    const handle=(event:MessageEvent)=>{try{const data=JSON.parse(event.data);const type=data.type; if(type==='director.stream.reset')setDirectorStream({reasoning:'',output:''});
      if(type==='director.stream.delta'){const channel=data.payload?.channel as string;const delta=data.payload?.delta as unknown;if(typeof delta==='string'&&(channel==='reasoning'||channel==='output'))setDirectorStream(prev=>channel==='reasoning'?({...prev,reasoning:prev.reasoning+delta}):({...prev,output:prev.output+delta}));}
      if(type==='page.html.delta'){const id=data.payload.page_task_id;setStreamHtml(prev=>({...prev,[id]:(prev[id]||'')+(data.payload.delta||'')}));}
      if(type==='page.retrying'||type==='page.started'){setStreamHtml(prev=>({...prev,[data.payload.page_task_id]:''}));}
      const pageStates:Record<string,string>={'page.queued':'WAITING_MODEL_CAPACITY','page.started':'RUNNING','page.validating':'VALIDATING','page.completed':'SUCCEEDED','page.failed':'FAILED'};
      if(pageStates[type])setSnapshot(prev=>prev?.presentation.id===activeId?({...prev,pages:prev.pages.map(page=>page.id===data.payload.page_task_id&&(!data.run_id||page.run_id===data.run_id)?({...page,status:pageStates[type],html_hash:data.payload.html_hash??page.html_hash,error:data.payload.error??null,error_code:type==='page.failed'?(data.payload.error_code??null):type==='page.started'||type==='page.retrying'?null:page.error_code}):page)}):prev);
      if(type.startsWith('page.scene.'))scheduleRefresh();
      if(type==='llm.usage.updated')loadUsage(activeId);
      if(type==='outline.ready'){setDoc(data.payload.outline);scheduleRefresh();}
      if(['run.completed','run.failed','run.cancelled','generation.queued','generation.started','outline.updated','job.waiting_scheduler','job.started','job.queue_timeout','call.queue_timeout'].includes(type))scheduleRefresh();
    }catch{} };
    ['page.scene.queued','page.scene.started','page.scene.completed','page.scene.failed','page.scene.cancelled','presentation.created','director.started','director.stream.reset','director.stream.delta','outline.ready','outline.updated','generation.queued','generation.started','page.queued','page.started','page.validating','page.html.delta','page.retrying','page.completed','page.failed','run.completed','run.failed','run.cancelled','run.queued','job.waiting_scheduler','job.started','job.queue_timeout','llm.queued','llm.started','call.queue_timeout','llm.usage.updated'].forEach(type=>source.addEventListener(type,handle as EventListener));
    source.onerror=()=>{}; return()=>{source.close();if(refreshTimer)clearTimeout(refreshTimer);};
  },[activeId,snapshot?.last_event_seq,loadSnapshot,loadUsage,refreshList]);
  const create=async()=>{if(!prompt.trim()||loading||entryMode==='design'||entryMode==='image')return;setLoading(true);setError('');try{const res=await api<{presentation_id:string;entry_mode:EntryMode}>('/presentations',{method:'POST',headers:{'Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({user_request:prompt,entry_mode:entryMode,options:{language:'zh-CN'}})});setPrompt('');await refreshList();setActiveId(res.presentation_id);}catch(e){setError(String(e));}finally{setLoading(false)}};
  const confirm=async()=>{if(!snapshot?.latest_outline||!doc)return;try{let outline=snapshot.latest_outline;if(JSON.stringify(doc)!==JSON.stringify(outline.document)){outline=await api<any>(`/presentations/${snapshot.presentation.id}/outline`,{method:'PUT',body:JSON.stringify({outline_id:outline.id,content_hash:outline.content_hash,document:doc})});}await api(`/presentations/${snapshot.presentation.id}/confirm`,{method:'POST',body:JSON.stringify({outline_id:outline.id,content_hash:outline.content_hash})});setWorkspaceView('generation');await loadSnapshot(snapshot.presentation.id)}catch(e){setError(String(e))}};
  const saveOutline=async()=>{if(!snapshot?.latest_outline||!doc)return;try{const updated=await api<any>(`/presentations/${snapshot.presentation.id}/outline`,{method:'PUT',body:JSON.stringify({outline_id:snapshot.latest_outline.id,content_hash:snapshot.latest_outline.content_hash,document:doc})});setSnapshot(s=>s?({...s,latest_outline:updated}):s)}catch(e){setError(String(e))}};
  const updateSlide=(index:number,patch:Partial<Slide>)=>setDoc(d=>d?({...d,slides:d.slides.map((s,i)=>i===index?({...s,...patch}):s)}):d);
  const startRailResize=(event:React.PointerEvent<HTMLDivElement>)=>{if(window.innerWidth<=900)return;event.currentTarget.setPointerCapture(event.pointerId);railResizeRef.current={startX:event.clientX,startWidth:pageRailWidth};};
  const moveRailResize=(event:React.PointerEvent<HTMLDivElement>)=>{const start=railResizeRef.current;if(!start)return;const max=Math.max(220,(event.currentTarget.parentElement?.clientWidth||800)-440);setPageRailWidth(Math.max(160,Math.min(max,start.startWidth+event.clientX-start.startX)));};
  const endRailResize=()=>{railResizeRef.current=null;};
  const choose=(id:string)=>{navigateSettings(null);setSnapshot(null);setDoc(null);setStreamHtml({});setDirectorStream({reasoning:'',output:''});setWorkspaceView('outline');loadedPresentationRef.current=null;followAgentTail.current=true;setActiveId(id);setError('');};
  const waitingScheduler=snapshot?.active_job?.status==='QUEUED'; const status=waitingScheduler?'WAITING_SCHEDULER':snapshot?.presentation.status||''; const isPlanning=status==='PLANNING'||(waitingScheduler&&snapshot?.active_run?.kind==='DIRECTOR'); const waiting=status==='WAITING_CONFIRMATION'; const generating=status==='GENERATING'||(waitingScheduler&&snapshot?.active_run?.kind==='GENERATION'); const isAgent=snapshot?.presentation.entry_mode==='agent'; const generationAvailable=Boolean(snapshot?.latest_outline?.confirmed&&snapshot?.pages.length); const outlineEditing=waiting;
  const currentSlide=doc?.slides[selectedPage]; const currentTask=snapshot?.pages.find(p=>p.page_number===selectedPage+1); const previewHtml=currentTask?.status==='SUCCEEDED'?`/api/v1/page-tasks/${currentTask.id}/html`:streamHtml[currentTask?.id||''];
  const displayHtml=typeof previewHtml==='string'&&previewHtml.startsWith('/')?previewHtml:null;
  useEffect(()=>{const controller=new AbortController();setSourceHtml('');if(view==='source'&&displayHtml)fetch(displayHtml,{signal:controller.signal}).then(r=>r.text()).then(text=>{if(!controller.signal.aborted)setSourceHtml(text)}).catch(()=>{if(!controller.signal.aborted)setSourceHtml('无法读取 HTML')});return()=>controller.abort()},[view,displayHtml]);
  useLayoutEffect(()=>{if(!isAgent||!followAgentTail.current)return;const element=workbenchRef.current;if(!element)return;const frame=requestAnimationFrame(()=>{element.scrollTop=element.scrollHeight});return()=>cancelAnimationFrame(frame)},[isAgent,snapshot?.conversation?.messages.length,directorStream.reasoning,directorStream.output,status,doc]);
  return <div className="app-shell">
    <aside className={`sidebar ${sidebarOpen?'':'collapsed'}`}>
      <div className="brand-row"><div className="brand-mark"><Layers3 size={17}/></div>{sidebarOpen&&<><span className="brand-name">lumina<span>draft</span></span><button className="icon-button sidebar-toggle" title="收起侧栏" onClick={()=>setSidebarOpen(false)}><PanelLeftClose size={16}/></button></>}</div>
      {sidebarOpen&&<><button className="new-button" onClick={()=>{navigateSettings(null);setActiveId(null);setSnapshot(null);setDoc(null);setEntryMode('ppt');setWorkspaceView('outline');loadedPresentationRef.current=null;followAgentTail.current=true;setError('')}}><Plus size={16}/> 新建演示文稿</button>
        <div className="side-label">工作区</div><button className={"side-item "+(!settingsPage?"active":"")} onClick={()=>navigateSettings(null)}><Sparkles size={15}/>创作工作台</button><button className={"side-item "+(settingsPage?"active":"")} onClick={()=>navigateSettings('providers')}><Settings2 size={15}/>模型与 API</button>
        <div className="side-label history-label">最近创作 <span>{presentations.length}</span></div><div className="history-list">{presentations.map(p=><button key={p.id} className={`history-item ${activeId===p.id?'selected':''}`} onClick={()=>choose(p.id)}><span className="history-mode-icon">{p.entry_mode==='agent'?<Bot size={14}/>:<LayoutTemplate size={14}/>}</span><span>{p.user_request.slice(0,26)||'未命名演示文稿'}</span><i className={`status-dot ${p.status.toLowerCase()}`}/></button>)}</div>
        <div className="sidebar-footer"><div className="avatar">L</div><div><strong>本地工作区</strong><small>单用户 · SQLite</small></div><CircleHelp size={16}/></div>
      </>}{!sidebarOpen&&<button className="icon-button expand-button" title="展开侧栏" onClick={()=>setSidebarOpen(true)}><ArrowRight size={17}/></button>}
    </aside>
    <main className="main-area">
      <header className="topbar"><div className="breadcrumbs"><span>工作区</span><span className="crumb-slash">/</span><strong>{snapshot?.latest_outline?.document.brief.title||'创作工作台'}</strong>{snapshot&&<><span className={`entry-mode-pill ${snapshot.presentation.entry_mode}`}><span>{snapshot.presentation.entry_mode==='agent'?<Bot size={12}/>:<LayoutTemplate size={12}/>}</span>{snapshot.presentation.entry_mode==='agent'?'Agent 模式':'PPT 生成'}</span><span className={`state-pill ${status.toLowerCase()}`}><i/>{statusLabel(status)}</span></>}</div><div className="top-actions"><button className="provider-chip" onClick={()=>navigateSettings('providers')}><span className="provider-indicator"/>{provider?.model||'配置模型'}<ChevronDown size={14}/></button><button className="icon-button" title="模型设置" onClick={()=>navigateSettings('providers')}><Settings2 size={17}/></button></div></header>
      {settingsPage ? <ProviderSettings tab={settingsPage} onNavigate={navigateSettings} onClose={()=>navigateSettings(null)} onChanged={refreshProvider}/> : !snapshot?<section className="welcome"><div className="welcome-kicker"><span/> PRESENTATION STUDIO</div><h1>把想法，变成<br/><em>清晰有力的演示。</em></h1><p>选择创作模式，描述主题、受众和目标。先共同确认内容结构，再逐页生成可预览的演示页面。</p>
        <div className="composer"><textarea value={prompt} onChange={e=>setPrompt(e.target.value)} onKeyDown={e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter')create()}} placeholder={entryMode==='agent'?'告诉我你想完成什么，我会用步骤化的创作过程协助你…':'例如：为创业团队制作一份关于 AI 产品市场进入策略的路演，突出客户问题、市场机会和 90 天计划…'}/><div className="composer-bottom"><div className="composer-options"><EntryModeMenu value={entryMode} onChange={setEntryMode}/><span className="composer-divider"/><span><span className="color-dot"/>中文</span></div><button className="send-button" disabled={!prompt.trim()||loading||entryMode==='design'||entryMode==='image'} onClick={create}>{loading?<LoaderCircle className="spin" size={16}/>:<Send size={15}/>}<span>{entryMode==='agent'?'开始创作':'生成说明书'}</span></button></div>
          {error&&<div className="inline-error"><X size={14}/>{error}{error.includes('配置')&&<button onClick={()=>navigateSettings('providers')}>去配置</button>}</div>}
        </div><div className="trust-line"><ShieldCheck size={14}/> {entryMode==='agent'?'Agent 模式会展示真实生成流程的阶段卡，不会自动调用额外工具。':'由你确认每页结构后，才开始生成页面'}</div>
        <div className="example-row"><span>试试这些主题</span>{['产品发布路演','季度业务复盘','新员工培训'].map(x=><button key={x} onClick={()=>setPrompt(`制作一份关于${x}的演示文稿，结构清晰、观点具体、视觉简洁专业。`)}>{x}<ArrowRight size={12}/></button>)}</div>
        </section>:<section ref={workbenchRef} onScroll={event=>{const element=event.currentTarget;followAgentTail.current=element.scrollHeight-element.scrollTop-element.clientHeight<48}} className={`workbench ${isAgent?'agent-workbench':''}`}>
         {isAgent&&<div className="agent-mode-banner"><Bot size={16}/><div><strong>Agent 模式</strong><span>以对话和阶段卡呈现本次 PPT 创作流程</span></div><span className="agent-mode-chip">对话式工作流</span></div>}
         <div className="work-heading"><div><div className="eyebrow">{waiting?'内容说明书':generating||status==='READY'||status==='PARTIAL_FAILED'?'页面创作':'任务进度'}</div><h1>{doc?.brief.title||'正在理解你的需求'}</h1><p>{doc?.brief.purpose||snapshot.presentation.user_request}</p></div><div className="work-heading-actions">{(isPlanning||generating)&&snapshot.active_run&&<button className="secondary-button" onClick={()=>api(`/runs/${snapshot.active_run!.id}/cancel`,{method:'POST'}).then(()=>loadSnapshot(snapshot.presentation.id)).catch(e=>setError(String(e)))}><X size={15}/>取消生成</button>}{doc&&waiting&&<><button className="secondary-button" onClick={saveOutline}><Check size={15}/>保存修改</button><button className="primary-button" onClick={confirm}><Sparkles size={15}/>确认并生成 {doc.slides.length} 页<ArrowRight size={15}/></button></>}{(status==='FAILED'||status==='PARTIAL_FAILED'||status==='CANCELLED')&&<button className="primary-button" onClick={()=>api(`/presentations/${snapshot.presentation.id}/retry`,{method:'POST'}).then(()=>loadSnapshot(snapshot.presentation.id)).catch(e=>setError(String(e)))}><RefreshCw size={15}/>重新生成</button>}</div></div>
         {doc&&<div className="ppt-view-switcher" role="tablist" aria-label="工作区视图"><button role="tab" aria-selected={workspaceView==='outline'} className={workspaceView==='outline'?'active':''} onClick={()=>setWorkspaceView('outline')}><LayoutTemplate size={14}/>大纲{snapshot.latest_outline?.confirmed&&<small>只读</small>}</button><button role="tab" aria-selected={workspaceView==='generation'} disabled={!generationAvailable} className={workspaceView==='generation'?'active':''} onClick={()=>generationAvailable&&setWorkspaceView('generation')}><Layers3 size={14}/>页面生成{generationAvailable&&<small>{snapshot.pages.filter(page=>page.status==='SUCCEEDED').length}/{snapshot.pages.length}</small>}</button></div>}
        {usage?.presentation_id===snapshot.presentation.id&&<div className="usage-bar" aria-label="当前作品会话 token 用量"><Activity size={14}/><span>会话用量</span><span title={usage.input_unreported_calls?`${usage.input_unreported_calls} 次调用未返回输入用量`:undefined}>输入 <strong>{formatUsage(usage.input_tokens,usage.input_unreported_calls)}</strong></span><span title={usage.output_unreported_calls?`${usage.output_unreported_calls} 次调用未返回输出用量`:undefined}>输出 <strong>{formatUsage(usage.output_tokens,usage.output_unreported_calls)}</strong></span><span title={usage.total_unreported_calls?`${usage.total_unreported_calls} 次调用未返回总用量`:undefined}>合计 <strong>{formatUsage(usage.total_tokens,usage.total_unreported_calls)}</strong></span><span>{usage.call_count} 次调用</span></div>}
        {error&&<div className="inline-error"><X size={14}/>{error}<button onClick={()=>setError('')}>关闭</button></div>}
          {isAgent&&<ConversationFeed messages={snapshot.conversation?.messages||[]} onViewOutline={()=>setWorkspaceView('outline')} onViewGeneration={()=>generationAvailable&&setWorkspaceView('generation')}/>}
         {isAgent&&<div className="agent-flow-strip"><div className="agent-flow-step active"><span>1</span><div><strong>理解需求</strong><small>Director 正在整理创作目标</small></div></div><div className="agent-flow-line"/><div className={`agent-flow-step ${waiting||generating||status==='READY'?'active':''}`}><span>2</span><div><strong>确认大纲</strong><small>先确认内容结构，再生成页面</small></div></div><div className="agent-flow-line"/><div className={`agent-flow-step ${generating||status==='READY'?'active':''}`}><span>3</span><div><strong>生成页面</strong><small>按页制作可预览 HTML</small></div></div></div>}
        {isPlanning&&!doc?<div className="planning-panel"><div className="planning-visual"><div className="orbit orbit-one"/><div className="orbit orbit-two"/><Sparkles size={25}/></div><div className="planning-content"><h2>正在梳理内容结构</h2><p>Director 正在确定叙事主线、页面大纲与全局视觉规范。</p><div className="progress-track"><i/></div><small>说明书完成后，你可以逐页编辑和确认</small><DirectorStreamView stream={directorStream} active={isPlanning}/></div></div>:null}
        {snapshot.director_stream&&(waiting||generating||status==='READY'||status==='PARTIAL_FAILED'||status==='CANCELLED'||(status==='FAILED'&&snapshot.latest_run?.kind==='DIRECTOR'))&&<DirectorStreamView stream={directorStream} active={false} defaultOpen={false}/>}
        {status==='FAILED'&&<div className="inline-error"><X size={14}/>{snapshot.latest_run?.error||'生成失败，请检查模型配置后重试。'}</div>}
        {doc && workspaceView === 'outline' && (
          <div className="outline-layout">
            <div className="outline-main">
              <div className="section-heading">
                <div>
                  <span className="section-index">01</span>
                  <h2>叙事结构</h2>
                </div>
                <span>{doc.slides.length} 页 · 可编辑</span>
              </div>
              <div className="slide-outline-list">
                {doc.slides.map((slide, index) => (
                  <article
                    className={`outline-card ${selectedPage === index ? 'focused' : ''}`}
                    key={slide.slide_id}
                    onClick={() => setSelectedPage(index)}
                  >
                    <div className="outline-number">{String(index + 1).padStart(2, '0')}</div>
                    <div className="outline-content">
                      <div className="outline-meta">
                        <span>{pageType(slide.page_type)}</span>
                        <i />核心观点
                      </div>
                      <input
                        className="outline-title-input"
                        value={slide.title}
                        readOnly={!outlineEditing}
                        onChange={(event) => updateSlide(index, { title: event.target.value })}
                      />
                      <input
                        className="key-message-input"
                        value={slide.key_message}
                        readOnly={!outlineEditing}
                        onChange={(event) =>
                          updateSlide(index, { key_message: event.target.value })
                        }
                      />
                      <OutlineBulletsInput
                        value={slide.content_outline.join('\n')}
                        readOnly={!outlineEditing}
                        onChange={(value) =>
                          updateSlide(index, {
                            content_outline: value.split('\n').filter(Boolean),
                          })
                        }
                      />
                    </div>
                    <div className="outline-card-tail">
                      <span>{slide.content_outline.length} 个要点</span>
                      <ArrowRight size={15} />
                    </div>
                  </article>
                ))}
              </div>
            </div>
          <aside className="outline-aside"><div className="aside-block"><div className="aside-title"><span className="aside-icon purple"><Sparkles size={15}/></span>作品 Brief {outlineEditing?null:<small className="readonly-note">已确认 · 只读</small>}</div><label>面向对象<input readOnly={!outlineEditing} value={doc.brief.audience} onChange={e=>setDoc({...doc,brief:{...doc.brief,audience:e.target.value}})}/></label><label>内容语气<input readOnly={!outlineEditing} value={doc.brief.tone} onChange={e=>setDoc({...doc,brief:{...doc.brief,tone:e.target.value}})}/></label><label>制作目的<textarea readOnly={!outlineEditing} value={doc.brief.purpose} onChange={e=>setDoc({...doc,brief:{...doc.brief,purpose:e.target.value}})} rows={3}/></label></div>
            <div className="aside-block style-block"><div className="aside-title"><span className="aside-icon green"><Layers3 size={15}/></span>全局视觉规范</div><p>{doc.style_spec.description}</p><div className="swatch-row">{Object.entries(doc.style_spec.colors).slice(0,5).map(([name,color])=><span title={`${name} ${color}`} key={name} style={{backgroundColor:color}}/>)}</div><ul>{doc.style_spec.layout_rules.slice(0,3).map(x=><li key={x}>{x}</li>)}</ul></div>
            {doc.brief.assumptions.length>0&&<div className="assumption-note"><CircleHelp size={14}/><span>{doc.brief.assumptions.join('；')}</span></div>}
          </aside></div>)}
        {doc&&workspaceView==='generation'&&generationAvailable&&<div className={`generation-layout ${previewOpen?'':'preview-hidden'}`} style={previewOpen?{gridTemplateColumns:`${pageRailWidth}px minmax(0,1fr)`}:undefined}><aside className="page-rail"><div className="section-heading compact"><div><span className="section-index">02</span><h2>页面</h2></div><span>{snapshot.pages.filter(p=>p.status==='SUCCEEDED').length}/{snapshot.pages.length}</span></div><div className="page-rail-list">{doc.slides.map((s,i)=>{const p=snapshot.pages.find(x=>x.page_number===i+1);return <button key={s.slide_id} className={`page-thumb ${i===selectedPage?'active':''}`} onClick={()=>setSelectedPage(i)}><span className="thumb-number">{String(i+1).padStart(2,'0')}</span><span className="thumb-title">{s.title}</span><span className={`thumb-state ${p?.status?.toLowerCase()||'pending'}`}><PageStatusIcon status={p?.status} sceneStatus={p?.scene?.status}/></span></button>})}</div><div className="rail-footer"><Activity size={14}/>{generating||snapshot.pages.some(p=>p.scene&&['QUEUED','RUNNING','FAILED'].includes(p.scene.status))?<GenerationProgress pages={snapshot.pages}/>:status==='CANCELLED'?'已取消':'生成完成'}</div><div className="rail-resize-handle" role="separator" aria-label="调整页面列表宽度" onPointerDown={startRailResize} onPointerMove={moveRailResize} onPointerUp={endRailResize} onPointerCancel={endRailResize}/></aside>
          <div className="canvas-column"><div className="canvas-toolbar"><div className="canvas-page-label"><strong>{String(selectedPage+1).padStart(2,'0')}</strong><span>/</span>{String(doc.slides.length).padStart(2,'0')}<i/>{currentSlide?.title}</div><div className="canvas-controls"><div className="segmented"><button className={view==='scene'?'selected':''} onClick={()=>setView('scene')}>新版预览</button><button className={view==='preview'?'selected':''} onClick={()=>setView('preview')}>HTML 预览</button><button className={view==='source'?'selected':''} onClick={()=>setView('source')}>HTML 源码</button></div><span className="canvas-size">800 × 450</span><button className="icon-button" title="上一页" disabled={selectedPage===0} onClick={()=>setSelectedPage(x=>Math.max(0,x-1))}><ArrowLeft size={15}/></button><button className="icon-button" title="下一页" disabled={selectedPage===doc.slides.length-1} onClick={()=>setSelectedPage(x=>Math.min(doc.slides.length-1,x+1))}><ArrowRight size={15}/></button><button className="icon-button" title="隐藏预览" onClick={()=>setPreviewOpen(false)}><PanelRightClose size={15}/></button></div></div>
            <div className="canvas-stage">{currentTask?.status==='SUCCEEDED'&&currentTask.html_hash&&view==='scene'?<SceneViewport key={currentTask.id+'/'+(currentTask.scene?.scene_id||'pending')} pageId={currentTask.id} htmlHash={currentTask.html_hash} scene={currentTask.scene} number={selectedPage+1} onScene={scene=>setSnapshot(prev=>prev?({...prev,pages:prev.pages.map(p=>p.id===currentTask.id?({...p,scene}):p)}):prev)}/>:currentTask?.status==='SUCCEEDED'&&displayHtml&&view==='preview'?<SlideFrame url={displayHtml} number={selectedPage+1} sceneId={currentTask.scene?.scene_id}/>:view==='source'?<pre className="source-view">{displayHtml?sourceHtml||'正在读取 HTML…':typeof previewHtml==='string'?previewHtml:'等待页面生成…'}</pre>:<div className="empty-canvas">{currentTask?.status==='WAITING_MODEL_CAPACITY'||currentTask?.status==='WAITING_CAPACITY'||currentTask?.status==='VALIDATING'?<><PageStatusIcon status={currentTask.status}/><span>{pageStatusLabel(currentTask.status)}</span></>:currentTask?.status==='RUNNING'?<><LoaderCircle className="spin" size={20}/><span>正在创作第 {selectedPage+1} 页</span><small>页面完成后会自动显示</small></>:currentTask?.status==='FAILED'?<><X size={20}/><span>这一页生成失败</span><small>{currentTask.error}</small></>:<><div className="empty-page-icon"><FileText size={21}/></div><span>等待这一页开始生成</span></>}</div>}</div>
             <div className="canvas-footer"><div className="render-status"><i className={currentTask?.status==='SUCCEEDED'?'success':''}/>{pageStatusLabel(currentTask?.status,currentTask?.scene?.status)}</div><span>固定画布 · 16:9</span></div></div></div>}
        {doc&&workspaceView==='generation'&&generationAvailable&&!previewOpen&&<button className="preview-restore-button" onClick={()=>setPreviewOpen(true)}><PanelRightOpen size={15}/>显示预览</button>}
      </section>}
    </main>

  </div>
}

function settingsTabFromPath(): SettingsTab|null {const segment=window.location.pathname.split('/')[2];return window.location.pathname.startsWith('/settings/') && ['providers','routing','calls'].includes(segment) ? segment as SettingsTab : null;}

function statusLabel(s:string){return ({PLANNING:'策划中',WAITING_SCHEDULER:'等待任务调度',WAITING_CONFIRMATION:'待确认',GENERATING:'生成中',READY:'已完成',PARTIAL_FAILED:'部分失败',FAILED:'生成失败',CANCELLED:'已取消'} as Record<string,string>)[s]||s}
function pageType(s:string){return ({cover:'封面',agenda:'目录',section:'章节',content:'内容页',ending:'结束页',other:'内容页'} as Record<string,string>)[s]||'内容页'}
function formatUsage(value:number|null,unreported:number){return value===null?'—':`${unreported?'≥':''}${value.toLocaleString()}`}

function ConversationFeed({messages,onViewOutline,onViewGeneration}:{messages:ConversationMessage[];onViewOutline:()=>void;onViewGeneration:()=>void}){
  if(!messages.length)return null;
  const text=(value:unknown,fallback:string)=>typeof value==='string'&&value.trim()?value:fallback;
  return <div className="conversation-feed" aria-label="持久化对话记录">
    {messages.map(message=>{
      const payload=message.payload||{};
      if(message.kind==='text')return <div className="agent-user-message" key={message.id}><div className="agent-avatar"><Bot size={15}/></div><div><span>你的需求</span><p>{message.body_text||''}</p></div></div>;
      if(message.kind==='activity')return <div className={`conversation-activity ${message.status.toLowerCase()}`} key={message.id}><div className="conversation-activity-icon"><Activity size={14}/></div><div><strong>{text(payload.label,'正在处理你的需求')}</strong><small>{message.status==='STREAMING'?'进行中':'已完成'}</small></div>{message.status==='STREAMING'&&<LoaderCircle className="spin" size={14}/>}</div>;
      if(message.kind==='outline_card')return <button className={`conversation-special-card outline-special-card ${message.status.toLowerCase()}`} key={message.id} onClick={onViewOutline}><span className="special-card-icon"><LayoutTemplate size={16}/></span><span className="special-card-copy"><strong>大纲已生成</strong><small>{message.status==='READY'?'可以查看并确认内容结构':'正在整理内容结构'}</small></span><span className="special-card-action">查看大纲 <ArrowRight size={13}/></span></button>;
      if(message.kind==='action')return <div className="conversation-action" key={message.id}><Check size={13}/>{payload.action==='confirm_outline'?'你已确认大纲':payload.action==='retry_run'?'你发起了重新生成':'已执行操作'}</div>;
      if(message.kind==='generation_card')return <button className={`conversation-special-card generation-special-card ${message.status.toLowerCase()}`} key={message.id} onClick={onViewGeneration}><span className="special-card-icon"><Layers3 size={16}/></span><span className="special-card-copy"><strong>页面生成工作台</strong><small>{typeof payload.page_count==='number'?`${payload.page_count} 页 · `:''}{message.status==='STREAMING'?'正在生成页面':message.status==='READY'?'生成已完成':message.status==='FAILED'?'部分页面生成失败':'已取消'}</small></span><span className="special-card-action">打开工作台 <ArrowRight size={13}/></span></button>;
      return null;
    })}
  </div>;
}

function DirectorStreamView({stream,active,defaultOpen=true}:{stream:DirectorStream;active:boolean;defaultOpen?:boolean}){
  const [open,setOpen]=useState(defaultOpen);
  const reasoning=stream.reasoning;
  const output=stream.output;
  return <section className="director-stream" aria-label="大纲生成过程">
    <button className="director-stream-toggle" onClick={()=>setOpen(value=>!value)} aria-expanded={open}>
      <span><Activity size={14}/>{active?'生成过程':'生成结果'}</span><span>{open?'收起':'展开'}<ChevronDown size={14}/></span>
    </button>
    {open&&<div className="director-stream-body">
      <div className="director-stream-channel reasoning-channel"><div className="director-stream-label"><span>思考过程</span>{active&&!reasoning&&<small>等待模型输出…</small>}{active&&reasoning&&<i/>}</div><StreamText text={reasoning||'当前供应商未返回 reasoning 文本。'}/></div>
      <div className="director-stream-channel output-channel"><div className="director-stream-label"><span>大纲正文</span>{active&&output&&<i/>}</div><StreamText text={output||'等待结构化内容…'} code/></div>
    </div>}
  </section>
}

function StreamText({text,code=false}:{text:string;code?:boolean}){
  const ref=useRef<HTMLPreElement>(null);
  const followTail=useRef(true);
  useEffect(()=>{const element=ref.current;if(element&&followTail.current)element.scrollTop=element.scrollHeight},[text]);
  return <pre ref={ref} className={code?'stream-code':''} onScroll={event=>{const element=event.currentTarget;followTail.current=element.scrollHeight-element.scrollTop-element.clientHeight<28}}>{text}</pre>
}

function SlideFrame({url,number,sceneId}:{url:string;number:number;sceneId?:string|null}){const ref=useRef<HTMLDivElement>(null);const [scale,setScale]=useState(1);const [ticketUrl,setTicketUrl]=useState('');useEffect(()=>{const element=ref.current;if(!element)return;const observer=new ResizeObserver(()=>setScale(Math.min(element.clientWidth/800,element.clientHeight/450)));observer.observe(element);return()=>observer.disconnect()},[]);useEffect(()=>{let live=true;setTicketUrl('');const id=url.split('/').at(-2);api<{preview_url:string}>(`/page-tasks/${id}/preview-tickets${sceneId?'?scene_id='+encodeURIComponent(sceneId):''}`,{method:'POST'}).then(result=>{if(live)setTicketUrl(result.preview_url)}).catch(()=>{});return()=>{live=false}},[url,sceneId]);return <div ref={ref} className="slide-viewport">{ticketUrl&&<iframe className="slide-frame" title={`第 ${number} 页预览`} src={ticketUrl} sandbox="" style={{transform:`scale(${scale})`}}/>}</div>}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
