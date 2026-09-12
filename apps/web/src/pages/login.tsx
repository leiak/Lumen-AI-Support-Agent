import { useEffect, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { useLocation, useNavigate } from 'react-router-dom';
import { AxiosError, type AxiosError as AxiosErrorType } from 'axios';
import { z } from 'zod';

import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { JWT_STORAGE_KEY } from '@/lib/api-client';
import { login, lookupTenant } from '@/lib/auth';

// Form schema: Zod first, then inferred TS type for react-hook-form.
const loginSchema = z.object({
  email: z.string().min(1, '请输入邮箱').email('邮箱格式不正确'),
  password: z.string().min(1, '请输入密码'),
});

type LoginFormValues = z.infer<typeof loginSchema>;

interface LocationState {
  from?: string;
}

// Debounce window for the tenant-lookup trigger — long enough to coalesce
// keystrokes but short enough that the user can submit quickly after
// typing the email.
const LOOKUP_DEBOUNCE_MS = 300;

function extractErrorMessage(error: unknown): string {
  if (error instanceof AxiosError) {
    const axiosErr = error as AxiosErrorType<{ detail?: string | Array<unknown> }>;
    const detail = axiosErr.response?.data?.detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0];
      if (
        first &&
        typeof first === 'object' &&
        'msg' in first &&
        typeof (first as { msg: unknown }).msg === 'string'
      ) {
        return (first as { msg: string }).msg;
      }
    }
    return axiosErr.message;
  }
  if (error instanceof Error) return error.message;
  return '登录失败,请稍后再试。';
}

export function LoginPage(): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const [submitError, setSubmitError] = useState<string | null>(null);
  // Resolved tenant id from the lookup endpoint — populated only after a
  // successful tenant lookup for the typed email.
  const [resolvedTenantId, setResolvedTenantId] = useState<string | null>(null);
  // Soft signal that the lookup failed (returned null). We surface a
  // neutral "邮箱或租户信息无法识别" hint — we do NOT distinguish "user
  // does not exist" from "wrong tenant" to avoid leaking which emails
  // are registered (anti-enumeration).
  const [lookupFailed, setLookupFailed] = useState(false);

  // Track the latest email we've kicked off a lookup for so stale
  // callbacks from earlier debounce ticks don't clobber fresher state.
  const lookupSeqRef = useRef(0);

  // If the user is already authenticated, bounce straight to /inbox.
  useEffect(() => {
    const existing = window.localStorage.getItem(JWT_STORAGE_KEY);
    if (existing) {
      navigate('/inbox', { replace: true });
    }
  }, [navigate]);

  const {
    register,
    handleSubmit,
    watch,
    formState: { errors, isSubmitting },
  } = useForm<LoginFormValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: { email: '', password: '' },
  });

  // Re-resolve the tenant whenever the email changes (debounced).
  const emailValue = watch('email');
  useEffect(() => {
    if (!emailValue || !loginSchema.shape.email.safeParse(emailValue).success) {
      // Reset hint state when the email becomes invalid or empty so the
      // user doesn't see the "邮箱或租户信息无法识别" hint for partial
      // input they haven't finished typing yet.
      setResolvedTenantId(null);
      setLookupFailed(false);
      return;
    }
    const seq = ++lookupSeqRef.current;
    const handle = window.setTimeout(() => {
      void lookupTenant(emailValue)
        .then((hint) => {
          if (seq !== lookupSeqRef.current) return;
          if (hint.tenant_id) {
            setResolvedTenantId(hint.tenant_id);
            setLookupFailed(false);
          } else {
            setResolvedTenantId(null);
            setLookupFailed(true);
          }
        })
        .catch(() => {
          if (seq !== lookupSeqRef.current) return;
          // Lookup error: treat as if no tenant was resolved. The user
          // sees the generic hint and can still try to submit — POST
          // /login will return a generic 401 if the email/password are
          // wrong.
          setResolvedTenantId(null);
          setLookupFailed(true);
        });
    }, LOOKUP_DEBOUNCE_MS);
    return () => window.clearTimeout(handle);
  }, [emailValue]);

  const onSubmit = handleSubmit(async (values) => {
    setSubmitError(null);
    // Guard: if the tenant wasn't resolved we cannot submit because
    // the backend's POST /login requires X-Tenant-Id. Show a generic
    // error rather than firing the request.
    if (!resolvedTenantId) {
      setSubmitError('邮箱或租户信息无法识别,请确认后重试。');
      return;
    }
    try {
      const response = await login({
        email: values.email,
        password: values.password,
        tenantId: resolvedTenantId,
      });
      window.localStorage.setItem(JWT_STORAGE_KEY, response.access_token);
      const state = location.state as LocationState | null;
      const from = state?.from ?? '/inbox';
      navigate(from, { replace: true });
    } catch (error) {
      // Backend returns generic 401 message ("Invalid credentials" /
      // "邮箱或密码错误") — pass through as-is to avoid leaking whether
      // the email was valid.
      setSubmitError(extractErrorMessage(error));
    }
  });

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <Card className="w-full max-w-[400px]">
        <CardHeader>
          <CardTitle className="text-center">Lumen AI Support Agent</CardTitle>
          <CardDescription className="text-center">坐席工作台</CardDescription>
        </CardHeader>
        <CardContent>
          <form
            noValidate
            onSubmit={onSubmit}
            className="space-y-4"
            aria-label="登录表单"
          >
            <div className="space-y-2">
              <label htmlFor="email" className="text-sm font-medium">
                邮箱
              </label>
              <Input
                id="email"
                type="email"
                autoComplete="email"
                aria-invalid={errors.email || lookupFailed ? 'true' : 'false'}
                aria-describedby={lookupFailed ? 'email-hint' : undefined}
                {...register('email')}
              />
              {errors.email ? (
                <p className="text-xs text-destructive" role="alert">
                  {errors.email.message}
                </p>
              ) : lookupFailed ? (
                <p
                  id="email-hint"
                  data-testid="tenant-hint-failed"
                  className="text-xs text-muted-foreground"
                >
                  邮箱或租户信息无法识别,请联系管理员确认账号。
                </p>
              ) : null}
            </div>

            <div className="space-y-2">
              <label htmlFor="password" className="text-sm font-medium">
                密码
              </label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                aria-invalid={errors.password ? 'true' : 'false'}
                {...register('password')}
              />
              {errors.password ? (
                <p className="text-xs text-destructive" role="alert">
                  {errors.password.message}
                </p>
              ) : null}
            </div>

            {submitError ? (
              <div
                role="alert"
                aria-live="polite"
                className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
              >
                {submitError}
              </div>
            ) : null}

            <Button type="submit" className="w-full" disabled={isSubmitting}>
              {isSubmitting ? '登录中…' : '登录'}
            </Button>

            <p className="pt-2 text-center text-xs text-muted-foreground">
              M1 demo · 联系管理员获取账号
            </p>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}