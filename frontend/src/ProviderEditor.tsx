import { useRef, useState, type FormEvent } from 'react';
import { Activity, Download, LoaderCircle, Plus, Save, ShieldCheck, Trash2 } from 'lucide-react';
import {
  type ApiProvider,
  type ModelCandidate,
  type ProviderFields,
  type ProviderModel,
  errorMessage,
  providerApi,
} from './provider-api';
import { ModelEditor } from './ModelEditor';
import { Notice } from './SettingsNotice';
import { SettingsDialog } from './SettingsDialog';

const newProvider: ProviderFields = {
  name: '',
  protocol: 'openai_responses',
  base_url: '',
  auth_mode: 'bearer',
  allow_private_http: false,
  enabled: true,
};
const fieldsOf = (provider: ApiProvider): ProviderFields => ({
  name: provider.name,
  protocol: provider.protocol,
  base_url: provider.base_url,
  auth_mode: provider.auth_mode,
  allow_private_http: provider.allow_private_http,
  enabled: provider.enabled,
});
type Props = {
  provider?: ApiProvider;
  onSaved: (id: string) => Promise<void>;
  onArchived: () => Promise<void>;
  refresh: () => Promise<void>;
};

export function ProviderEditor({ provider, onSaved, onArchived, refresh }: Props) {
  const [fields, setFields] = useState<ProviderFields>(provider ? fieldsOf(provider) : newProvider);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(null);
  const [editing, setEditing] = useState<ProviderModel | 'new' | null>(null);
  const [candidates, setCandidates] = useState<ModelCandidate[] | null>(null);
  const keyInput = useRef<HTMLInputElement>(null);
  const [confirmation, setConfirmation] = useState<'archive' | 'revoke' | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const dirty = !!provider && JSON.stringify(fields) !== JSON.stringify(fieldsOf(provider));
  const update = <K extends keyof ProviderFields>(key: K, value: ProviderFields[K]) =>
    setFields((current) => ({ ...current, [key]: value }));

  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy('save');
    setNotice(null);
    let saved: ApiProvider | null = null;
    try {
      saved = provider
        ? await providerApi.update(provider.id, fields, provider.revision)
        : await providerApi.create(fields, keyInput.current?.value.trim());
      if (provider && fields.auth_mode === 'bearer' && keyInput.current?.value.trim())
        await providerApi.credential(saved.id, keyInput.current.value.trim(), saved.revision);
      setFields(fieldsOf(saved));
      await onSaved(saved.id);
      setNotice({ text: '配置已保存。连接或模型变化后，请重新测试。', error: false });
    } catch (error) {
      if (saved) await refresh();
      setNotice({
        text: (saved ? '基本信息已保存；凭据操作未完成：' : '') + errorMessage(error),
        error: true,
      });
    } finally {
      if (keyInput.current) keyInput.current.value = '';
      setBusy('');
    }
  }

  async function confirmAction() {
    if (!provider || !confirmation) return;
    const action = confirmation;
    setBusy(action);
    setNotice(null);
    try {
      if (action === 'archive') {
        await providerApi.archive(provider.id, provider.revision);
        await onArchived();
      } else {
        await providerApi.revoke(provider.id, provider.revision);
        await refresh();
        setNotice({ text: '该 Provider 的所有凭据版本已撤销。', error: false });
      }
      setConfirmation(null);
    } catch (error) {
      setNotice({ text: errorMessage(error), error: true });
    } finally {
      setBusy('');
    }
  }

  async function discover() {
    if (!provider) return;
    setBusy('discover');
    setNotice(null);
    try {
      const result = await providerApi.discover(provider.id, provider.revision);
      setCandidates(result.items);
    } catch (error) {
      setNotice({ text: errorMessage(error) + '。仍可手动添加模型。', error: true });
    } finally {
      setBusy('');
    }
  }

  async function test(model: ProviderModel) {
    setTesting(model.id);
    setNotice(null);
    try {
      const result = await providerApi.test(model.id);
      await refresh();
      setNotice({
        text:
          result.status === 'passed'
            ? model.display_name + ' 的文本流与结构化输出检查均已通过。'
            : model.display_name + ' 存在未通过的检查，请查看模型状态。',
        error: result.status !== 'passed',
      });
    } catch (error) {
      setNotice({ text: errorMessage(error), error: true });
    } finally {
      setTesting(null);
    }
  }

  return (
    <>
      <form className="settings-card" onSubmit={save}>
        <header>
          <div>
            <h2>{provider?.name || '添加 Provider'}</h2>
            <p>
              {provider ? '编辑 Provider 连接与认证信息' : '创建一个兼容 OpenAI Responses 的连接'}
            </p>
          </div>
          <span className="settings-badge">Responses</span>
        </header>
        <fieldset disabled={!!busy || !!testing} className="settings-form">
          <div className="settings-fields">
            <label>
              显示名称
              <input
                required
                maxLength={160}
                value={fields.name}
                onChange={(event) => update('name', event.target.value)}
                placeholder="例如：团队推理服务"
              />
            </label>
            <label>
              API 协议
              <select aria-label="API 协议" value={fields.protocol} onChange={() => {}}>
                <option value="openai_responses">OpenAI Responses</option>
                <option disabled>Chat Completions · 待扩展</option>
                <option disabled>Messages · 待扩展</option>
                <option disabled>Gemini · 待扩展</option>
              </select>
            </label>
          </div>
          <label>
            API 根地址
            <input
              aria-label="API 根地址"
              required
              type="url"
              maxLength={500}
              value={fields.base_url}
              onChange={(event) => update('base_url', event.target.value)}
              placeholder="https://api.example.com/v1"
            />
            <small>填写 API 根地址，系统自动追加 /responses 或 /models。</small>
          </label>
          <div className="settings-fields">
            <label>
              认证方式
              <select
                aria-label="认证方式"
                value={fields.auth_mode}
                onChange={(event) => {
                  update('auth_mode', event.target.value as ProviderFields['auth_mode']);
                  if (keyInput.current) keyInput.current.value = '';
                }}
              >
                <option value="bearer">Bearer API Key</option>
                <option value="none">无认证</option>
              </select>
            </label>
          </div>
          {fields.auth_mode === 'bearer' && (
            <label>
              API Key{' '}
              <span className="credential-state">
                {provider?.credential_configured
                  ? '已配置 · 版本 ' + provider.credential_version
                  : '尚未配置，可稍后添加'}
              </span>
              <input
                aria-label="API Key"
                ref={keyInput}
                type="password"
                maxLength={500}
                autoComplete="new-password"
                placeholder={
                  provider?.credential_configured
                    ? '留空保留现有 Key；输入新 Key 进行替换'
                    : '粘贴 API Key（可留空）'
                }
              />
              <small>
                <ShieldCheck size={12} />
                仅写入系统凭据库；每次提交后清空输入。
              </small>
            </label>
          )}
          {provider?.credential_store_available === false && (
            <Notice error>系统凭据库不可用，请恢复本机凭据存储后重试。</Notice>
          )}
          <div className="settings-toggles">
            <label>
              <input
                type="checkbox"
                checked={fields.enabled}
                onChange={(event) => update('enabled', event.target.checked)}
              />
              启用 Provider
            </label>
            <label>
              <input
                type="checkbox"
                checked={fields.allow_private_http}
                onChange={(event) => update('allow_private_http', event.target.checked)}
              />
              允许可信局域网 IP 使用 HTTP
            </label>
          </div>
          {fields.auth_mode === 'none' && provider?.credential_configured && (
            <Notice error>
              保存无认证模式会撤销该 Provider 的全部凭据版本，已有作品将无法再使用这些 Key。
            </Notice>
          )}
          <footer>
            <small>{provider ? 'revision ' + provider.revision : '保存后即可管理模型目录'}</small>
            <button type="submit" className="primary-button">
              {busy === 'save' ? <LoaderCircle size={14} className="spin" /> : <Save size={14} />}
              保存 Provider
            </button>
          </footer>
        </fieldset>
      </form>
      {notice && <Notice error={notice.error}>{notice.text}</Notice>}
      {provider && (
        <>
          <section className="settings-card model-directory">
            <header>
              <div>
                <h2>
                  模型目录 <span className="count-label">{provider.models.length}</span>
                </h2>
                <p>上下文长度由接口提供或手动填写，未知值保持为空。</p>
              </div>
              <div className="button-row">
                <button
                  className="secondary-button"
                  disabled={!!busy || !!testing || dirty || !provider.enabled}
                  onClick={discover}
                >
                  {busy === 'discover' ? (
                    <LoaderCircle size={14} className="spin" />
                  ) : (
                    <Download size={14} />
                  )}
                  获取可用模型
                </button>
                <button
                  className="primary-button"
                  disabled={!!busy || dirty || !!testing}
                  onClick={() => setEditing('new')}
                >
                  <Plus size={14} />
                  手动添加
                </button>
              </div>
            </header>
            {dirty && <Notice>连接信息尚未保存。请先保存，再发现、编辑或测试模型。</Notice>}
            <div className="settings-table-wrap">
              <table className="settings-table">
                <thead>
                  <tr>
                    <th>模型</th>
                    <th>上下文 / 输出</th>
                    <th>模型并发</th>
                    <th>测试状态</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {provider.models.map((model) => (
                    <tr key={model.id}>
                      <td>
                        <strong>{model.display_name}</strong>
                        <small>{model.remote_model_id}</small>
                        {!model.enabled && <span className="settings-badge warning">已停用</span>}
                      </td>
                      <td>
                        {model.context_window_tokens?.toLocaleString() || '未提供'}
                        <small>输出 {model.max_output_tokens?.toLocaleString() || '未提供'}</small>
                      </td>
                      <td>模型并发 {model.model_concurrency}</td>
                      <td>
                        <ModelTestStatus model={model} />
                      </td>
                      <td>
                        <div className="button-row">
                          <button
                            className="text-button"
                            disabled={!!busy || !!testing || dirty}
                            onClick={() => setEditing(model)}
                          >
                            编辑
                          </button>
                          <button
                            className="secondary-button"
                            disabled={
                              !!busy ||
                              !!testing ||
                              dirty ||
                              !provider.enabled ||
                              !model.enabled ||
                              (provider.auth_mode === 'bearer' && !provider.credential_configured)
                            }
                            onClick={() => void test(model)}
                          >
                            {testing === model.id ? (
                              <LoaderCircle size={13} className="spin" />
                            ) : (
                              <Activity size={13} />
                            )}
                            测试
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!provider.models.length && (
              <div className="settings-empty">还没有模型。获取目录或手动添加 Model ID。</div>
            )}
            <p className="panel-footnote">
              测试会发起两次真实 API
              请求，可能产生费用。新模型默认声明多模态与工具能力，尚未验证；当前创作流程仍使用文本输入。
            </p>
          </section>
          <section className="settings-danger">
            <div>
              <strong>凭据与归档</strong>
              <p>被业务路由使用的 Provider 不能归档；归档保留历史调用和作品记录。</p>
            </div>
            <div className="button-row">
              <button
                className="secondary-button danger"
                disabled={!!busy || !!testing}
                onClick={() => setConfirmation('revoke')}
              >
                撤销全部凭据
              </button>
              <button
                className="secondary-button danger"
                disabled={!!busy || !!testing}
                onClick={() => setConfirmation('archive')}
              >
                <Trash2 size={14} />
                归档 Provider
              </button>
            </div>
          </section>
        </>
      )}
      {editing && provider && (
        <ModelEditor
          providerId={provider.id}
          model={editing === 'new' ? undefined : editing}
          onClose={() => setEditing(null)}
          onSaved={refresh}
        />
      )}
      {candidates && provider && (
        <DiscoveryDialog
          candidates={candidates}
          provider={provider}
          onClose={() => setCandidates(null)}
          onImported={refresh}
        />
      )}
      {confirmation && (
        <SettingsDialog
          label="确认操作"
          className="compact-modal"
          busy={!!busy}
          onClose={() => setConfirmation(null)}
        >
          <h2>{confirmation === 'revoke' ? '撤销全部凭据？' : '归档 Provider？'}</h2>
          <p>
            {confirmation === 'revoke'
              ? '当前与历史凭据均会失效，依赖这些凭据的已有作品将不能继续调用。此操作不能恢复。'
              : '归档后不可再选择此连接，已有作品与调用记录仍然保留。'}
          </p>
          <div className="button-row">
            <button
              className="secondary-button"
              disabled={!!busy}
              onClick={() => setConfirmation(null)}
            >
              取消
            </button>
            <button className="primary-button danger" disabled={!!busy} onClick={confirmAction}>
              确认{confirmation === 'revoke' ? '撤销' : '归档'}
            </button>
          </div>
        </SettingsDialog>
      )}
    </>
  );
}

function ModelTestStatus({ model }: { model: ProviderModel }) {
  if (!model.last_test) return <span className="settings-badge">未测试</span>;
  if (model.last_test.status === 'running')
    return <span className="settings-badge">测试进行中</span>;
  if (model.last_test.status === 'cancelled')
    return <span className="settings-badge warning">测试已取消 · 请重试</span>;
  if (model.last_test.stale)
    return <span className="settings-badge warning">配置已变更 · 请重测</span>;
  return (
    <div className="model-checks">
      {model.last_test.checks.map((check) => (
        <span
          key={check.name}
          title={check.message}
          className={'settings-badge ' + (check.status === 'passed' ? 'success' : 'warning')}
        >
          {check.name === 'streaming_text' ? '文本流' : '结构化'} ·{' '}
          {check.status === 'passed' ? '通过' : '失败'}
        </span>
      ))}
    </div>
  );
}

function DiscoveryDialog({
  candidates,
  provider,
  onClose,
  onImported,
}: {
  candidates: ModelCandidate[];
  provider: ApiProvider;
  onClose: () => void;
  onImported: () => Promise<void>;
}) {
  const [items, setItems] = useState(candidates);
  const [selected, setSelected] = useState<Set<string>>(
    new Set(candidates.filter((item) => !item.already_added).map((item) => item.remote_model_id)),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [filter, setFilter] = useState('');
  async function importModels() {
    setBusy(true);
    setError('');
    const completed = new Set<string>();
    try {
      for (const item of items.filter(
        (item) => selected.has(item.remote_model_id) && !item.already_added,
      )) {
        await providerApi.addModel(
          provider.id,
          {
            remote_model_id: item.remote_model_id,
            display_name: item.display_name || item.remote_model_id,
            context_window_tokens: item.context_window_tokens,
            max_output_tokens: item.max_output_tokens,
            model_concurrency: item.model_concurrency,
          },
          item.metadata_source,
        );
        completed.add(item.remote_model_id);
      }
      await onImported();
      onClose();
    } catch (error) {
      setItems((current) =>
        current.map((item) =>
          completed.has(item.remote_model_id) ? { ...item, already_added: true } : item,
        ),
      );
      setSelected((current) => new Set([...current].filter((id) => !completed.has(id))));
      await onImported();
      setError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <SettingsDialog label="导入可用模型" busy={busy} onClose={onClose}>
      <header>
        <div>
          <h2>发现 {items.length} 个模型</h2>
          <p>选择要导入的模型。接口未提供的上下文长度可稍后手动填写。</p>
        </div>
        <button className="secondary-button" disabled={busy} onClick={onClose}>
          关闭
        </button>
      </header>
      <label className="provider-search">
        <input
          aria-label="搜索发现模型"
          placeholder="搜索 Model ID"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
        />
      </label>
      {error && <Notice error>{error}</Notice>}
      <div className="discovery-list">
        {items
          .filter((item) => item.remote_model_id.toLowerCase().includes(filter.toLowerCase()))
          .map((item) => (
            <label key={item.remote_model_id}>
              <input
                type="checkbox"
                disabled={busy || item.already_added}
                checked={selected.has(item.remote_model_id) && !item.already_added}
                onChange={(event) =>
                  setSelected((current) => {
                    const next = new Set(current);
                    if (event.target.checked) next.add(item.remote_model_id);
                    else next.delete(item.remote_model_id);
                    return next;
                  })
                }
              />
              <span>
                <strong>{item.display_name}</strong>
                <small>{item.remote_model_id}</small>
              </span>
              <small>
                {item.already_added
                  ? '已添加'
                  : item.context_window_tokens
                    ? item.context_window_tokens.toLocaleString() + ' tokens'
                    : '接口未提供上下文'}
              </small>
            </label>
          ))}
      </div>
      <footer>
        <span>已选择 {selected.size} 个</span>
        <button className="primary-button" disabled={busy || !selected.size} onClick={importModels}>
          {busy ? <LoaderCircle size={14} className="spin" /> : <Download size={14} />}导入所选模型
        </button>
      </footer>
    </SettingsDialog>
  );
}
