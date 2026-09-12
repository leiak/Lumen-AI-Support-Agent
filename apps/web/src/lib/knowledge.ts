import { z } from 'zod';

import { apiClient } from '@/lib/api-client';

// Backend shapes (verified against `apps/api/src/knowledge/schemas.py`,
// `apps/api/src/knowledge/enums.py` and `apps/api/src/knowledge/api.py`).
//
// Status / source_type values travel over the wire as lowercase strings
// (StrEnum). Models expose them on the DB as `String` columns; Pydantic
// re-validates them on read so the frontend sees the canonical lowercase
// forms (`draft`, `indexing`, `indexed`, `failed` / `upload`, `url`,
// `manual`).

export const ArticleStatusSchema = z.enum([
  'draft',
  'indexing',
  'indexed',
  'failed',
]);
export type ArticleStatus = z.infer<typeof ArticleStatusSchema>;

export const ArticleSourceTypeSchema = z.enum(['upload', 'url', 'manual']);
export type ArticleSourceType = z.infer<typeof ArticleSourceTypeSchema>;

export const KnowledgeBaseSchema = z.object({
  id: z.string(),
  tenant_id: z.string(),
  name: z.string(),
  slug: z.string(),
  description: z.string().nullable(),
  embedding_model: z.string(),
  chunk_size: z.number().int(),
  chunk_overlap: z.number().int(),
  created_at: z.string(),
  updated_at: z.string(),
});
export type KnowledgeBase = z.infer<typeof KnowledgeBaseSchema>;

export const KnowledgeBaseListSchema = z.object({
  items: z.array(KnowledgeBaseSchema),
});
export type KnowledgeBaseList = z.infer<typeof KnowledgeBaseListSchema>;

export const ArticleVersionSchema = z.object({
  id: z.string(),
  article_id: z.string(),
  version_number: z.number().int(),
  content_hash: z.string(),
  created_at: z.string(),
  raw_text: z.string(),
});
export type ArticleVersion = z.infer<typeof ArticleVersionSchema>;

export const ArticleSchema = z.object({
  id: z.string(),
  tenant_id: z.string(),
  knowledge_base_id: z.string(),
  title: z.string(),
  source_type: ArticleSourceTypeSchema,
  source_uri: z.string().nullable(),
  status: ArticleStatusSchema,
  current_version_id: z.string().nullable(),
  error_message: z.string().nullable(),
  created_at: z.string(),
  updated_at: z.string(),
});
export type Article = z.infer<typeof ArticleSchema>;

export const ArticleWithVersionSchema = ArticleSchema.extend({
  version: ArticleVersionSchema.nullable(),
});
export type ArticleWithVersion = z.infer<typeof ArticleWithVersionSchema>;

export const ArticleListSchema = z.object({
  items: z.array(ArticleSchema),
});
export type ArticleList = z.infer<typeof ArticleListSchema>;

export const ReindexResultSchema = z.object({
  article_id: z.string(),
  skipped: z.boolean(),
  version_number: z.number().int(),
  status: ArticleStatusSchema,
  chunks_indexed: z.number().int(),
});
export type ReindexResult = z.infer<typeof ReindexResultSchema>;

/** Known embedding-model identifiers accepted by the create-KB dialog. */
export const EMBEDDING_MODEL_OPTIONS = [
  'text-embedding-3-small',
  'text-embedding-3-large',
  'text-embedding-ada-002',
] as const;
export type EmbeddingModelOption = (typeof EMBEDDING_MODEL_OPTIONS)[number];

/** Default chunking knobs (matches the backend defaults). */
export const DEFAULT_CHUNK_SIZE = 800;
export const DEFAULT_CHUNK_OVERLAP = 100;

/**
 * GET /api/v1/knowledge/knowledge-bases — list KBs for the caller's tenant.
 * The endpoint returns at most 100 KBs (newest-first) and does NOT expose
 * a total count, so we surface `items.length` as the total to keep the
 * caller-facing shape consistent.
 */
export async function fetchKnowledgeBases(): Promise<KnowledgeBase[]> {
  const { data } = await apiClient.get('/api/v1/knowledge/knowledge-bases');
  const parsed = KnowledgeBaseListSchema.parse(data);
  return parsed.items;
}

export interface CreateKnowledgeBaseInput {
  name: string;
  description?: string | null;
  embedding_model?: EmbeddingModelOption;
}

/**
 * POST /api/v1/knowledge/knowledge-bases — create a KB. The backend slug
 * regex requires lowercase alnum + dash, starting with an alphanumeric;
 * we derive the slug from the name client-side so users don't have to
 * worry about the constraint (and the create dialog stays simple).
 */
export async function createKnowledgeBase(
  input: CreateKnowledgeBaseInput,
): Promise<KnowledgeBase> {
  const slug = slugify(input.name);
  const { data } = await apiClient.post('/api/v1/knowledge/knowledge-bases', {
    name: input.name,
    slug,
    description: input.description ?? null,
    embedding_model: input.embedding_model ?? 'text-embedding-3-small',
  });
  return KnowledgeBaseSchema.parse(data);
}

/**
 * DELETE /api/v1/knowledge/knowledge-bases/{id} — soft-delete a KB.
 *
 * The backend (Stage 6.9) cascade-deletes articles + versions + chunks
 * and best-effort-sweeps the Qdrant collection; the KB row itself is
 * not retained, but the operation is non-destructive from the user's
 * perspective (no article state remains visible). We treat the DELETE
 * as a soft-delete at the UX layer (confirmation dialog) even though
 * it's a hard delete server-side.
 */
