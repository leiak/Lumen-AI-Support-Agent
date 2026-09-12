import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  cleanup,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AxiosError } from 'axios';

import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
import { LoginPage } from '@/pages/login';
import type * as ApiClient from '@/lib/api-client';

// Mock the api-client module so the form never touches a real network.
// We let the real `login()` (from @/lib/auth) run so its X-Tenant-Id header
// logic is exercised end-to-end through the mocked apiClient.
vi.mock('@/lib/api-client', async () => {
  const actual = await vi.importActual<typeof ApiClient>('@/lib/api-client');
  return {
    ...actual,
    apiClient: {
      post: vi.fn(),
      get: vi.fn(),
    },
  };
});

function renderLogin(initialEntry = '/login'): void {
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/inbox" element={<div data-testid="inbox-page">Inbox</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  vi.mocked(apiClient.post).mockReset();
});

afterEach(() => {
  cleanup();
});

describe('LoginPage', () => {
  it('test_login_form_renders_email_password_and_submit_button', () => {
    renderLogin();

    expect(screen.getByLabelText('邮箱')).toBeInTheDocument();
    expect(screen.getByLabelText('密码')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '登录' })).toBeInTheDocument();
    expect(
      screen.getByText('M1 demo · 联系管理员获取账号'),
    ).toBeInTheDocument();
    expect(screen.getByText('Lumen AI Support Agent')).toBeInTheDocument();
    expect(screen.getByText('坐席工作台')).toBeInTheDocument();
  });

  it('test_login_email_field_validates_required', async () => {
    const user = userEvent.setup();
    renderLogin();

    await user.click(screen.getByRole('button', { name: '登录' }));

    expect(await screen.findByText('请输入邮箱')).toBeInTheDocument();
    expect(apiClient.post).not.toHaveBeenCalled();
  });

  it('test_login_password_field_validates_required', async () => {
    const user = userEvent.setup();
    renderLogin();

    await user.type(screen.getByLabelText('邮箱'), 'agent@example.com');
    await user.click(screen.getByRole('button', { name: '登录' }));

    expect(await screen.findByText('请输入密码')).toBeInTheDocument();
    expect(apiClient.post).not.toHaveBeenCalled();
  });

  it('test_login_email_field_rejects_invalid_format', async () => {
    const user = userEvent.setup();
    renderLogin();

    await user.type(screen.getByLabelText('邮箱'), 'not-an-email');
    await user.type(screen.getByLabelText('密码'), 'password123');
    await user.click(screen.getByRole('button', { name: '登录' }));

    expect(await screen.findByText('邮箱格式不正确')).toBeInTheDocument();
    expect(apiClient.post).not.toHaveBeenCalled();
  });

  it('test_login_submit_calls_api_with_credentials', async () => {
    const user = userEvent.setup();
    vi.mocked(apiClient.post).mockResolvedValue({
      data: {
        access_token: 'jwt-abc',
        token_type: 'bearer',
        expires_in: 3600,
        user: {
          id: 'u1',
          tenant_id: 'demo',
          email: 'agent@example.com',
          full_name: 'Agent',
          role: 'agent',
        },
      },
    });
    renderLogin();

    await user.type(screen.getByLabelText('邮箱'), 'agent@example.com');
    await user.type(screen.getByLabelText('密码'), 'password123');
    await user.click(screen.getByRole('button', { name: '登录' }));

    await waitFor(() => expect(apiClient.post).toHaveBeenCalledTimes(1));
    const [url, payload, config] = vi.mocked(apiClient.post).mock.calls[0]!;
    expect(url).toBe('/api/v1/auth/login');
    expect(payload).toEqual({
      email: 'agent@example.com',
      password: 'password123',
    });
    expect(config?.headers).toMatchObject({ 'X-Tenant-Id': expect.any(String) });
  });

  it('test_login_success_stores_jwt_and_redirects', async () => {
    const user = userEvent.setup();
    vi.mocked(apiClient.post).mockResolvedValue({
      data: {
        access_token: 'jwt-success-token',
        token_type: 'bearer',
        expires_in: 3600,
        user: {
          id: 'u1',
          tenant_id: 'demo',
          email: 'agent@example.com',
          full_name: 'Agent',
          role: 'agent',
        },
      },
    });
    renderLogin();

    await user.type(screen.getByLabelText('邮箱'), 'agent@example.com');
    await user.type(screen.getByLabelText('密码'), 'password123');
    await user.click(screen.getByRole('button', { name: '登录' }));

    await waitFor(() =>
      expect(window.localStorage.getItem(JWT_STORAGE_KEY)).toBe('jwt-success-token'),
    );
    expect(await screen.findByTestId('inbox-page')).toBeInTheDocument();
  });

  it('test_login_failure_shows_error_message', async () => {
    const user = userEvent.setup();
    const axiosError = new AxiosError('Request failed');
    axiosError.response = {
      status: 401,
      data: { detail: 'Invalid credentials' },
      statusText: 'Unauthorized',
      headers: {},
      config: {} as never,
    };
    vi.mocked(apiClient.post).mockRejectedValue(axiosError);
    renderLogin();

    await user.type(screen.getByLabelText('邮箱'), 'agent@example.com');
    await user.type(screen.getByLabelText('密码'), 'wrong-password');
    await user.click(screen.getByRole('button', { name: '登录' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Invalid credentials');
    expect(window.localStorage.getItem(JWT_STORAGE_KEY)).toBeNull();
  });

  it('test_login_already_authenticated_redirects_to_inbox', async () => {
    window.localStorage.setItem(JWT_STORAGE_KEY, 'existing-token');
    renderLogin();

    expect(await screen.findByTestId('inbox-page')).toBeInTheDocument();
    expect(apiClient.post).not.toHaveBeenCalled();
  });
});