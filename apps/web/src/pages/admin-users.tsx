import { useState } from 'react';
import { KeyRound, Trash2, UserPlus } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  createTenantUser,
  deleteTenantUser,
  fetchTenantUsers,
  patchTenantUser,
  resetTenantUserPassword,
  USER_ROLES,
  type TenantUser,
  type TenantUserCreateInput,
  type TenantUserPasswordResetInput,
  type TenantUserUpdateInput,
  type UserRole,
} from '@/lib/tenant-users';
import { useCurrentUser } from '@/lib/use-current-user';
import { cn } from '@/lib/utils';

// Tenant Users admin page — full CRUD for agents / supervisors / admins.
//
// Backend (apps/api/src/admin/api.py):
//   GET    /api/v1/admin/tenants/{tid}/users
//   POST   /api/v1/admin/tenants/{tid}/users
//   PATCH  /api/v1/admin/tenants/{tid}/users/{uid}
//   POST   /api/v1/admin/tenants/{tid}/users/{uid}/reset-password
//   DELETE /api/v1/admin/tenants/{tid}/users/{uid}
//
// PII: emails + full names are PII; the page must never log them.
// Cross-tenant access returns 404 (server-enforced anti-enumeration,
// same pattern as kb-drafts / llm-configs / sla-policies / budget).
// DELETE-self is rejected with 400 by the server.

const TENANT_USERS_QUERY_KEY = ['admin', 'tenant-users'] as const;

const ROLE_STYLES: Record<UserRole, string> = {
  admin: 'bg-red-100 text-red-900 dark:bg-red-900/40 dark:text-red-200',
  supervisor:
    'bg-purple-100 text-purple-900 dark:bg-purple-900/40 dark:text-purple-200',
  agent: 'bg-sky-100 text-sky-900 dark:bg-sky-900/40 dark:text-sky-200',
  viewer:
    'bg-zinc-200 text-zinc-700 dark:bg-zinc-700/60 dark:text-zinc-200',
};

function RoleBadge({ role }: { role: UserRole }): JSX.Element {
  return (
    <Badge
      variant="outline"
      className={cn('border-transparent font-semibold', ROLE_STYLES[role])}
      data-testid={`user-role-badge-${role}`}
    >
      {role}
    </Badge>
  );
}

// Suppress tsc noUnusedLocals while the page is being wired up — the
// component will be rendered once the user-list row lands. WIP.
void RoleBadge;

function StatusBadge({ isActive }: { isActive: boolean }): JSX.Element {
  return (
    <Badge
      variant="outline"
      className={cn(
        'border-transparent font-semibold',
        isActive
          ? 'bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200'
          : 'bg-zinc-300 text-zinc-700 dark:bg-zinc-700 dark:text-zinc-300',
      )}
      data-testid={isActive ? 'user-status-active' : 'user-status-inactive'}
    >
      {isActive ? '启用' : '禁用'}
    </Badge>
  );
}

