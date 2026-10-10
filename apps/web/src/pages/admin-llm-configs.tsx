import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useCurrentUser } from '@/lib/use-current-user';
import {
  createLLMConfig,
  deleteLLMConfig,
  fetchLLMConfigs,
  updateLLMConfig,
  type LLMConfig,
  type LLMProvider,
} from '@/lib/llm-configs';

const LLM_CONFIGS_QUERY_KEY = ['admin', 'llm-configs'] as const;

const PROVIDER_OPTIONS: { value: LLMProvider; label: string }[] = [
  { value: 'minimax', label: 'MiniMax' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'openai', label: 'OpenAI' },
];

export function AdminLLMConfigsPage(): JSX.Element {
  const { user } = useCurrentUser();
  const tenantId = user?.tenant_id ?? '';
  const qc = useQueryClient();

  const [createOpen, setCreateOpen] = useState(false);
  const [provider, setProvider] = useState<LLMProvider>('minimax');
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('');

  const configsQuery = useQuery({
    queryKey: [...LLM_CONFIGS_QUERY_KEY, tenantId],
    queryFn: () => fetchLLMConfigs(tenantId),
    enabled: Boolean(tenantId),
  });

  const createMut = useMutation({
    mutationFn: () =>
      createLLMConfig(tenantId, {
        provider_name: provider,
        api_key: apiKey.trim(),
        base_url: baseUrl.trim() === '' ? null : baseUrl.trim(),
        enabled: true,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [...LLM_CONFIGS_QUERY_KEY, tenantId] });
      setCreateOpen(false);
      setApiKey('');
      setBaseUrl('');
    },
  });

  const toggleMut = useMutation({
    mutationFn: (input: { providerName: string; enabled: boolean }) =>
      updateLLMConfig(tenantId, input.providerName, { enabled: input.enabled }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [...LLM_CONFIGS_QUERY_KEY, tenantId] });
    },
  });

  const deleteMut = useMutation({
    mutationFn: (providerName: string) => deleteLLMConfig(tenantId, providerName),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [...LLM_CONFIGS_QUERY_KEY, tenantId] });
    },
  });

  const configs: LLMConfig[] = configsQuery.data ?? [];

  function handleSubmitCreate(): void {
    if (apiKey.trim().length === 0) return;
    createMut.mutate();
  }

  function handleDelete(providerName: string): void {
    if (!window.confirm(`删除 ${providerName} 的 BYOK 配置?该配置将被移除,后续可通过新建重新启用。`)) {
      return;
    }
    deleteMut.mutate(providerName);
  }

  return (
    <div
      className="flex h-full flex-col gap-6 p-6"
      data-testid="admin-llm-configs-page"
    >
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">LLM 配置</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            租户的 BYOK API key。密钥在落库前以 Fernet 加密;响应不包含明文或密文。
          </p>
        </div>
        <Button
          type="button"
          size="sm"
          data-testid="llm-config-create-open"
          onClick={() => setCreateOpen((v) => !v)}
        >
          {createOpen ? '取消' : '新建配置'}
        </Button>
      </header>

      {createOpen ? (
        <Card data-testid="llm-config-create-card">
          <CardHeader>
            <CardTitle className="text-base">新建 BYOK 配置</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <label className="flex flex-col gap-1 text-sm">
                <span className="text-muted-foreground">Provider</span>
                <select
                  className="rounded border px-2 py-1.5"
                  data-testid="llm-config-create-provider"
                  value={provider}
                  onChange={(e) => setProvider(e.target.value as LLMProvider)}
                >
                  {PROVIDER_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1 text-sm sm:col-span-2">
                <span className="text-muted-foreground">API key</span>
                <input
                  className="rounded border px-2 py-1.5 font-mono text-xs"
                  data-testid="llm-config-create-api-key"
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="sk-…"
                />
              </label>
              <label className="flex flex-col gap-1 text-sm sm:col-span-3">
                <span className="text-muted-foreground">
                  base_url (可选,留空使用 provider 默认)
                </span>
                <input
                  className="rounded border px-2 py-1.5 font-mono text-xs"
                  data-testid="llm-config-create-base-url"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="https://api.example.com/v1"
                />
              </label>
            </div>
            <div className="mt-3 flex justify-end gap-2">
              <Button
                type="button"
                size="sm"
                data-testid="llm-config-create-submit"
                onClick={handleSubmitCreate}
                disabled={createMut.isPending || apiKey.trim().length === 0}
              >
                {createMut.isPending ? '创建中…' : '创建'}
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {configsQuery.isLoading ? (
        <p className="text-sm text-muted-foreground" data-testid="llm-configs-loading">
          加载中…
        </p>
      ) : configsQuery.isError ? (
        <p
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          data-testid="llm-configs-error"
        >
          加载失败:{configsQuery.error instanceof Error
            ? configsQuery.error.message
            : '未知错误'}
        </p>
      ) : configs.length === 0 ? (
        <p
          className="text-sm text-muted-foreground"
          data-testid="llm-configs-empty"
        >
          尚未配置 BYOK provider — 点 “新建配置” 添加。
        </p>
      ) : (
        <Card>
          <CardContent className="p-0">
            <table className="w-full text-sm" data-testid="llm-configs-table">
              <thead className="border-b text-left">
                <tr>
                  <th className="px-4 py-2 font-medium">Provider</th>
                  <th className="px-4 py-2 font-medium">base_url</th>
                  <th className="px-4 py-2 font-medium">状态</th>
                  <th className="px-4 py-2 font-medium">最近更新</th>
                  <th className="px-4 py-2 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {configs.map((c) => (
                  <tr
                    key={c.provider_name}
                    data-testid={`llm-config-row-${c.provider_name}`}
                    className="border-b last:border-0"
                  >
                    <td className="px-4 py-2 font-medium">{c.provider_name}</td>
                    <td className="px-4 py-2 font-mono text-xs text-muted-foreground">
                      {c.base_url ?? '(default)'}
                    </td>
                    <td className="px-4 py-2">
                      <span
                        data-testid={`llm-config-status-${c.provider_name}`}
                        className={
                          c.enabled
                            ? 'rounded bg-emerald-100 px-2 py-0.5 text-xs text-emerald-700'
                            : 'rounded bg-muted px-2 py-0.5 text-xs text-muted-foreground'
                        }
                      >
                        {c.enabled ? '启用' : '禁用'}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-xs text-muted-foreground">
                      {c.updated_at}
                    </td>
                    <td className="px-4 py-2">
                      <div className="flex gap-2">
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          data-testid={`llm-config-toggle-${c.provider_name}`}
                          onClick={() =>
                            toggleMut.mutate({
                              providerName: c.provider_name,
                              enabled: !c.enabled,
                            })
                          }
                          disabled={toggleMut.isPending}
                        >
                          {c.enabled ? '禁用' : '启用'}
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          data-testid={`llm-config-delete-${c.provider_name}`}
                          onClick={() => handleDelete(c.provider_name)}
                          disabled={deleteMut.isPending}
                        >
                          删除
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
