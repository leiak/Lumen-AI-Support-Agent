import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { AxiosError } from 'axios';
import { ArrowLeft, FileUp, Plus } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { ArticleCreateDialog } from '@/components/kb/article-create-dialog';
import { ArticleList } from '@/components/kb/article-list';
import { ArticleUploadDialog } from '@/components/kb/article-upload-dialog';
import {
  deleteArticle,
  fetchArticles,
  fetchKnowledgeBase,
  reindexArticle,
  type Article,
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

interface DeleteConfirmationProps {
  article: Article | null;
  busy: boolean;
  errorMessage: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

function DeleteArticleDialog({
  article,
  busy,
  errorMessage,
  onCancel,
  onConfirm,
}: DeleteConfirmationProps): JSX.Element {
  const open = article !== null;
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next && !busy) onCancel();
      }}
    >
      <DialogContent data-testid="article-delete-dialog">
        <DialogHeader>
          <DialogTitle>确认删除文章</DialogTitle>
          <DialogDescription>
            删除文章{' '}
            <span className="font-medium text-foreground">
              {article?.title ?? ''}
            </span>
            ? 该操作不可撤销,向量也会一并清理。
          </DialogDescription>
        </DialogHeader>
        {errorMessage !== null ? (
          <p
            className="text-sm text-destructive"
            role="alert"
            data-testid="article-delete-error"
          >
            {errorMessage}
          </p>
        ) : null}
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={onCancel}
            disabled={busy}
            data-testid="article-delete-cancel"
          >
            取消
          </Button>
          <Button
            type="button"
            variant="destructive"
            onClick={onConfirm}
            disabled={busy}
            data-testid="article-delete-confirm"
          >
            {busy ? '删除中…' : '确认删除'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function KbDetailPage(): JSX.Element {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { kbId } = useParams<{ kbId: string }>();
  const safeKbId = kbId ?? '';

  const [createOpen, setCreateOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<Article | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [reindexError, setReindexError] = useState<string | null>(null);

  const kbQuery = useQuery({
    queryKey: ['knowledge-base', safeKbId],
    queryFn: () => fetchKnowledgeBase(safeKbId),
    enabled: Boolean(safeKbId),
  });

  const articlesQuery = useQuery({
    queryKey: ['knowledge-base', safeKbId, 'articles'],
    queryFn: () => fetchArticles(safeKbId),
    enabled: Boolean(safeKbId),
  });

  const deleteMutation = useMutation({
    mutationFn: (articleId: string) => deleteArticle(articleId),
    onSuccess: () => {
      setPendingDelete(null);
      setDeleteError(null);
      void queryClient.invalidateQueries({
        queryKey: ['knowledge-base', safeKbId, 'articles'],
      });
    },
    onError: (err) => {
      setDeleteError(extractErrorMessage(err));
    },
  });

  const reindexMutation = useMutation({
    mutationFn: (articleId: string) => reindexArticle(articleId, { force: true }),
    onSuccess: () => {
      setReindexError(null);
      void queryClient.invalidateQueries({
        queryKey: ['knowledge-base', safeKbId, 'articles'],
      });
    },
    onError: (err) => {
      setReindexError(extractErrorMessage(err));
    },
  });

  const handleBack = (): void => {
    navigate('/kb');
  };

  const handleCreated = (): void => {
    void queryClient.invalidateQueries({
      queryKey: ['knowledge-base', safeKbId, 'articles'],
    });
  };

  const handleAskDelete = (article: Article): void => {
    setDeleteError(null);
    setPendingDelete(article);
  };

  const handleCancelDelete = (): void => {
    if (deleteMutation.isPending) return;
    setPendingDelete(null);
    setDeleteError(null);
  };

  const handleConfirmDelete = (): void => {
    if (pendingDelete === null) return;
    deleteMutation.mutate(pendingDelete.id);
  };

  const handleReindex = (article: Article): void => {
    setReindexError(null);
    reindexMutation.mutate(article.id);
  };

  if (kbQuery.isError) {
    return (
      <div className="flex h-full flex-col p-6" data-testid="kb-detail-error">
        <div className="flex items-center gap-2">
          <Button type="button" size="sm" variant="outline" onClick={handleBack}>
            <ArrowLeft />
            返回知识库
          </Button>
        </div>
        <div className="mt-6 flex flex-1 flex-col items-center justify-center gap-3 text-center">
          <p className="text-sm text-destructive" role="alert">
            {extractErrorMessage(kbQuery.error)}
          </p>
          <Button type="button" size="sm" onClick={() => void kbQuery.refetch()}>
            重试
          </Button>
        </div>
      </div>
    );
  }

  if (kbQuery.isLoading || !kbQuery.data) {
    return (
      <div
        className="flex h-full items-center justify-center"
        data-testid="kb-detail-loading"
      >
        <p className="text-sm text-muted-foreground">加载中…</p>
      </div>
    );
  }

  const kb = kbQuery.data;

  return (
    <div className="flex h-full w-full flex-col overflow-hidden">
      <div className="flex items-center gap-2 border-b bg-background px-4 py-2">
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={handleBack}
          data-testid="kb-detail-back"
        >
          <ArrowLeft />
          返回
        </Button>
        <h2 className="text-base font-semibold" data-testid="kb-detail-name">
          {kb.name}
        </h2>
        <span className="ml-auto flex items-center gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => setUploadOpen(true)}
            data-testid="kb-detail-upload"
          >
            <FileUp />
            上传文档
          </Button>
          <Button
            type="button"
            size="sm"
            onClick={() => setCreateOpen(true)}
            data-testid="kb-detail-new-article"
          >
            <Plus />
            新建文章
          </Button>
        </span>
      </div>

      <div className="flex flex-1 flex-col overflow-hidden p-4">
        <Card className="flex flex-1 flex-col overflow-hidden">
          <CardHeader className="pb-2">
            <CardTitle className="text-base">文章</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-1 flex-col overflow-hidden p-0">
            {articlesQuery.isError ? (
              <div
                className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center"
                role="alert"
                data-testid="kb-detail-articles-error"
              >
                <p className="text-sm text-destructive">
                  {extractErrorMessage(articlesQuery.error)}
                </p>
                <Button type="button" size="sm" onClick={() => void articlesQuery.refetch()}>
                  重试
                </Button>
              </div>
            ) : (
              <ArticleList
                articles={articlesQuery.data ?? []}
                kbId={safeKbId}
                onDelete={handleAskDelete}
                onReindex={handleReindex}
                busyArticleId={reindexMutation.variables ?? null}
                isLoading={articlesQuery.isLoading}
              />
            )}
            {reindexError !== null ? (
              <p
                className="border-t px-4 py-2 text-xs text-destructive"
                role="alert"
                data-testid="kb-detail-reindex-error"
              >
                {reindexError}
              </p>
            ) : null}
          </CardContent>
        </Card>
      </div>

      <ArticleCreateDialog
        kbId={safeKbId}
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={handleCreated}
      />
      <ArticleUploadDialog
        kbId={safeKbId}
        open={uploadOpen}
        onOpenChange={setUploadOpen}
        onCreated={handleCreated}
      />
      <DeleteArticleDialog
        article={pendingDelete}
        busy={deleteMutation.isPending}
        errorMessage={deleteError}
        onCancel={handleCancelDelete}
        onConfirm={handleConfirmDelete}
      />
    </div>
  );
}
