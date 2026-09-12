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
import { Textarea } from '@/components/ui/textarea';
import {
  createArticle,
  type ArticleSourceType,
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

export interface ArticleCreateDialogProps {
  kbId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the new article after a successful create. */
  onCreated?: (articleId: string) => void;
}

const SOURCE_OPTIONS: ReadonlyArray<{ value: ArticleSourceType; label: string }> = [
  { value: 'manual', label: '文本 (直接输入)' },
  { value: 'upload', label: '文件 (上传)' },
  { value: 'url', label: 'URL (抓取)' },
];

/**
 * Modal form for creating a new article by typing/pasting raw text.
 * File uploads are handled by the separate ``ArticleUploadDialog`` —
 * this dialog only covers the manual-text path.
 *
 * Validation: ``title`` is required and must be non-empty after trim;
 * ``raw_text`` is required for ``source_type === 'manual'``. Backend
 * additionally caps raw_text at 10 MiB and title at 500 chars, but we
 * leave that to the server and surface its 422 verbatim.
 */
export function ArticleCreateDialog({
  kbId,
  open,
  onOpenChange,
  onCreated,
}: ArticleCreateDialogProps): JSX.Element {
  const [title, setTitle] = useState('');
  const [sourceType, setSourceType] = useState<ArticleSourceType>('manual');
  const [rawText, setRawText] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset state when the dialog closes so a fresh open starts clean.
  useEffect(() => {
    if (!open) {
      setTitle('');
      setSourceType('manual');
      setRawText('');
      setSubmitting(false);
      setError(null);
    }
  }, [open]);

  const trimmedTitle = title.trim();
  const trimmedText = rawText.trim();
  const titleError = title.length > 0 && trimmedTitle.length === 0
    ? '标题不能为空'
    : null;
  const textError =
    sourceType === 'manual' && rawText.length > 0 && trimmedText.length === 0
      ? '正文不能为空'
      : null;
  const canSubmit =
    !submitting &&
    trimmedTitle.length > 0 &&
    (sourceType !== 'manual' || trimmedText.length > 0);

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const article = await createArticle(kbId, {
        title: trimmedTitle,
        source_type: sourceType,
        raw_text: trimmedText,
      });
      onCreated?.(article.id);
      onOpenChange(false);
    } catch (err) {
      setError(extractErrorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="article-create-dialog">
        <form onSubmit={handleSubmit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>新建文章</DialogTitle>
            <DialogDescription>
              手动输入标题和正文,后端会自动调度索引任务。
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-2">
            <Label htmlFor="article-create-title">标题</Label>
            <Input
              id="article-create-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="例如:退换货流程"
              data-testid="article-create-title"
              required
            />
            {titleError !== null ? (
              <p className="text-xs text-destructive" role="alert">
                {titleError}
              </p>
            ) : null}
          </div>

          <div className="space-y-2">
            <Label htmlFor="article-create-source">来源类型</Label>
            <select
              id="article-create-source"
              value={sourceType}
              onChange={(e) => setSourceType(e.target.value as ArticleSourceType)}
              data-testid="article-create-source"
              className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            >
              {SOURCE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>

          <div className="space-y-2">
            <Label htmlFor="article-create-text">正文</Label>
            <Textarea
              id="article-create-text"
              value={rawText}
              onChange={(e) => setRawText(e.target.value)}
              placeholder="粘贴或输入文章正文,最多 10 MiB。"
              rows={8}
              data-testid="article-create-text"
              required
            />
            {textError !== null ? (
              <p className="text-xs text-destructive" role="alert">
                {textError}
              </p>
            ) : null}
          </div>

          {error !== null ? (
            <p className="text-sm text-destructive" role="alert" data-testid="article-create-error">
              {error}
            </p>
          ) : null}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={submitting}
              data-testid="article-create-cancel"
            >
              取消
            </Button>
            <Button
              type="submit"
              disabled={!canSubmit}
              data-testid="article-create-submit"
            >
              {submitting ? '创建中…' : '创建'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
