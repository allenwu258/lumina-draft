import type { SceneIR } from '@lumina/scene-core';
export type SceneSummary = {
  status: string;
  job_id: string | null;
  scene_id: string | null;
  scene_hash: string | null;
  source_html_hash: string | null;
  error_code: string | null;
  error: string | null;
  warning_count: number;
};
export async function sceneRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch('/api/v1' + path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  });
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail?.message || '场景请求失败');
  }
  return response.json();
}
export const ensureScene = (id: string, hash: string, retry = false, signal?: AbortSignal) =>
  sceneRequest<SceneSummary>(`/page-tasks/${id}/scene-conversions`, {
    method: 'POST',
    body: JSON.stringify({ expected_html_hash: hash, retry }),
    signal,
  });
export const fetchScene = (id: string, signal: AbortSignal) =>
  sceneRequest<SceneIR>(`/scenes/${id}`, { signal });
