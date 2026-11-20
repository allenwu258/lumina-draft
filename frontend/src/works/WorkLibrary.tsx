import { useEffect, useState } from 'react';
import { ArrowLeft, FileText, FolderOpen, LoaderCircle } from 'lucide-react';
import { sceneRequest } from '../scene/scene-api';
import './work-editor.css';

type WorkItem = {
  id: string;
  name: string;
  type: string;
  revision: number;
  page_count: number;
  updated_at: string;
};

export function WorkLibrary() {
  const [works, setWorks] = useState<WorkItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    sceneRequest<WorkItem[]>('/works', { signal: controller.signal })
      .then((items) => {
        if (!controller.signal.aborted) setWorks(items);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  return (
    <main className="work-library">
      <header className="work-library-header">
        <a className="work-editor-back" href="/" aria-label="返回创作工作台"><ArrowLeft size={17} /></a>
        <div><FolderOpen size={18} /><h1>我的作品</h1></div>
      </header>
      {loading ? (
        <div className="work-library-state"><LoaderCircle className="spin" size={19} />正在加载作品</div>
      ) : error ? (
        <div className="work-library-state" role="alert">{error}</div>
      ) : works.length ? (
        <ul className="work-library-list">
          {works.map((work) => (
            <li key={work.id}>
              <button onClick={() => window.location.assign(`/edit/${work.id}`)}>
                <FileText size={18} />
                <span><strong>{work.name}</strong><small>{work.page_count} 页 · 已保存 v{work.revision}</small></span>
                <time dateTime={work.updated_at}>{new Date(work.updated_at).toLocaleString()}</time>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <div className="work-library-state">还没有已创建的作品</div>
      )}
    </main>
  );
}
