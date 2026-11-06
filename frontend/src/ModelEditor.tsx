import { useState, type FormEvent } from 'react';
import { LoaderCircle, Plus, Save, Trash2, X } from 'lucide-react';
import {
  type ModelFields,
  type ProviderModel,
  type ReasoningTier,
  errorMessage,
  providerApi,
} from './provider-api';
import { Notice } from './SettingsNotice';
import { SettingsDialog } from './SettingsDialog';

type Props = {
  providerId: string;
  model?: ProviderModel;
  onClose: () => void;
  onSaved: () => Promise<void>;
};
const modelFields = (model?: ProviderModel): ModelFields => ({
  remote_model_id: model?.remote_model_id || '',
  display_name: model?.display_name || '',
  context_window_tokens: model?.context_window_tokens ?? null,
  max_output_tokens: model?.max_output_tokens ?? null,
  max_concurrency: model?.max_concurrency ?? 3,
});

export function ModelEditor({ providerId, model: initialModel, onClose, onSaved }: Props) {
  const [model, setModel] = useState(initialModel);
  const [fields, setFields] = useState<ModelFields>(modelFields(initialModel));
  const [enabled, setEnabled] = useState(initialModel?.enabled ?? true);
  const [tiers, setTiers] = useState<ReasoningTier[]>(initialModel?.reasoning_tiers || []);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(null);
  const [confirmArchive, setConfirmArchive] = useState(false);
  const update = <K extends keyof ModelFields>(key: K, value: ModelFields[K]) =>
    setFields((current) => ({ ...current, [key]: value }));
  const updateTier = (index: number, patch: Partial<ReasoningTier>) =>
    setTiers((current) =>
      current.map((item, position) => (position === index ? { ...item, ...patch } : item)),
    );
  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy('model');
    setNotice(null);
    try {
      const payload = {
        ...fields,
        display_name: fields.display_name.trim() || fields.remote_model_id.trim(),
      };
      const saved = model
        ? await providerApi.updateModel(model.id, payload, enabled, model.revision)
        : await providerApi.addModel(providerId, payload);
      setModel(saved);
      setFields(modelFields(saved));
      if (!model) setTiers(saved.reasoning_tiers);
      await onSaved();
      setNotice({ text: '模型信息已保存。', error: false });
    } catch (error) {
      setNotice({ text: errorMessage(error), error: true });
    } finally {
      setBusy('');
    }
  }
  async function saveTiers(event: FormEvent) {
    event.preventDefault();
    if (!model) return;
    setBusy('tiers');
    setNotice(null);
    try {
      const result = await providerApi.tiers(model.id, tiers, model.revision);
      setModel((current) =>
        current
          ? { ...current, revision: result.revision, reasoning_tiers: result.tiers }
          : current,
      );
      setTiers(result.tiers);
      await onSaved();
      setNotice({ text: '思考档位已保存。', error: false });
    } catch (error) {
      setNotice({ text: errorMessage(error), error: true });
    } finally {
      setBusy('');
    }
  }
  async function archive() {
    if (!model) return;
    setBusy('archive');
    setNotice(null);
    try {
      await providerApi.archiveModel(model.id, model.revision);
      await onSaved();
      onClose();
    } catch (error) {
      setNotice({ text: errorMessage(error), error: true });
    } finally {
      setBusy('');
    }
  }
  return (
    <SettingsDialog label="模型设置" className="model-editor" busy={!!busy} onClose={onClose}>
      <header>
        <div>
          <h2>{model ? '模型设置' : '添加模型'}</h2>
          <p>编辑远程标识、上下文元数据和模型并发。</p>
        </div>
        <button className="icon-button" title="关闭模型设置" disabled={!!busy} onClick={onClose}>
          <X size={18} />
        </button>
      </header>
      {notice && <Notice error={notice.error}>{notice.text}</Notice>}
      <form onSubmit={save}>
        <fieldset disabled={!!busy} className="settings-form">
          <div className="settings-fields">
            <label>
              远程 Model ID
              <input
                required
                maxLength={200}
                value={fields.remote_model_id}
                onChange={(event) => update('remote_model_id', event.target.value)}
                placeholder="接口实际接收的模型 ID"
              />
            </label>
            <label>
              显示名称
              <input
                maxLength={200}
                value={fields.display_name}
                onChange={(event) => update('display_name', event.target.value)}
                placeholder="留空使用 Model ID"
              />
            </label>
          </div>
          <div className="settings-fields three-fields">
            <label>
              上下文窗口（Token）
              <input
                type="number"
                min={1}
                step={1}
                value={fields.context_window_tokens ?? ''}
                onChange={(event) =>
                  update(
                    'context_window_tokens',
                    event.target.value ? Number(event.target.value) : null,
                  )
                }
                placeholder="未知"
              />
            </label>
            <label>
              最大输出（Token）
              <input
                type="number"
                min={1}
                step={1}
                value={fields.max_output_tokens ?? ''}
                onChange={(event) =>
                  update(
                    'max_output_tokens',
                    event.target.value ? Number(event.target.value) : null,
                  )
                }
                placeholder="未知"
              />
            </label>
            <label>
              模型并发限制
              <input
                type="number"
                required
                min={1}
                max={32}
                value={fields.max_concurrency}
                onChange={(event) => update('max_concurrency', Number(event.target.value))}
              />
            </label>
          </div>
          <small className="panel-footnote">
            上下文和输出长度仅作目录元数据；正式生成不发送 Token
            上限。能力默认声明为多模态、支持工具，尚未验证。
          </small>
          {model && (
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(event) => setEnabled(event.target.checked)}
              />
              启用模型
            </label>
          )}
          <footer>
            <small>
              {model
                ? '元数据来源：' + model.metadata_source + ' · revision ' + model.revision
                : '创建后默认提供 Off / Low / Medium / High'}
            </small>
            <button className="primary-button" type="submit">
              {busy === 'model' ? <LoaderCircle className="spin" size={14} /> : <Save size={14} />}
              保存模型信息
            </button>
          </footer>
        </fieldset>
      </form>
      {model && (
        <form onSubmit={saveTiers} className="reasoning-form">
          <fieldset disabled={!!busy} className="settings-form">
            <div className="subsection-heading">
              <div>
                <h3>思考档位</h3>
                <p>Off 始终保留且不发送 reasoning，其他档位映射到 API effort 值。</p>
              </div>
              <button
                type="button"
                className="secondary-button"
                disabled={tiers.length >= 20}
                onClick={() =>
                  setTiers((current) => [
                    ...current,
                    {
                      code: '',
                      label: '',
                      api_value: '',
                      enabled: true,
                      sort_order: current.length,
                    },
                  ])
                }
              >
                <Plus size={14} />
                添加档位
              </button>
            </div>
            <div className="tier-table">
              <div className="tier-row tier-head">
                <span>Code</span>
                <span>显示名称</span>
                <span>API 值</span>
                <span>启用</span>
                <span />
              </div>
              {tiers.map((tier, index) => (
                <div className="tier-row" key={index}>
                  <input
                    aria-label={'档位代码 ' + (index + 1)}
                    required
                    minLength={2}
                    maxLength={40}
                    pattern="[a-z][a-z0-9_-]{1,39}"
                    readOnly={tier.code === 'off'}
                    value={tier.code}
                    onChange={(event) => updateTier(index, { code: event.target.value })}
                  />
                  <input
                    aria-label={'档位名称 ' + (index + 1)}
                    required
                    maxLength={80}
                    value={tier.label}
                    onChange={(event) => updateTier(index, { label: event.target.value })}
                  />
                  <input
                    aria-label={'API 参数值 ' + (index + 1)}
                    required={tier.code !== 'off'}
                    disabled={tier.code === 'off'}
                    maxLength={80}
                    value={tier.api_value || ''}
                    placeholder={tier.code === 'off' ? '不发送' : '例如 high'}
                    onChange={(event) => updateTier(index, { api_value: event.target.value })}
                  />
                  <input
                    aria-label={'启用档位 ' + tier.label}
                    type="checkbox"
                    disabled={tier.code === 'off'}
                    checked={tier.enabled}
                    onChange={(event) => updateTier(index, { enabled: event.target.checked })}
                  />
                  <button
                    className="icon-button danger"
                    title="删除档位"
                    type="button"
                    disabled={tier.code === 'off'}
                    onClick={() =>
                      setTiers((current) => current.filter((_, position) => position !== index))
                    }
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              ))}
            </div>
            <footer>
              <small>档位与模型信息分别保存</small>
              <button className="primary-button" type="submit">
                {busy === 'tiers' ? (
                  <LoaderCircle className="spin" size={14} />
                ) : (
                  <Save size={14} />
                )}
                保存思考档位
              </button>
            </footer>
          </fieldset>
        </form>
      )}
      {model && (
        <div className="settings-danger">
          {confirmArchive ? (
            <>
              <span>确认归档此模型？被业务路由引用的模型不能归档。</span>
              <div className="button-row">
                <button
                  className="secondary-button"
                  disabled={!!busy}
                  onClick={() => setConfirmArchive(false)}
                >
                  取消
                </button>
                <button className="secondary-button danger" disabled={!!busy} onClick={archive}>
                  确认归档
                </button>
              </div>
            </>
          ) : (
            <button
              className="text-button danger"
              disabled={!!busy}
              onClick={() => setConfirmArchive(true)}
            >
              归档模型
            </button>
          )}
        </div>
      )}
    </SettingsDialog>
  );
}
