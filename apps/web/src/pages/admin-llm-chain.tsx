import { useMemo, useState } from 'react';
import {
  ArrowDown,
  ArrowUp,
  Cpu,
  Loader2,
  Save,
  ToggleLeft,
  ToggleRight,
} from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import {
  fetchLlmChain,
  moveStep,
  updateLlmChain,
  type LLMChainStep,
} from '@/lib/llm-chain';

// Runtime LLM chain admin page — Tier 3 Task 3.4.
//
// Backend:
//   GET  /api/v1/admin/llm-chain
//   PUT  /api/v1/admin/llm-chain
//
// Renders the chain as a reorderable list with per-row "enabled"
// toggles. Save commits the full chain (PUT semantics). On success
// we invalidate the cached read so the UI stays the source of truth.
//
// PII: provider names + model identifiers only — no API keys.

const CHAIN_QUERY_KEY = ['admin', 'llm-chain'] as const;

interface ChainRowProps {
  step: LLMChainStep;
  index: number;
  total: number;
  onToggle: () => void;
  onMove: (delta: -1 | 1) => void;
  onModelChange: (model: string) => void;
}

function ChainRow({
  step,
  index,
  total,
  onToggle,
  onMove,
  onModelChange,
}: ChainRowProps): JSX.Element {
  return (
    <li
      data-testid={`chain-row-${step.provider}`}
      className={`flex items-center gap-3 rounded-md border bg-background p-3 ${
        step.enabled ? '' : 'opacity-60'
      }`}
    >
      <Badge variant="outline" className="font-mono">
        #{index + 1}
      </Badge>
      <span className="min-w-[7rem] font-medium">{step.provider}</span>
      <Input
        data-testid={`chain-model-${step.provider}`}
        value={step.model}
        onChange={(e) => onModelChange(e.target.value)}
        className="h-8 max-w-[16rem] font-mono text-xs"
      />
      <div className="ml-auto flex items-center gap-1">
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label="Move up"
          data-testid={`chain-up-${step.provider}`}
          disabled={index === 0}
          onClick={() => onMove(-1)}
        >
          <ArrowUp className="h-4 w-4" />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label="Move down"
          data-testid={`chain-down-${step.provider}`}
          disabled={index === total - 1}
          onClick={() => onMove(1)}
        >
          <ArrowDown className="h-4 w-4" />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={step.enabled ? 'Disable step' : 'Enable step'}
          data-testid={`chain-toggle-${step.provider}`}
          onClick={onToggle}
        >
          {step.enabled ? (
            <ToggleRight className="h-4 w-4 text-primary" />
          ) : (
            <ToggleLeft className="h-4 w-4 text-muted-foreground" />
          )}
        </Button>
      </div>
    </li>
  );
}

