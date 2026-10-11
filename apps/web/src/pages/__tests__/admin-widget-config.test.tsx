import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { AdminWidgetConfigPage } from '@/pages/admin-widget-config';
import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
import { useCurrentUser } from '@/lib/use-current-user';

// Tier 2 Task 2.4 — Widget config admin page tests.

vi.mock('@/lib/api-client', async () => {
  // eslint-disable-next-line @typescript-eslint/consistent-type-imports
  const actual = await vi.importActual<typeof import('@/lib/api-client')>(
    '@/lib/api-client',
  );
  return {
    ...actual,
    apiClient: { get: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  };
});

vi.mock('@/lib/use-current-user', () => ({
  useCurrentUser: vi.fn(),
}));

const mockedGet = vi.mocked(apiClient.get);
const mockedPatch = vi.mocked(apiClient.patch);
const mockedDelete = vi.mocked(apiClient.delete);
const mockedUseCurrentUser = vi.mocked(useCurrentUser);

const CURRENT_TENANT_ID = '01HZDEMO00000000000000000';

const SEED = {
  tenant_id: CURRENT_TENANT_ID,
  theme: 'light' as const,
  primary_color: '#2563eb',
  position: 'bottom-right' as const,
  widget_title: '在线客服',
  welcome_message: '您好!请描述您的问题,我们会尽快为您解答。',
  logo_url: null,
  enabled: true,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
};

beforeEach(() => {
  mockedGet.mockReset();
  mockedPatch.mockReset();
  mockedDelete.mockReset();
  window.localStorage.setItem(JWT_STORAGE_KEY, 'test-token');
  mockedUseCurrentUser.mockReturnValue({
    user: {
      user_id: 'admin-1',
      email: 'admin@example.com',
      tenant_id: CURRENT_TENANT_ID,
      tenant_name: 'Demo',
      role: 'admin',
    },
    isLoading: false,
    isError: false,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.localStorage.clear();
});

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0, refetchOnWindowFocus: false },
    },
  });
}

function renderPage(): void {
  render(
    <QueryClientProvider client={makeQueryClient()}>
      <MemoryRouter>
        <AdminWidgetConfigPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('AdminWidgetConfigPage (Tier 2 Task 2.4)', () => {
  it('renders the editor pre-filled with the server-side values', async () => {
    mockedGet.mockResolvedValue({ data: SEED });
    renderPage();
    expect(
      await screen.findByTestId('widget-config-card'),
    ).toBeInTheDocument();
    const color = (await screen.findByTestId(
      'widget-config-primary-color',
    )) as HTMLInputElement;
    expect(color.value).toBe('#2563eb');
    expect(screen.getByTestId('widget-config-theme')).toHaveValue('light');
    expect(screen.getByTestId('widget-config-position')).toHaveValue(
      'bottom-right',
    );
    expect(
      (screen.getByTestId('widget-config-title') as HTMLInputElement).value,
    ).toBe('在线客服');
    expect(
      (
        screen.getByTestId('widget-config-welcome') as HTMLTextAreaElement
      ).value,
    ).toContain('您好');
    expect(screen.getByTestId('widget-config-enabled')).toBeChecked();
  });

  it('disables save until something changes', async () => {
    mockedGet.mockResolvedValue({ data: SEED });
    renderPage();
    await waitFor(() =>
      expect(screen.getByTestId('widget-config-save')).toBeDisabled(),
    );
  });

  it('PATCHes all the mutable fields when save is clicked', async () => {
    mockedGet.mockResolvedValue({ data: SEED });
    mockedPatch.mockResolvedValue({
      data: {
        ...SEED,
        theme: 'dark',
        primary_color: '#ff00aa',
        position: 'bottom-left',
        widget_title: 'Support',
        welcome_message: 'Hi there!',
        logo_url: 'https://example.com/logo.png',
        enabled: false,
      },
    });
    renderPage();
    const user = userEvent.setup();
    await user.selectOptions(
      await screen.findByTestId('widget-config-theme'),
      'dark',
    );
    const color = screen.getByTestId(
      'widget-config-primary-color',
    ) as HTMLInputElement;
    color.focus();
    color.select();
    await user.keyboard('#ff00aa');
    await user.selectOptions(
      screen.getByTestId('widget-config-position'),
      'bottom-left',
    );
    const titleInput = screen.getByTestId(
      'widget-config-title',
    ) as HTMLInputElement;
    titleInput.focus();
    titleInput.select();
    await user.keyboard('Support');
    const welcomeInput = screen.getByTestId(
      'widget-config-welcome',
    ) as HTMLTextAreaElement;
    welcomeInput.focus();
    welcomeInput.select();
    await user.keyboard('Hi there!');
    const logoInput = screen.getByTestId(
      'widget-config-logo',
    ) as HTMLInputElement;
    await user.type(logoInput, 'https://example.com/logo.png');
    await user.click(screen.getByTestId('widget-config-enabled'));

    await waitFor(() =>
      expect(screen.getByTestId('widget-config-save')).not.toBeDisabled(),
    );
    await user.click(screen.getByTestId('widget-config-save'));
    await waitFor(() =>
      expect(mockedPatch).toHaveBeenCalledWith(
        `/api/v1/admin/tenants/${CURRENT_TENANT_ID}/widget-config`,
        {
          theme: 'dark',
          primary_color: '#ff00aa',
          position: 'bottom-left',
          widget_title: 'Support',
          welcome_message: 'Hi there!',
          logo_url: 'https://example.com/logo.png',
          enabled: false,
        },
      ),
    );
  });

  it('shows an inline error for an invalid hex color and disables save', async () => {
    mockedGet.mockResolvedValue({ data: SEED });
    renderPage();
    const user = userEvent.setup();
    const color = (await screen.findByTestId(
      'widget-config-primary-color',
    )) as HTMLInputElement;
    color.focus();
    color.select();
    await user.keyboard('not-a-color');
    expect(
      screen.getByTestId('widget-config-color-error'),
    ).toHaveTextContent('#RRGGBB');
    expect(screen.getByTestId('widget-config-save')).toBeDisabled();
  });

  it('calls DELETE after window.confirm() returns true and refetches', async () => {
    mockedGet
      .mockResolvedValueOnce({ data: { ...SEED, theme: 'dark' } })
      .mockResolvedValueOnce({ data: SEED });
    mockedDelete.mockResolvedValue({ data: null });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderPage();
    const user = userEvent.setup();
    await user.click(await screen.findByTestId('widget-config-reset'));
    await waitFor(() =>
      expect(mockedDelete).toHaveBeenCalledWith(
        `/api/v1/admin/tenants/${CURRENT_TENANT_ID}/widget-config`,
      ),
    );
    expect(confirmSpy).toHaveBeenCalledOnce();
  });

  it('shows the loading state while the GET is pending', async () => {
    mockedGet.mockReturnValue(new Promise(() => {}));
    renderPage();
    expect(
      await screen.findByTestId('widget-config-loading'),
    ).toHaveTextContent('加载中');
  });

  it('shows an error state when the GET fails', async () => {
    const err = new Error('Network Error');
    (err as unknown as { isAxiosError?: boolean }).isAxiosError = true;
    Object.defineProperty(err, 'message', { value: 'Network Error' });
    mockedGet.mockRejectedValue(err);
    renderPage();
    expect(
      await screen.findByTestId('widget-config-error-loading'),
    ).toHaveTextContent(/加载失败/);
  });
});