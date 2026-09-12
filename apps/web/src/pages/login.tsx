import { useEffect, useState } from 'react';
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
import { login } from '@/lib/auth';
import { JWT_STORAGE_KEY } from '@/lib/api-client';

// Form schema: Zod first, then inferred TS type for react-hook-form.
const loginSchema = z.object({
  email: z.string().min(1, '请输入邮箱').email('邮箱格式不正确'),
  password: z.string().min(1, '请输入密码'),
});

type LoginFormValues = z.infer<typeof loginSchema>;

interface LocationState {
  from?: string;
}

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
    formState: { errors, isSubmitting },
  } = useForm<LoginFormValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: { email: '', password: '' },
  });

  const onSubmit = handleSubmit(async (values) => {
    setSubmitError(null);
    try {
      const response = await login({ email: values.email, password: values.password });
      window.localStorage.setItem(JWT_STORAGE_KEY, response.access_token);
      const state = location.state as LocationState | null;
      const from = state?.from ?? '/inbox';
      navigate(from, { replace: true });
    } catch (error) {
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
                aria-invalid={errors.email ? 'true' : 'false'}
                {...register('email')}
              />
              {errors.email ? (
                <p className="text-xs text-destructive" role="alert">
                  {errors.email.message}
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