export async function deleteKnowledgeBase(kbId: string): Promise<void> {
  await apiClient.delete(`/api/v1/knowledge/knowledge-bases/${kbId}`);
}

/**
 * GET /api/v1/knowledge/knowledge-bases/{id} — fetch a single KB.
 */
export async function fetchKnowledgeBase(kbId: string): Promise<KnowledgeBase> {
  const { data } = await apiClient.get(
    `/api/v1/knowledge/knowledge-bases/${kbId}`,
  );
  return KnowledgeBaseSchema.parse(data);
}

/**
 * GET /api/v1/knowledge/knowledge-bases/{id}/articles — list articles
 * inside a KB. The optional `status` query param filters server-side.
 */
export async function fetchArticles(
  kbId: string,
  params?: { status?: ArticleStatus },
): Promise<Article[]> {
  const { data } = await apiClient.get(
    `/api/v1/knowledge/knowledge-bases/${kbId}/articles`,
    { params: params?.status ? { status: params.status } : {} },
  );
  const parsed = ArticleListSchema.parse(data);
  return parsed.items;
}

export interface CreateArticleInput {
  title: string;
  source_type: ArticleSourceType;
  raw_text: string;
  source_uri?: string | null;
}

/**
 * POST /api/v1/knowledge/knowledge-bases/{id}/articles — create an
 * article from a typed/pasted raw_text body. The indexer is
 * fire-and-forget on the server, so the response is the freshly-created
 * article (status=DRAFT) and the status will transition asynchronously.
 */
export async function createArticle(
  kbId: string,
  input: CreateArticleInput,
): Promise<Article> {
  const { data } = await apiClient.post(
    `/api/v1/knowledge/knowledge-bases/${kbId}/articles`,
    {
      title: input.title,
      source_type: input.source_type,
      raw_text: input.raw_text,
      source_uri: input.source_uri ?? null,
    },
  );
  return ArticleSchema.parse(data);
}

/**
 * GET /api/v1/knowledge/articles/{id} — fetch an article with the
 * hydrated current ArticleVersion. The version carries `raw_text`,
 * which the detail page renders in a `<pre>` block.
 */
export async function fetchArticle(articleId: string): Promise<ArticleWithVersion> {
  const { data } = await apiClient.get(`/api/v1/knowledge/articles/${articleId}`);
  return ArticleWithVersionSchema.parse(data);
}

/**
 * DELETE /api/v1/knowledge/articles/{id} — hard-delete an article.
 * Best-effort Qdrant cleanup runs server-side before the row is
 * removed. 204 No Content on success.
 */
export async function deleteArticle(articleId: string): Promise<void> {
  await apiClient.delete(`/api/v1/knowledge/articles/${articleId}`);
}

/**
 * PATCH /api/v1/knowledge/articles/{id} — patch mutable metadata.
 * The current M1 surface only renames or sets a source_uri; raw_text
 * changes go through `/articles/{id}/upload` instead.
 */
export async function updateArticle(
  articleId: string,
  input: { title?: string; source_uri?: string | null },
): Promise<Article> {
  const { data } = await apiClient.patch(
    `/api/v1/knowledge/articles/${articleId}`,
    input,
  );
  return ArticleSchema.parse(data);
}

/**
 * POST /api/v1/knowledge/knowledge-bases/{id}/articles/upload — upload
 * a document and create a new article. Multipart: `file` (required),
 * optional `title`, optional `source_uri`. The backend enforces a 50 MiB
 * cap; we also check client-side to fail fast and avoid a wasteful
 * network round-trip.
 */
export async function uploadArticle(
  kbId: string,
  formData: FormData,
): Promise<Article> {
  const { data } = await apiClient.post(
    `/api/v1/knowledge/knowledge-bases/${kbId}/articles/upload`,
    formData,
  );
  return ArticleSchema.parse(data);
}

/**
 * POST /api/v1/knowledge/articles/{id}/upload — re-upload (replace
 * the existing article's content with a new file). Returns a
 * ReindexResult rather than an Article.
 */
export async function reuploadArticle(
  articleId: string,
  formData: FormData,
): Promise<ReindexResult> {
  const { data } = await apiClient.post(
    `/api/v1/knowledge/articles/${articleId}/upload`,
    formData,
  );
  return ReindexResultSchema.parse(data);
}

/**
 * POST /api/v1/knowledge/articles/{id}/reindex — force a re-index
 * pass. `force=true` mints a new version even when the content hash
 * is unchanged.
 */
export async function reindexArticle(
  articleId: string,
  options: { force?: boolean } = {},
): Promise<ReindexResult> {
  const { data } = await apiClient.post(
    `/api/v1/knowledge/articles/${articleId}/reindex`,
    { force: options.force ?? false },
  );
  return ReindexResultSchema.parse(data);
}

/**
 * 50 MiB cap (matches `apps/api/src/knowledge/parser.MAX_PARSE_BYTES`).
 * Exposed as a constant so the upload dialog can render the limit
 * hint and reject oversize files before they leave the browser.
 */
export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

/**
 * Convert an arbitrary KB name into the slug the backend expects.
 *
 * The backend regex is ``^[a-z0-9][a-z0-9-]{0,99}$`` — lowercase
 * alnum + dash, must start with an alphanumeric. We lowercase, replace
 * runs of disallowed characters with single dashes, and trim leading
 * / trailing dashes. When the name strips down to nothing (e.g. an
 * all-CJK string), we fall back to ``kb-{timestamp}`` so the slug is
 * never empty.
 */
export function slugify(input: string): string {
  const ascii = input
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 100);
  if (ascii.length > 0 && /^[a-z0-9]/.test(ascii)) return ascii;
  return `kb-${Date.now().toString(36)}`;
}
