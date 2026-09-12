import { useEffect, useState } from 'react';
import { AxiosError } from 'axios';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  createKnowledgeBase,
  EMBEDDING_MODEL_OPTIONS,
  type EmbeddingModelOption,
  type KnowledgeBase,
} from '@/lib/knowledge';

interface ApiErrorPayload {
  detail?: string | Array<{ msg?: string }>;
}

function extractErrorMessage(error: unknown): string {
  if (error instanceof AxiosError) {
    const payload = error.response?.data as ApiErrorPayload | undefined;
    const detail = payload?.detail;
    if (typeof detail === 'string' && detail.trim().length > 0) return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0];
      if (first && typeof first === 'object' && typeof first.msg === 'string') {
        return first.msg;
      }
    }
    return error.message || '创建失败';
  }
  if (error instanceof Error && error.message) return error.message;
  return '创建失败';
}

export interface KbCreateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated?: (kb: KnowledgeBase) => void;
}

/**
 * Modal form for creating a new knowledge base. The slug is derived
 * client-side from the KB name (see ``slugify`` in `lib/knowledge.ts`)
 * so users don't have to think about URL-safe identifiers.
 */
export function KbCreateDialog({
  open,
  onOpenChange,
  onCreated,
}: KbCreateDialogProps): JSX.Element {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [embeddingModel, setEmbeddingModel] =
    useState<EmbeddingModelOption>('text-embedding-3-small');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setName('');
      setDescription('');
      setEmbeddingModel('text-embedding-3-small');
      setSubmitting(false);
      setError(null);
    }
  }, [open]);

  const trimmedName = name.trim();
  const nameError = name.length > 0 && trimmedName.length === 0 ? '名称不能为空' : null;
  const canSubmit = !submitting && trimmedName.length > 0;

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const kb = await createKnowledgeBase({
        name: trimmedName,
        description: description.trim().length > 0 ? description.trim() : null,
        embedding_model: embeddingModel,
      });
      onCreated?.(kb);
      onOpenChange(false);
    } catch (err) {
      setError(extractErrorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="kb-create-dialog">
        <form onSubmit={handleSubmit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>新建知识库</DialogTitle>
            <DialogDescription>
              知识库用于组织文章与向量,slug 会根据名称自动生成。
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-2">
            <Label htmlFor="kb-create-name">名称</Label>
            <Input
              id="kb-create-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="例如:产品 FAQ"
              data-testid="kb-create-name"
              required
            />
            {nameError !== null ? (
              <p className="text-xs text-destructive" role="alert">
                {nameError}
              </p>
            ) : null}
          </div>

          <div className="space-y-2">
            <Label htmlFor="kb-create-description">描述 (可选)</Label>
            <Input
              id="kb-create-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="一句话说明此知识库的用途"
              data-testid="kb-create-description"
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="kb-create-model">Embedding 模型</Label>
            <select
              id="kb-create-model"
              value={embeddingModel}
              onChange={(e) =>
                setEmbeddingModel(e.target.value as EmbeddingModelOption)
              }
              data-testid="kb-create-model"
              className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            >
              {EMBEDDING_MODEL_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          </div>

          {error !== null ? (
            <p className="text-sm text-destructive" role="alert" data-testid="kb-create-error">
              {error}
            </p>
          ) : null}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={submitting}
              data-testid="kb-create-cancel"
            >
              取消
            </Button>
            <Button
              type="submit"
              disabled={!canSubmit}
              data-testid="kb-create-submit"
            >
              {submitting ? '创建中…' : '创建'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
