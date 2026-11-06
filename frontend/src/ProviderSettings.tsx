import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowLeft,
  ChevronRight,
  GitBranch,
  LoaderCircle,
  Plus,
  Search,
  Server,
  ShieldCheck,
} from 'lucide-react';
import { type ApiProvider, type BusinessRoute, errorMessage, providerApi } from './provider-api';
import { ProviderEditor } from './ProviderEditor';
import { RoutingSettings } from './RoutingSettings';
import { CallHistory } from './CallHistory';
import './provider-settings.css';
import { Notice } from './SettingsNotice';

export type SettingsTab = 'providers' | 'routing' | 'calls';
type Props = {
  tab: SettingsTab;
  onNavigate: (tab: SettingsTab) => void;
  onClose: () => void;
  onChanged: () => void;
};

export function ProviderSettings({ tab, onNavigate, onClose, onChanged }: Props) {
  const [providers, setProviders] = useState<ApiProvider[]>([]);
  const [routes, setRoutes] = useState<BusinessRoute[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const content = useRef<HTMLElement>(null);
  useEffect(() => {
    content.current?.scrollTo({ top: 0 });
  }, [tab]);
  const refresh = useCallback(async () => {
    const [providerData, routeData] = await Promise.all([providerApi.list(), providerApi.routes()]);
    setProviders(providerData.items);
    setRoutes(routeData.items);
    setSelected((current) =>
      current === 'new'
        ? current
        : providerData.items.some((item) => item.id === current)
          ? current
          : providerData.items[0]?.id || null,
    );
    onChanged();
  }, [onChanged]);
  useEffect(() => {
    let active = true;
    refresh()
      .catch((error) => active && setError(errorMessage(error)))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [refresh]);
  const selectedProvider = providers.find((item) => item.id === selected);
  const reload = () => {
    setError('');
    setLoading(true);
    refresh()
      .catch((error) => setError(errorMessage(error)))
      .finally(() => setLoading(false));
  };
  return (
    <section ref={content} className="provider-settings">
      <header className="settings-heading">
        <div>
          <div className="eyebrow">MODEL WORKSPACE</div>
          <h1>模型与 API</h1>
          <p>管理模型连接，为每个创作环节选择合适的模型。</p>
        </div>
        <button className="secondary-button" onClick={onClose}>
          <ArrowLeft size={15} />
          返回工作台
        </button>
      </header>
      <nav className="settings-tabs" aria-label="模型设置导航">
        <button
          className={tab === 'providers' ? 'selected' : ''}
          onClick={() => onNavigate('providers')}
        >
          <Server size={16} />
          Provider 管理<span>{providers.length}</span>
        </button>
        <button
          className={tab === 'routing' ? 'selected' : ''}
          onClick={() => onNavigate('routing')}
        >
          <GitBranch size={16} />
          业务路由
        </button>
        <button className={tab === 'calls' ? 'selected' : ''} onClick={() => onNavigate('calls')}>
          调用记录
        </button>
      </nav>
      {error && (
        <Notice error>
          {error}
          <button onClick={reload}>重新加载</button>
        </Notice>
      )}
      {loading ? (
        <div className="settings-empty">
          <LoaderCircle className="spin" />
          正在读取本地配置…
        </div>
      ) : tab === 'providers' ? (
        <div className="provider-workspace">
          <aside className="provider-list-panel">
            <div className="provider-list-heading">
              <h2>Providers</h2>
              <button
                className="icon-button"
                title="添加 Provider"
                onClick={() => setSelected('new')}
              >
                <Plus size={18} />
              </button>
            </div>
            <label className="provider-search">
              <Search size={14} />
              <input
                aria-label="搜索 Provider"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索名称或 API 地址"
              />
            </label>
            <div className="provider-list">
              {providers
                .filter((item) =>
                  (item.name + ' ' + item.base_url).toLowerCase().includes(query.toLowerCase()),
                )
                .map((item) => (
                  <button
                    key={item.id}
                    className={'provider-list-item ' + (selected === item.id ? 'selected' : '')}
                    onClick={() => setSelected(item.id)}
                  >
                    <span className="provider-logo">
                      <Server size={18} />
                    </span>
                    <span>
                      <strong>{item.name}</strong>
                      <small>Responses · {item.models.length} 个模型</small>
                      <em>
                        {!item.enabled
                          ? '已停用'
                          : item.auth_mode === 'bearer' && !item.credential_configured
                            ? '待配置凭据'
                            : '已启用'}
                        {routes.some((route) => route.resolved?.provider_id === item.id)
                          ? ' · 业务使用中'
                          : ''}
                      </em>
                    </span>
                    <ChevronRight size={14} />
                  </button>
                ))}
            </div>
            <button className="secondary-button add-provider" onClick={() => setSelected('new')}>
              <Plus size={15} />
              添加 Provider
            </button>
            <p className="panel-footnote">
              <ShieldCheck size={14} />
              密钥仅保存于本机系统凭据库
            </p>
          </aside>
          <div className="provider-detail">
            {selected === 'new' || selectedProvider ? (
              <ProviderEditor
                key={selected || 'new'}
                provider={selectedProvider}
                onSaved={async (id) => {
                  await refresh();
                  setSelected(id);
                }}
                onArchived={async () => {
                  setSelected(null);
                  await refresh();
                }}
                refresh={refresh}
              />
            ) : (
              <div className="settings-empty">
                <Server size={28} />
                <h2>添加第一个 Provider</h2>
                <p>连接兼容 Responses 的 API，然后添加模型。</p>
                <button className="primary-button" onClick={() => setSelected('new')}>
                  <Plus size={15} />
                  添加 Provider
                </button>
              </div>
            )}
          </div>
        </div>
      ) : tab === 'routing' ? (
        <RoutingSettings providers={providers} routes={routes} refresh={refresh} />
      ) : (
        <CallHistory />
      )}
    </section>
  );
}
