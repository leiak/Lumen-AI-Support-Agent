import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { AxiosError } from 'axios';
import { ArrowLeft, FileUp, RefreshCw, Trash2 } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { ArticleStatusBadge } from '@/components/kb/article-status-badge';
import {
  MAX_UPLOAD_BYTES,
  deleteArticle,
  fetchArticle,
  reindexArticle,
  reuploadArticle,
  type ArticleStatus,
  type ArticleWithVersion,
} from '@/lib/knowledge';

interface FetchErrorPayload {
  detail?: string | Array<{ msg?: string }>;
}

function extractErrorMessage(error: unknown): string {
  if (error instanceof AxiosError) {
    const payload = error.response?.data as FetchErrorPayload | undefined;
    const detail = payload?.detail;
    if (typeof detail === 'string' && detail.trim().length > 0) return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0];
      if (first && typeof first === 'object' && typeof first.msg === 'string') {
        return first.msg;
      }
    }
    return error.message || '请求失败';
  }
  if (error instanceof Error && error.message) return error.message;
  return '请求失败';
}

const SOURCE_TYPE_LABEL = {
  upload: '文件',
  url: 'URL',
  manual: '文本',
} as const satisfies Record<ArticleWithVersion['source_type'], string>;

function formatDateTime(iso: string): string {
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return iso;
  return then.toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
}

function formatMaxSize(bytes: number): string {
  return `${bytes / (1024 * 1024)} MiB`;
}

