import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AxiosError } from 'axios';
import { Plus } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { KbCreateDialog } from '@/components/kb/kb-create-dialog';
import { KbList } from '@/components/kb/kb-list';
import {
  deleteKnowledgeBase,
  fetchArticles,
  fetchKnowledgeBases,
  type KnowledgeBase,
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

const KB_QUERY_KEY = ['knowledge-bases'] as const;

export function KbPage(): JSX.Element {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [deletingKbId, setDeletingKbId] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const listQuery = useQuery({
    queryKey: [...KB_QUERY_KEY],
    queryFn: fetchKnowledgeBases,
  });

  // Pull article counts for every visible KB so the list can show
  // "文章数" without forcing the user into the detail page. Each KB
  // gets its own query so one slow list doesn't block the others.
  const knowledgeBases = listQuery.data ?? [];
  const articleCounts = useQuery({
    queryKey: ['knowledge-bases', 'article-counts', knowledgeBases.map((k) => k.id).join(',')],
    queryFn: async (): Promise<Record<string, number>> => {
      const entries = await Promise.all(
        knowledgeBases.map(async (kb) => {
          try {
            const articles = await fetchArticles(kb.id);
            return [kb.id, articles.length] as const;
          } catch {
            // A 404 / 403 here means the KB disappeared mid-render;
            // surface it as "unknown" rather than failing the whole
            // count fetch.
            return [kb.id, 0] as const;
          }
        }),
      );
      return Object.fromEntries(entries);
    },
    enabled: knowledgeBases.length > 0,
    staleTime: 30_000,
  });

  const deleteMutation = useMutation({
    mutationFn: (kbId: string) => deleteKnowledgeBase(kbId),
    onMutate: (kbId) => {
      setDeletingKbId(kbId);
      setDeleteError(null);
    },
    onError: (err) => {
      setDeleteError(extractErrorMessage(err));
    },
    onSettled: () => {
      setDeletingKbId(null);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: KB_QUERY_KEY });
    },
  });

  const handleDelete = (kb: KnowledgeBase): void => {
    setDeleteError(null);
    deleteMutation.mutate(kb.id);
  };

  const handleCreated = (kb: KnowledgeBase): void => {
    void queryClient.invalidateQueries({ queryKey: KB_QUERY_KEY });
    navigate(`/kb/${kb.id}`);
  };

  const countsByKb = useMemo<Record<string, number>>(
    () => articleCounts.data ?? {},
    [articleCounts.data],
  );

  return (
    <div className="flex h-full w-full flex-col p-4">
      <Card className="flex flex-1 flex-col overflow-hidden">
        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
          <CardTitle className="text-lg">知识库</CardTitle>
          <Button
            type="button"
            size="sm"
            onClick={() => setCreateOpen(true)}
            data-testid="kb-create-open"
          >
            <Plus />
            新建知识库
          </Button>
        </CardHeader>
        <CardContent className="flex flex-1 flex-col overflow-hidden p-0">
          {listQuery.isError ? (
            <div
              className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center"
              role="alert"
              data-testid="kb-error"
            >
              <p className="text-sm text-destructive">
                {extractErrorMessage(listQuery.error)}
              </p>
              <Button type="button" size="sm" onClick={() => void listQuery.refetch()}>
                重试
              </Button>
            </div>
          ) : listQuery.isLoading ? (
            <KbList
              knowledgeBases={[]}
              isLoading
              onDelete={() => undefined}
            />
          ) : (
            <KbList
              knowledgeBases={knowledgeBases}
              articleCounts={countsByKb}
              busyKbId={deletingKbId}
              onDelete={handleDelete}
            />
          )}

          {deleteError !== null ? (
            <p
              className="border-t px-4 py-2 text-xs text-destructive"
              role="alert"
              data-testid="kb-delete-error"
            >
              {deleteError}
            </p>
          ) : null}
        </CardContent>
      </Card>

      <KbCreateDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={handleCreated}
      />
    </div>
  );
}
