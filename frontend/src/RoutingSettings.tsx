import { useState } from 'react';
import { Check, ChevronRight, GitBranch, LoaderCircle, ShieldCheck } from 'lucide-react';
import {
  type ApiProvider,
  type BusinessRoute,
  errorMessage,
  modelAvailability,
  providerApi,
} from './provider-api';
import { Notice } from './SettingsNotice';

export function RoutingSettings({
  providers,
  routes,
  refresh,
}: {
  providers: ApiProvider[];
  routes: BusinessRoute[];
  refresh: () => Promise<void>;
}) {
  return (
    <div className="routing-settings">
      <div className="settings-info">
        <ShieldCheck size={18} />
        <div>
          <strong>配置在新作品创建时冻结</strong>
          <p>
            模型和思考预算分别继承。已有作品及其重试继续使用原快照；撤销凭据或停用连接会阻止后续调用。
          </p>
        </div>
      </div>
      {routes.map((route) => (
        <RouteEditor
          key={route.code + '-' + route.revision}
          route={route}
          providers={providers}
          defaultRoute={routes.find((item) => item.code === 'default')}
          refresh={refresh}
        />
      ))}
    </div>
  );
}

function RouteEditor({
  route,
  providers,
  defaultRoute,
  refresh,
}: {
  route: BusinessRoute;
  providers: ApiProvider[];
  defaultRoute?: BusinessRoute;
  refresh: () => Promise<void>;
}) {
  const [modelId, setModelId] = useState(route.model_id || '');
  const [tier, setTier] = useState(route.reasoning_tier || '');
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const effectiveId = modelId || defaultRoute?.model_id;
  const provider = providers.find((item) => item.models.some((model) => model.id === effectiveId));
  const model = provider?.models.find((item) => item.id === effectiveId);
  const effectiveTier = tier || defaultRoute?.reasoning_tier;
  const selectedTier = model?.reasoning_tiers.find((item) => item.code === effectiveTier);
  const tiers = model?.reasoning_tiers.filter((item) => item.enabled) || [];
  const invalidTier = !!effectiveTier && !tiers.some((item) => item.code === effectiveTier);
  const modelIssue =
    provider && model ? modelAvailability(provider, model, route.code) : '请选择模型';
  const changed = modelId !== (route.model_id || '') || tier !== (route.reasoning_tier || '');
  async function save() {
    setSaving(true);
    setMessage('');
    try {
      await providerApi.route(route.code, modelId || null, tier || null, route.revision);
      await refresh();
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }
  return (
    <article className="settings-card route-card">
      <header>
        <div className={'route-icon ' + (route.code === 'default' ? 'global' : '')}>
          <GitBranch size={20} />
        </div>
        <div>
          <h2>{route.label}</h2>
          <p>
            {route.code === 'default'
              ? '未单独配置的业务使用此模型与思考预算'
              : route.code === 'ppt_outline'
                ? 'Director · 生成结构化作品说明书'
                : 'Page Composer · 逐页生成 HTML 内容'}
          </p>
        </div>
        <span className="settings-badge">
          {route.code === 'default' ? '全局配置' : modelId || tier ? '独立覆盖' : '跟随默认'}
        </span>
      </header>
      <div className="route-fields">
        <label>
          模型
          <select
            aria-label="模型"
            value={modelId}
            onChange={(event) => setModelId(event.target.value)}
          >
            <option value="">
              {route.code === 'default' ? '选择默认模型' : '跟随全局默认模型'}
            </option>
            {providers.map((provider) => (
              <optgroup label={provider.name} key={provider.id}>
                {provider.models.map((model) => {
                  const issue = modelAvailability(provider, model, route.code);
                  return (
                    <option key={model.id} value={model.id} disabled={!!issue}>
                      {model.display_name}
                      {model.display_name !== model.remote_model_id
                        ? ' · ' + model.remote_model_id
                        : ''}
                      {issue ? '（' + issue + '）' : ''}
                    </option>
                  );
                })}
              </optgroup>
            ))}
          </select>
        </label>
        <label>
          思考预算
          <select
            aria-label="思考预算"
            value={tier}
            onChange={(event) => setTier(event.target.value)}
          >
            <option value="" disabled={route.code === 'default'}>
              {route.code === 'default'
                ? '选择思考预算'
                : '继承全局预算（' + (defaultRoute?.reasoning_tier || '未配置') + '）'}
            </option>
            {tiers.map((item) => (
              <option key={item.code} value={item.code}>
                {item.label}
                {item.code === 'off' ? ' · 不发送 reasoning' : ''}
              </option>
            ))}
            {tier && !tiers.some((item) => item.code === tier) && (
              <option value={tier}>{tier}（当前模型不可用，请重新选择）</option>
            )}
          </select>
        </label>
      </div>
      <div className="route-resolution">
        <span>生效配置</span>
        <strong>
          {provider?.name || '—'}
          <ChevronRight size={12} />
          {model?.display_name || '未选择模型'}
        </strong>
        <small>
          预算 {effectiveTier || '—'} · Provider 总并发 {provider?.max_concurrency ?? '—'}
          （所有模型共享）
          {selectedTier && ' · API effort: ' + (selectedTier.api_value || '省略')}
        </small>
      </div>
      {(invalidTier || modelIssue) && (
        <Notice error>
          {invalidTier ? '当前模型不支持所选预算，请显式选择有效档位。' : modelIssue}
        </Notice>
      )}
      {message && <Notice error>{message}</Notice>}
      <footer>
        <small>
          revision {route.revision}
          {route.ready ? ' · 已就绪' : ' · 待配置或测试'}
        </small>
        <div className="button-row">
          {route.code !== 'default' && (
            <button
              className="secondary-button"
              disabled={saving}
              onClick={() => {
                setModelId('');
                setTier('');
              }}
            >
              恢复默认
            </button>
          )}
          <button
            className="primary-button"
            disabled={
              saving ||
              !changed ||
              invalidTier ||
              !!modelIssue ||
              (route.code === 'default' && (!modelId || !tier))
            }
            onClick={save}
          >
            {saving ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}保存路由
          </button>
        </div>
      </footer>
    </article>
  );
}
