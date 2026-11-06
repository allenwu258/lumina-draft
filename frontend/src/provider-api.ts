export type ReasoningTier = {
  code: string;
  label: string;
  api_value: string | null;
  enabled: boolean;
  sort_order: number;
};
export type ModelCheck = {
  name: 'streaming_text' | 'structured_output';
  status: string;
  error_code?: string;
  message?: string;
  duration_ms: number;
};
export type ModelTest = {
  id?: string;
  status: string;
  stale?: boolean;
  checks: ModelCheck[];
  started_at?: string;
};
export type ProviderModel = {
  id: string;
  provider_id: string;
  remote_model_id: string;
  display_name: string;
  context_window_tokens: number | null;
  max_output_tokens: number | null;
  max_concurrency: number;
  enabled: boolean;
  revision: number;
  metadata_source: string;
  capability_source: string;
  reasoning_tiers: ReasoningTier[];
  last_test: ModelTest | null;
};
export type ApiProvider = {
  id: string;
  name: string;
  protocol: 'openai_responses';
  base_url: string;
  auth_mode: 'bearer' | 'none';
  credential_configured: boolean;
  credential_version: number | null;
  credential_store_available?: boolean;
  allow_private_http: boolean;
  max_concurrency: number;
  enabled: boolean;
  revision: number;
  models: ProviderModel[];
};
export type ProviderFields = Pick<
  ApiProvider,
  | 'name'
  | 'protocol'
  | 'base_url'
  | 'auth_mode'
  | 'allow_private_http'
  | 'max_concurrency'
  | 'enabled'
>;
export type ModelFields = Pick<
  ProviderModel,
  | 'remote_model_id'
  | 'display_name'
  | 'context_window_tokens'
  | 'max_output_tokens'
  | 'max_concurrency'
>;
export type ModelCandidate = ModelFields & { metadata_source: string; already_added: boolean };
export type RouteCode = 'default' | 'ppt_outline' | 'ppt_html';
export type ResolvedRoute = {
  provider_id: string;
  provider_name: string;
  model_id: string;
  model_name: string;
  remote_model_id: string;
  reasoning_tier: string;
  reasoning_effort: string | null;
  provider_concurrency: number;
  model_concurrency: number;
};
export type BusinessRoute = {
  code: RouteCode;
  label: string;
  model_id: string | null;
  reasoning_tier: string | null;
  revision: number;
  ready: boolean;
  resolved: ResolvedRoute | null;
  issues?: string[];
};
export type ModelCall = {
  id: string;
  purpose: string;
  business_code: string | null;
  model: string;
  status: string;
  error: string | null;
  error_code: string | null;
  started_at: string;
  queue_duration_ms: number | null;
  total_duration_ms: number | null;
};
export type CallDetail = ModelCall & {
  usage: { input_tokens: number | null; output_tokens: number | null; total_tokens: number | null };
  config_snapshot: ResolvedRoute | null;
};

export class ApiError extends Error {
  constructor(
    message: string,
    public code: string,
    public status: number,
  ) {
    super(message);
  }
}

export async function providerRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1/settings/llm${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  });
  const data = await response.json();
  if (!response.ok) {
    const detail = data.detail;
    const message =
      typeof detail === 'string'
        ? detail
        : Array.isArray(detail)
          ? detail
              .map(
                (item: { loc?: string[]; msg: string }) =>
                  `${item.loc?.slice(1).join('.') || '字段'}：${item.msg}`,
              )
              .join('；')
          : detail?.message || '操作失败，请重试';
    throw new ApiError(message, detail?.code || 'REQUEST_FAILED', response.status);
  }
  return data as T;
}

const send = <T>(path: string, method: string, body?: unknown) =>
  providerRequest<T>(path, {
    method,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
export const providerApi = {
  list: () => providerRequest<{ items: ApiProvider[] }>('/providers'),
  create: (fields: ProviderFields, apiKey?: string) =>
    send<ApiProvider>('/providers', 'POST', { ...fields, api_key: apiKey || null }),
  update: (id: string, fields: ProviderFields, revision: number) =>
    send<ApiProvider>(`/providers/${id}`, 'PATCH', { ...fields, expected_revision: revision }),
  archive: (id: string, revision: number) =>
    send(`/providers/${id}?expected_revision=${revision}`, 'DELETE'),
  credential: (id: string, apiKey: string, revision: number) =>
    send(`/providers/${id}/credential`, 'PUT', { api_key: apiKey, expected_revision: revision }),
  revoke: (id: string, revision: number) =>
    send(`/providers/${id}/credential?expected_revision=${revision}`, 'DELETE'),
  discover: (id: string, revision: number) =>
    send<{ items: ModelCandidate[] }>(
      `/providers/${id}/discover-models?expected_revision=${revision}`,
      'POST',
    ),
  addModel: (id: string, fields: ModelFields, source = 'manual') =>
    send<ProviderModel>(`/providers/${id}/models`, 'POST', { ...fields, metadata_source: source }),
  updateModel: (id: string, fields: ModelFields, enabled: boolean, revision: number) =>
    send<ProviderModel>(`/models/${id}`, 'PATCH', {
      ...fields,
      enabled,
      expected_revision: revision,
    }),
  archiveModel: (id: string, revision: number) =>
    send(`/models/${id}?expected_revision=${revision}`, 'DELETE'),
  tiers: (id: string, tiers: ReasoningTier[], revision: number) =>
    send<{ model_id: string; revision: number; tiers: ReasoningTier[] }>(
      `/models/${id}/reasoning-tiers`,
      'PUT',
      {
        tiers: tiers.map(({ code, label, api_value, enabled }, sort_order) => ({
          code,
          label,
          api_value,
          enabled,
          sort_order,
        })),
        expected_revision: revision,
      },
    ),
  test: (id: string) => send<ModelTest>(`/models/${id}/test`, 'POST'),
  routes: () => providerRequest<{ items: BusinessRoute[] }>('/business-routes'),
  route: (
    code: RouteCode,
    model_id: string | null,
    reasoning_tier: string | null,
    revision: number,
  ) =>
    send(`/business-routes/${code}`, 'PUT', {
      model_id,
      reasoning_tier,
      expected_revision: revision,
    }),
  calls: (cursor?: string) =>
    providerRequest<{ items: ModelCall[]; next_cursor: string | null }>(
      `/calls?limit=25${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`,
    ),
  call: (id: string) => providerRequest<CallDetail>(`/calls/${id}`),
};

export const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : '操作失败，请重试';
export function modelAvailability(
  provider: ApiProvider,
  model: ProviderModel,
  code?: RouteCode,
): string | null {
  if (!provider.enabled || !model.enabled) return '已停用';
  if (provider.auth_mode === 'bearer' && !provider.credential_configured) return '缺少 API Key';
  if (code) {
    const checks = model.last_test?.stale ? [] : model.last_test?.checks || [];
    const required =
      code === 'ppt_outline' ? ['streaming_text', 'structured_output'] : ['streaming_text'];
    if (
      required.some(
        (name) => !checks.some((check) => check.name === name && check.status === 'passed'),
      )
    )
      return '请先通过模型测试';
  }
  return null;
}
