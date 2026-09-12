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
  MAX_UPLOAD_BYTES,
  uploadArticle,
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
    return error.message || '上传失败';
  }
  if (error instanceof Error && error.message) return error.message;
  return '上传失败';
}

function formatMaxSize(bytes: number): string {
  const mib = bytes / (1024 * 1024);
  return `${mib} MiB`;
}

export interface ArticleUploadDialogProps {
  kbId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the new article after a successful upload. */
  onCreated?: (articleId: string) => void;
}

const ACCEPTED_EXTS = '.txt,.md,.html,.pdf,.json';

/**
 * Modal form for uploading a document and creating a new article in
 * one step. File size is enforced client-side (50 MiB cap matches
 * ``MAX_PARSE_BYTES`` on the backend) so we fail fast before paying
 * the network round-trip; the backend enforces the same cap
 * defensively as well.
 *
 * The accepted extension list is intentionally permissive — the
 * backend's parser is the authoritative source of truth for
 * supported formats (txt, md, html, pdf, json).
 */
export function ArticleUploadDialog({
  kbId,
  open,
  onOpenChange,
  onCreated,
}: ArticleUploadDialogProps): JSX.Element {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setFile(null);
      setTitle('');
      setSubmitting(false);
      setError(null);
    }
  }, [open]);

  const oversize = file !== null && file.size > MAX_UPLOAD_BYTES;
  const canSubmit = !submitting && file !== null && !oversize;

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>): void => {
    const next = event.target.files?.[0] ?? null;
    setFile(next);
    // Auto-fill the title from the filename when the title is empty,
    // stripped of its extension. Matches the backend behaviour for
    // the "no title supplied" branch.
    if (next !== null && title.trim().length === 0) {
      const stripped = next.name.replace(/\.[^.]+$/, '');
      setTitle(stripped);
    }
  };

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (!canSubmit || file === null) return;
    setSubmitting(true);
    setError(null);
    try {
      const formData = new FormData();
      formData.append('file', file);
      if (title.trim().length > 0) {
        formData.append('title', title.trim());
      }
      const article = await uploadArticle(kbId, formData);
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
      <DialogContent data-testid="article-upload-dialog">
        <form onSubmit={handleSubmit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>上传文档</DialogTitle>
            <DialogDescription>
              支持 txt / md / html / pdf / json,单个文件最大 {formatMaxSize(MAX_UPLOAD_BYTES)}。
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-2">
            <Label htmlFor="article-upload-file">文件</Label>
            <Input
              id="article-upload-file"
              type="file"
              accept={ACCEPTED_EXTS}
              onChange={handleFileChange}
              data-testid="article-upload-file"
            />
            {file !== null ? (
              <p className="text-xs text-muted-foreground" data-testid="article-upload-file-info">
                {file.name} · {(file.size / (1024 * 1024)).toFixed(2)} MiB
              </p>
            ) : null}
            {oversize ? (
              <p
                className="text-xs text-destructive"
                role="alert"
                data-testid="article-upload-oversize"
              >
                文件超过 {formatMaxSize(MAX_UPLOAD_BYTES)} 上限,请压缩后再上传。
              </p>
            ) : null}
          </div>

          <div className="space-y-2">
            <Label htmlFor="article-upload-title">标题 (可选)</Label>
            <Input
              id="article-upload-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="留空将使用文件名"
              data-testid="article-upload-title"
            />
          </div>

          {error !== null ? (
            <p className="text-sm text-destructive" role="alert" data-testid="article-upload-error">
              {error}
            </p>
          ) : null}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={submitting}
              data-testid="article-upload-cancel"
            >
              取消
            </Button>
            <Button
              type="submit"
              disabled={!canSubmit}
              data-testid="article-upload-submit"
            >
              {submitting ? '上传中…' : '上传'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