function UserEditorRow({
  user,
  tenantId,
  currentUserId,
}: {
  user: TenantUser;
  tenantId: string;
  currentUserId: string;
}): JSX.Element {
  const qc = useQueryClient();
  const [role, setRole] = useState<UserRole>(user.role);
  const [isActive, setIsActive] = useState(user.is_active);

  const dirty = role !== user.role || isActive !== user.is_active;

  const patchMut = useMutation({
    mutationFn: (input: TenantUserUpdateInput) =>
      patchTenantUser(tenantId, user.id, input),
    onSuccess: () => {
      void qc.invalidateQueries({
        queryKey: [...TENANT_USERS_QUERY_KEY, tenantId],
      });
    },
  });

  const deleteMut = useMutation({
    mutationFn: () => deleteTenantUser(tenantId, user.id),
    onSuccess: () => {
      void qc.invalidateQueries({
        queryKey: [...TENANT_USERS_QUERY_KEY, tenantId],
      });
    },
  });

  const [pwOpen, setPwOpen] = useState(false);
  const [pwValue, setPwValue] = useState('');
  const pwResetMut = useMutation({
    mutationFn: (input: TenantUserPasswordResetInput) =>
      resetTenantUserPassword(tenantId, user.id, input),
    onSuccess: () => {
      setPwOpen(false);
      setPwValue('');
    },
  });

  function handleSave(): void {
    patchMut.mutate({ role, is_active: isActive });
  }

  function handleDelete(): void {
    if (!window.confirm(`删除用户 “${user.email}”?`)) return;
    deleteMut.mutate();
  }

  function handleResetPw(): void {
    if (pwValue.length < 8) return;
    pwResetMut.mutate({ new_password: pwValue });
  }

  const isSelf = user.id === currentUserId;

  return (
    <tr className="border-b" data-testid={`user-row-${user.id}`}>
      <td className="px-4 py-3 align-middle">
        <span
          className="text-sm font-medium"
          data-testid={`user-email-${user.id}`}
        >
          {user.email}
        </span>
        {isSelf && (
          <span
            className="ml-2 rounded bg-blue-100 px-1.5 py-0.5 text-xs text-blue-900 dark:bg-blue-900/40 dark:text-blue-200"
            data-testid={`user-self-badge-${user.id}`}
          >
            你
          </span>
        )}
      </td>
      <td className="px-4 py-3 align-middle text-sm text-muted-foreground">
        <span data-testid={`user-full-name-${user.id}`}>
          {user.full_name ?? '—'}
        </span>
      </td>
      <td className="px-4 py-3 align-middle">
        <select
          className="rounded border px-2 py-1 text-sm"
          data-testid={`user-edit-role-${user.id}`}
          value={role}
          onChange={(e) => setRole(e.target.value as UserRole)}
          disabled={isSelf}
        >
          {USER_ROLES.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
      </td>
      <td className="px-4 py-3 align-middle">
        <label className="inline-flex items-center gap-1 text-xs">
          <input
            type="checkbox"
            data-testid={`user-edit-active-${user.id}`}
            checked={isActive}
            onChange={(e) => setIsActive(e.target.checked)}
            disabled={isSelf}
          />
          <StatusBadge isActive={isActive} />
        </label>
      </td>
      <td className="px-4 py-3 align-middle">
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            data-testid={`user-save-${user.id}`}
            onClick={handleSave}
            disabled={!dirty || patchMut.isPending || isSelf}
          >
            {patchMut.isPending ? '保存中…' : '保存'}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            data-testid={`user-reset-pw-${user.id}`}
            onClick={() => setPwOpen((v) => !v)}
          >
            <KeyRound className="mr-1 h-3 w-3" />
            重置密码
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            data-testid={`user-delete-${user.id}`}
            onClick={handleDelete}
            disabled={deleteMut.isPending || isSelf}
          >
            <Trash2 className="mr-1 h-3 w-3" />
            删除
          </Button>
        </div>
        {pwOpen && (
          <div
            className="mt-2 flex gap-2"
            data-testid={`user-pw-form-${user.id}`}
          >
            <input
              type="password"
              placeholder="新密码 (≥ 8 字符)"
              className="rounded border px-2 py-1 text-sm"
              data-testid={`user-pw-input-${user.id}`}
              value={pwValue}
              onChange={(e) => setPwValue(e.target.value)}
            />
            <Button
              type="button"
              size="sm"
              data-testid={`user-pw-submit-${user.id}`}
              onClick={handleResetPw}
              disabled={pwValue.length < 8 || pwResetMut.isPending}
            >
              提交
            </Button>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              onClick={() => {
                setPwOpen(false);
                setPwValue('');
              }}
            >
              取消
            </Button>
          </div>
        )}
      </td>
    </tr>
  );
}

function CreateUserForm({ tenantId }: { tenantId: string }): JSX.Element {
  const qc = useQueryClient();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [fullName, setFullName] = useState('');
  const [role, setRole] = useState<UserRole>('agent');

  const createMut = useMutation({
    mutationFn: (input: TenantUserCreateInput) =>
      createTenantUser(tenantId, input),
    onSuccess: () => {
      void qc.invalidateQueries({
        queryKey: [...TENANT_USERS_QUERY_KEY, tenantId],
      });
      setEmail('');
      setPassword('');
      setFullName('');
      setRole('agent');
    },
  });

  function handleSubmit(): void {
    if (email.length === 0 || password.length < 8) return;
    createMut.mutate({
      email,
      password,
      full_name: fullName.trim().length > 0 ? fullName.trim() : undefined,
      role,
    });
  }

  return (
    <Card data-testid="user-create-card">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <UserPlus className="h-4 w-4" />
          新建用户
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-5">
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">邮箱</span>
            <input
              type="email"
              className="rounded border px-2 py-1.5"
              data-testid="user-create-email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              maxLength={255}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">初始密码</span>
            <input
              type="password"
              className="rounded border px-2 py-1.5"
              data-testid="user-create-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              maxLength={128}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">姓名 (可选)</span>
            <input
              className="rounded border px-2 py-1.5"
              data-testid="user-create-full-name"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              maxLength={200}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted-foreground">角色</span>
            <select
              className="rounded border px-2 py-1.5"
              data-testid="user-create-role"
              value={role}
              onChange={(e) => setRole(e.target.value as UserRole)}
            >
              {USER_ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </label>
          <div className="flex items-end">
            <Button
              type="button"
              size="sm"
              data-testid="user-create-submit"
              onClick={handleSubmit}
              disabled={
                createMut.isPending || email.length === 0 || password.length < 8
              }
            >
              {createMut.isPending ? '创建中…' : '创建'}
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export function AdminUsersPage(): JSX.Element {
  const { user: currentUser } = useCurrentUser();
  const tenantId = currentUser?.tenant_id ?? '';
  const currentUserId = currentUser?.user_id ?? '';

  const usersQuery = useQuery({
    queryKey: [...TENANT_USERS_QUERY_KEY, tenantId],
    queryFn: () => fetchTenantUsers(tenantId),
    enabled: Boolean(tenantId),
  });

  return (
    <div
      className="flex h-full flex-col gap-6 p-6"
      data-testid="admin-users-page"
    >
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">用户管理</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          管理本租户内的 agent / supervisor / admin 账号。
        </p>
      </div>

      <CreateUserForm tenantId={tenantId} />

      {usersQuery.isLoading ? (
        <p
          className="text-sm text-muted-foreground"
          data-testid="users-loading"
        >
          加载中…
        </p>
      ) : usersQuery.isError ? (
        <p
          className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          role="alert"
          data-testid="users-error"
        >
          加载失败:{' '}
          {usersQuery.error instanceof Error
            ? usersQuery.error.message
            : '未知错误'}
        </p>
      ) : (usersQuery.data?.length ?? 0) === 0 ? (
        <p className="text-sm text-muted-foreground" data-testid="users-empty">
          暂无用户 — 上面填一张表提交即创建。
        </p>
      ) : (
        <div
          className="overflow-hidden rounded-md border bg-card"
          data-testid="users-table-wrapper"
        >
          <table className="w-full text-left text-sm">
            <thead className="bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-4 py-2 font-medium">邮箱</th>
                <th className="px-4 py-2 font-medium">姓名</th>
                <th className="px-4 py-2 font-medium">角色</th>
                <th className="px-4 py-2 font-medium">状态</th>
                <th className="px-4 py-2 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {usersQuery.data!.map((u) => (
                <UserEditorRow
                  key={u.id}
                  user={u}
                  tenantId={tenantId}
                  currentUserId={currentUserId}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}