export function ArticleDetailPage(): JSX.Element {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { kbId, articleId } = useParams<{ kbId: string; articleId: string }>();
  const safeKbId = kbId ?? '';
  const safeArticleId = articleId ?? '';

  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState(false);
  const [reuploadOpen, setReuploadOpen] = useState(false);
  const [reuploadFile, setReuploadFile] = useState<File | null>(null);
  const [reuploadTitle, setReuploadTitle] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);

  const articleQuery = useQuery({
    queryKey: ['article', safeArticleId],
    queryFn: () => fetchArticle(safeArticleId),
    enabled: Boolean(safeArticleId),
  });

  const reindexMutation = useMutation({
    mutationFn: () => reindexArticle(safeArticleId, { force: true }),
    onSuccess: () => {
      setActionError(null);
      void queryClient.invalidateQueries({ queryKey: ['article', safeArticleId] });
    },
    onError: (err) => {
      setActionError(extractErrorMessage(err));
    },
  });

  const reuploadMutation = useMutation({
    mutationFn: () => {
      if (reuploadFile === null) {
        return Promise.reject(new Error('请先选择文件'));
      }
      const formData = new FormData();
      formData.append('file', reuploadFile);
      if (reuploadTitle.trim().length > 0) {
        formData.append('title', reuploadTitle.trim());
      }
      return reuploadArticle(safeArticleId, formData);
    },
    onSuccess: () => {
      setActionError(null);
      setReuploadFile(null);
      setReuploadTitle('');
      if (fileInputRef.current) fileInputRef.current.value = '';
      setReuploadOpen(false);
      void queryClient.invalidateQueries({ queryKey: ['article', safeArticleId] });
    },
    onError: (err) => {
      setActionError(extractErrorMessage(err));
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteArticle(safeArticleId),
    onSuccess: () => {
      setPendingDelete(false);
      navigate(`/kb/${safeKbId}`);
    },
    onError: (err) => {
      setActionError(extractErrorMessage(err));
      setPendingDelete(false);
    },
  });

  // Auto-fill the reupload title from the current article title when
  // the dialog opens, so the user can tweak instead of re-type.
  useEffect(() => {
    if (reuploadOpen && articleQuery.data) {
      setReuploadTitle(articleQuery.data.title);
    }
    if (!reuploadOpen) {
      setReuploadFile(null);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  }, [reuploadOpen, articleQuery.data]);

  const handleBack = (): void => {
    if (safeKbId) navigate(`/kb/${safeKbId}`);
    else navigate('/kb');
  };

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>): void => {
    const next = event.target.files?.[0] ?? null;
    setReuploadFile(next);
  };

  const handleReuploadSubmit = async (): Promise<void> => {
    if (reuploadFile === null || reuploadFile.size > MAX_UPLOAD_BYTES) return;
    reuploadMutation.mutate();
  };

  if (articleQuery.isError) {
    return (
      <div className="flex h-full flex-col p-6" data-testid="article-detail-error">
        <div className="flex items-center gap-2">
          <Button type="button" size="sm" variant="outline" onClick={handleBack}>
            <ArrowLeft />
            返回文章列表
          </Button>
        </div>
        <div className="mt-6 flex flex-1 flex-col items-center justify-center gap-3 text-center">
          <p className="text-sm text-destructive" role="alert">
            {extractErrorMessage(articleQuery.error)}
          </p>
          <Button type="button" size="sm" onClick={() => void articleQuery.refetch()}>
            重试
          </Button>
        </div>
      </div>
    );
  }

  if (articleQuery.isLoading || !articleQuery.data) {
    return (
      <div
        className="flex h-full items-center justify-center"
        data-testid="article-detail-loading"
      >
        <Skeleton className="h-6 w-48" />
      </div>
    );
  }

  const article = articleQuery.data;
  const oversize = reuploadFile !== null && reuploadFile.size > MAX_UPLOAD_BYTES;

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      <div className="flex items-center gap-2 border-b bg-background px-4 py-2">
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={handleBack}
          data-testid="article-detail-back"
        >
          <ArrowLeft />
          返回
        </Button>
        <h2 className="truncate text-base font-semibold" title={article.title}>
          {article.title}
        </h2>
        <span className="ml-auto flex items-center gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => setReuploadOpen(true)}
            data-testid="article-detail-reupload"
          >
            <FileUp />
            重新上传
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => {
              setActionError(null);
              reindexMutation.mutate();
            }}
            disabled={reindexMutation.isPending}
            data-testid="article-detail-reindex"
          >
            <RefreshCw />
            重新索引
          </Button>
          <Button
            type="button"
            size="sm"
            variant="destructive"
            onClick={() => setPendingDelete(true)}
            data-testid="article-detail-delete"
          >
            <Trash2 />
            删除
          </Button>
        </span>
      </div>

      <div className="flex flex-1 flex-col gap-4 overflow-auto p-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">元数据</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-2 text-sm">
            <MetaRow label="ID">
              <span className="font-mono text-xs">{article.id}</span>
            </MetaRow>
            <MetaRow label="状态">
              <span className="flex items-center gap-2">
                <ArticleStatusBadge status={article.status as ArticleStatus} />
                <span className="text-xs text-muted-foreground">{article.status}</span>
              </span>
            </MetaRow>
            <MetaRow label="来源类型">{SOURCE_TYPE_LABEL[article.source_type]}</MetaRow>
            <MetaRow label="来源 URI">{article.source_uri ?? '—'}</MetaRow>
            <MetaRow label="创建时间">{formatDateTime(article.created_at)}</MetaRow>
            <MetaRow label="更新时间">{formatDateTime(article.updated_at)}</MetaRow>
            {article.error_message !== null ? (
              <MetaRow label="错误信息">
                <code
                  className="rounded bg-muted px-2 py-1 font-mono text-xs text-destructive"
                  data-testid="article-error-message"
                >
                  {article.error_message}
                </code>
              </MetaRow>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">正文</CardTitle>
          </CardHeader>
          <CardContent>
            {article.version !== null ? (
              <pre
                className="max-h-[480px] overflow-auto whitespace-pre-wrap rounded-md bg-muted/40 p-4 font-mono text-xs leading-relaxed"
                data-testid="article-raw-text"
              >
                {article.version.raw_text}
              </pre>
            ) : (
              <p className="text-sm text-muted-foreground" data-testid="article-no-version">
                暂无版本内容。
              </p>
            )}
          </CardContent>
        </Card>

        {actionError !== null ? (
          <p
            className="text-sm text-destructive"
            role="alert"
            data-testid="article-action-error"
          >
            {actionError}
          </p>
        ) : null}
      </div>

      <Dialog
        open={reuploadOpen}
        onOpenChange={(open) => {
          if (!reuploadMutation.isPending) setReuploadOpen(open);
        }}
      >
        <DialogContent data-testid="article-reupload-dialog">
          <DialogHeader>
            <DialogTitle>重新上传</DialogTitle>
            <DialogDescription>
              用新文件替换当前文章内容;最大 {formatMaxSize(MAX_UPLOAD_BYTES)}。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <input
              ref={fileInputRef}
              type="file"
              accept=".txt,.md,.html,.pdf,.json"
              onChange={handleFileChange}
              data-testid="article-reupload-file"
              className="block w-full text-sm file:mr-3 file:rounded-md file:border file:border-input file:bg-secondary file:px-3 file:py-1.5 file:text-sm file:font-medium hover:file:bg-secondary/80"
            />
            {reuploadFile !== null ? (
              <p className="text-xs text-muted-foreground">
                {reuploadFile.name} · {(reuploadFile.size / (1024 * 1024)).toFixed(2)} MiB
              </p>
            ) : null}
            {oversize ? (
              <p
                className="text-xs text-destructive"
                role="alert"
                data-testid="article-reupload-oversize"
              >
                文件超过 {formatMaxSize(MAX_UPLOAD_BYTES)} 上限。
              </p>
            ) : null}
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setReuploadOpen(false)}
              disabled={reuploadMutation.isPending}
              data-testid="article-reupload-cancel"
            >
              取消
            </Button>
            <Button
              type="button"
              onClick={() => {
                void handleReuploadSubmit();
              }}
              disabled={reuploadFile === null || oversize || reuploadMutation.isPending}
              data-testid="article-reupload-submit"
            >
              {reuploadMutation.isPending ? '上传中…' : '上传'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={pendingDelete}
        onOpenChange={(open) => {
          if (!deleteMutation.isPending) setPendingDelete(open);
        }}
      >
        <DialogContent data-testid="article-delete-confirm-dialog">
          <DialogHeader>
            <DialogTitle>确认删除文章</DialogTitle>
            <DialogDescription>
              删除文章{' '}
              <span className="font-medium text-foreground">{article.title}</span> ?
              该操作不可撤销。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setPendingDelete(false)}
              disabled={deleteMutation.isPending}
              data-testid="article-delete-confirm-cancel"
            >
              取消
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={() => deleteMutation.mutate()}
              disabled={deleteMutation.isPending}
              data-testid="article-delete-confirm-submit"
            >
              {deleteMutation.isPending ? '删除中…' : '确认删除'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

interface MetaRowProps {
  label: string;
  children: React.ReactNode;
}

function MetaRow({ label, children }: MetaRowProps): JSX.Element {
  return (
    <div className="grid grid-cols-[120px_1fr] items-baseline gap-3">
      <span className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span className="text-sm">{children}</span>
    </div>
  );
}
