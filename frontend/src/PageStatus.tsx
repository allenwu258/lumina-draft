import { CheckCircle2, Clock3, LoaderCircle, ShieldCheck, X } from 'lucide-react';

export function pageStatusLabel(status?: string, sceneStatus?: string): string {
  if (status === 'SUCCEEDED') {
    if (sceneStatus === 'QUEUED') return '等待转换';
    if (sceneStatus === 'RUNNING') return '正在转换';
    if (sceneStatus === 'FAILED') return '转换失败';
    if (sceneStatus === 'CANCELLED') return '转换已取消';
    if (sceneStatus === 'NOT_REQUESTED') return 'HTML 已就绪';
  }
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

export function PageStatusIcon({ status, sceneStatus }: { status?: string; sceneStatus?: string }) {
  const label = pageStatusLabel(status, sceneStatus);
  if (status === 'SUCCEEDED' && ['QUEUED', 'RUNNING'].includes(sceneStatus || ''))
    return <Clock3 size={14} aria-label={label} />;
  if (status === 'SUCCEEDED' && ['FAILED', 'CANCELLED'].includes(sceneStatus || ''))
    return <X size={14} aria-label={label} />;
  if (status === 'RUNNING') return <LoaderCircle size={14} className="spin" aria-label={label} />;
  if (status === 'SUCCEEDED') return <CheckCircle2 size={14} aria-label={label} />;
  if (status === 'FAILED' || status === 'CANCELLED') return <X size={14} aria-label={label} />;
  if (status === 'VALIDATING') return <ShieldCheck size={14} aria-label={label} />;
  return <Clock3 size={14} aria-label={label} />;
}

export function GenerationProgress({
  pages,
}: {
  pages: { status: string; scene?: { status: string } }[];
}) {
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
      {pages.some((p) => p.scene && ['QUEUED', 'RUNNING', 'FAILED'].includes(p.scene.status)) && (
        <span>
          转换中{' '}
          {pages.filter((p) => p.scene && ['QUEUED', 'RUNNING'].includes(p.scene.status)).length} ·
          转换失败 {pages.filter((p) => p.scene?.status === 'FAILED').length}
        </span>
      )}
    </span>
  );
}
