import { useState } from 'react';
import { LayoutTemplate } from 'lucide-react';

type Version = { brief: {title: string; purpose: string}; slides: {slide_id: string; title: string; key_message: string; content_outline: string[]}[] };
type Props = { path: string; revision: number; current: boolean; openEditor: () => void };

export function OutlineVersionCard({path, revision, current, openEditor}: Props) {
  const [version, setVersion] = useState<Version | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [error, setError] = useState('');
  async function toggle() {
    setExpanded(!expanded);
    if (expanded || version) return;
    try {
      const response = await fetch(path);
      if (!response.ok) throw new Error('暂时无法读取此版本，请重试');
      const data = await response.json();
      setVersion(data.document);
      setError('');
    } catch (cause) { setError(String(cause)); }
  }
  return <section className="outline-version-card">
    <button className="conversation-special-card outline-special-card" onClick={toggle} aria-expanded={expanded}>
      <LayoutTemplate size={16}/><span className="special-card-copy"><strong>大纲 V{revision}</strong>
      <small>{current ? '当前版本' : '历史版本'}</small></span>{expanded ? '收起' : '查看大纲'}
    </button>
    {expanded && <div className="outline-version-content">
      {version ? <><strong>{version.brief.title}</strong><p>{version.brief.purpose}</p><ol>{version.slides.map(slide =>
        <li key={slide.slide_id}><strong>{slide.title}</strong><p>{slide.key_message}</p><ul>{slide.content_outline.map((point,index)=><li key={index}>{point}</li>)}</ul></li>)}</ol></> : <p>{error || '正在读取大纲…'}</p>}
      {current && <button className="secondary-button" onClick={openEditor}>打开当前大纲</button>}
    </div>}
  </section>;
}
