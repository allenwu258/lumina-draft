import { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { type CallDetail, type ModelCall, errorMessage, providerApi } from './provider-api';
import { Notice } from './SettingsNotice';
import { SettingsDialog } from './SettingsDialog';

export function CallHistory() {
  const [calls, setCalls] = useState<ModelCall[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [detail, setDetail] = useState<CallDetail | null>(null);
  const load = useCallback(async (cursor?: string) => {
    setLoading(true);
    setError('');
    try {
      const result = await providerApi.calls(cursor);
      setCalls((current) => (cursor ? [...current, ...result.items] : result.items));
      setCursor(result.next_cursor);
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  return (
    <div className="settings-card calls-card">
      <header>
        <div>
          <h2>最近模型调用</h2>
          <p>查看业务模型、执行状态、排队时间与用量。测试调用也会记录。</p>
        </div>
        <button className="secondary-button" disabled={loading} onClick={() => void load()}>
          <RefreshCw size={14} />
          刷新
        </button>
      </header>
      {error && <Notice error>{error}</Notice>}
      <div className="settings-table-wrap">
        <table className="settings-table">
          <thead>
            <tr>
              <th>时间 / 业务</th>
              <th>模型</th>
              <th>状态</th>
              <th>耗时</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {calls.map((call) => (
              <tr key={call.id}>
                <td>
                  {new Date(call.started_at).toLocaleString()}
                  <small>
                    {call.business_code ||
                      (call.purpose === 'provider_test' ? '模型测试' : call.purpose)}
                  </small>
                </td>
                <td>{call.model}</td>
                <td>
                  <span
                    className={
                      'settings-badge ' +
                      (call.status === 'SUCCEEDED'
                        ? 'success'
                        : call.status === 'RUNNING' || call.status === 'QUEUED'
                          ? ''
                          : 'warning')
                    }
                  >
                    {call.status === 'QUEUED'
                      ? call.queue_reason === 'MODEL_CAPACITY'
                        ? '等待模型调用名额'
                        : '等待任务调度'
                      : call.status}
                  </span>
                  {call.error && <small>{call.error}</small>}
                </td>
                <td>
                  {call.total_duration_ms === null ? '—' : call.total_duration_ms + ' ms'}
                  <small>排队 {call.queue_duration_ms ?? '—'} ms</small>
                </td>
                <td>
                  <button
                    className="text-button"
                    onClick={() =>
                      providerApi
                        .call(call.id)
                        .then(setDetail)
                        .catch((error) => setError(errorMessage(error)))
                    }
                  >
                    详情
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!calls.length && !loading && <div className="settings-empty">暂无调用记录</div>}
      {cursor && (
        <button className="secondary-button" disabled={loading} onClick={() => void load(cursor)}>
          加载更多
        </button>
      )}
      {loading && <p role="status">正在读取…</p>}
      {detail && (
        <SettingsDialog label="模型调用详情" onClose={() => setDetail(null)}>
          <header>
            <h2>调用详情</h2>
            <button className="secondary-button" onClick={() => setDetail(null)}>
              关闭
            </button>
          </header>
          <dl className="call-detail">
            <dt>模型</dt>
            <dd>{detail.model}</dd>
            <dt>状态</dt>
            <dd>
              {detail.status} {detail.error_code}
            </dd>
            <dt>输入 / 输出 / 总 Token</dt>
            <dd>
              {detail.usage.input_tokens ?? '—'} / {detail.usage.output_tokens ?? '—'} /{' '}
              {detail.usage.total_tokens ?? '—'}
            </dd>
            <dt>错误信息</dt>
            <dd>{detail.error || '—'}</dd>
            <dt>冻结配置</dt>
            <dd>
              <pre>{JSON.stringify(detail.config_snapshot, null, 2)}</pre>
            </dd>
          </dl>
        </SettingsDialog>
      )}
    </div>
  );
}
