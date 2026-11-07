import { CheckCircle2, Clock3, LoaderCircle, ShieldCheck, X } from 'lucide-react';

export function pageStatusLabel(status?: string): string {
  switch (status) {
    case 'RUNNING':
      return '正在生成';
    case 'WAITING_CAPACITY':
    case 'WAITING_MODEL_CAPACITY':
      return '等待模型调用名额';
    case 'VALIDATING':
      return '正在校验 HTML';
    case 'SUCCEEDED':
      return '页面已就绪';
    case 'FAILED':
      return '生成失败';
    case 'CANCELLED':
      return '已取消';
    default:
      return '等待生成';
  }
}

export function PageStatusIcon({ status }: { status?: string }) {
  const label = pageStatusLabel(status);
  if (status === 'RUNNING') return <LoaderCircle size={14} className="spin" aria-label={label} />;
  if (status === 'SUCCEEDED') return <CheckCircle2 size={14} aria-label={label} />;
  if (status === 'FAILED' || status === 'CANCELLED') return <X size={14} aria-label={label} />;
  if (status === 'VALIDATING') return <ShieldCheck size={14} aria-label={label} />;
  return <Clock3 size={14} aria-label={label} />;
}

export function GenerationProgress({ pages }: { pages: { status: string }[] }) {
  const count = (statuses: string[]) =>
    pages.filter((page) => statuses.includes(page.status)).length;
  return (
    <span className="generation-progress" aria-live="polite">
      <span>
        生成中 {count(['RUNNING'])} · 等待调度 {count(['PENDING'])} · 等待模型{' '}
        {count(['WAITING_MODEL_CAPACITY', 'WAITING_CAPACITY'])}
      </span>
      <span>
        校验 {count(['VALIDATING'])} · 完成 {count(['SUCCEEDED'])}
      </span>
    </span>
  );
}