export function AdminLlmChainPage(): JSX.Element {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: CHAIN_QUERY_KEY,
    queryFn: fetchLlmChain,
  });

  // Local draft the operator edits; saved on "Save".
  const [draft, setDraft] = useState<LLMChainStep[] | null>(null);

  // When the upstream data changes, reset the draft to mirror it.
  const stepsForDraft = useMemo<LLMChainStep[]>(() => {
    if (draft !== null) return draft;
    return query.data?.steps ?? [];
  }, [draft, query.data]);

  const dirty = useMemo(() => {
    if (draft === null) return false;
    const initial = query.data?.steps ?? [];
    return JSON.stringify(draft) !== JSON.stringify(initial);
  }, [draft, query.data]);

  const mutation = useMutation({
    mutationFn: updateLlmChain,
    onSuccess: (next) => {
      queryClient.setQueryData(CHAIN_QUERY_KEY, next);
      setDraft(null);
    },
  });

  const updateAt = (idx: number, mut: (s: LLMChainStep) => LLMChainStep): void => {
    const current = draft ?? query.data?.steps ?? [];
    const next = current.map((s, i) => (i === idx ? mut(s) : s));
    setDraft(next);
  };

  const move = (idx: number, delta: -1 | 1): void => {
    const current = draft ?? query.data?.steps ?? [];
    const target = idx + delta;
    if (target < 0 || target >= current.length) return;
    setDraft(moveStep(current, idx, target));
  };

  const toggle = (idx: number): void => {
    updateAt(idx, (s) => ({ ...s, enabled: !s.enabled }));
  };

  const setModel = (idx: number, model: string): void => {
    updateAt(idx, (s) => ({ ...s, model }));
  };

  const enabledCount = stepsForDraft.filter((s) => s.enabled).length;

  const save = (): void => {
    if (!dirty) return;
    mutation.mutate({ steps: stepsForDraft });
  };

  const reset = (): void => setDraft(null);

  return (
    <div className="space-y-6 p-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Cpu className="h-5 w-5" />
            LLM 运行时链路
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm text-muted-foreground">
          <p>
            配置全局 LLM Fallback 链路,按顺序逐个尝试,
            命中第一个可用步骤。
          </p>
          <p>
            调整顺序或临时禁用步骤不会立即影响已缓存的解析器 ——
            新建会话会按新链路执行。至少需要 1 个启用步骤。
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <span>当前链路</span>
            <Badge variant="secondary" data-testid="enabled-count">
              {enabledCount}/{stepsForDraft.length} 启用
            </Badge>
          </CardTitle>
        </CardHeader>
        <CardContent>
          {query.isLoading && (
            <div
              className="flex items-center gap-2 text-sm text-muted-foreground"
              data-testid="chain-loading"
            >
              <Loader2 className="h-4 w-4 animate-spin" />
              加载中…
            </div>
          )}

          {query.isError && (
            <p className="text-sm text-destructive" data-testid="chain-error">
              加载失败,请稍后重试。
            </p>
          )}

          {stepsForDraft.length === 0 && !query.isLoading && (
            <p
              className="text-sm text-muted-foreground"
              data-testid="chain-empty"
            >
              当前链路为空,使用下方表单新增一个步骤。
            </p>
          )}

          {stepsForDraft.length > 0 && (
            <ul className="space-y-2" data-testid="chain-list">
              {stepsForDraft.map((step, idx) => (
                <ChainRow
                  key={`${step.provider}-${idx}`}
                  step={step}
                  index={idx}
                  total={stepsForDraft.length}
                  onToggle={() => toggle(idx)}
                  onMove={(d) => move(idx, d)}
                  onModelChange={(m) => setModel(idx, m)}
                />
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>已注册 Provider</CardTitle>
        </CardHeader>
        <CardContent>
          {query.data?.providers.length === 0 ? (
            <p className="text-sm text-muted-foreground" data-testid="providers-empty">
              没有已注册的全局 Provider。
            </p>
          ) : (
            <ul className="space-y-1 text-sm" data-testid="providers-list">
              {query.data?.providers.map((p) => (
                <li key={p.name} className="flex items-center gap-2">
                  <span className="font-mono">{p.name}</span>
                  <span className="text-muted-foreground">/</span>
                  <span className="font-mono text-xs">{p.model}</span>
                  {p.is_default && <Badge variant="outline">default</Badge>}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <div className="flex items-center gap-2">
        <Button
          data-testid="chain-save"
          onClick={save}
          disabled={!dirty || mutation.isPending || enabledCount === 0}
        >
          {mutation.isPending ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          ) : (
            <Save className="mr-2 h-4 w-4" />
          )}
          保存
        </Button>
        <Button
          variant="outline"
          data-testid="chain-reset"
          onClick={reset}
          disabled={!dirty || mutation.isPending}
        >
          重置
        </Button>
        {mutation.isError && (
          <span
            className="text-sm text-destructive"
            data-testid="chain-save-error"
          >
            保存失败:{(mutation.error as Error).message ?? '未知错误'}
          </span>
        )}
      </div>
    </div>
  );
}