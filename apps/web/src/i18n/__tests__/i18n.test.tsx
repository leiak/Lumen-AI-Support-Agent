import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { I18nextProvider, useTranslation } from 'react-i18next';

import {
  i18n,
  persistLanguage,
  SUPPORTED_LANGUAGES,
} from '@/i18n';
import { LanguageSwitcher } from '@/components/layout/language-switcher';

// Tier 3 Task 3.1 — i18n smoke tests.
//
// Two assertions per layer:
//  1. The i18next instance has both locales registered and resolves
//     translation keys against them.
//  2. The LanguageSwitcher flips the active language and updates
//     downstream consumers via the React context (so the operator
//     sees the change without a reload).

function LocaleProbe({
  testId,
}: {
  testId: string;
}): JSX.Element {
  const { t, i18n: i18nInstance } = useTranslation();
  return (
    <p data-testid={testId} data-lang={i18nInstance.resolvedLanguage ?? i18nInstance.language}>
      {t('inbox.title')}
    </p>
  );
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  // Reset to a known locale between tests so cross-test pollution
  // doesn't bite (each test gets its own module-instance of i18next).
  void i18n.changeLanguage('zh-CN');
});

describe('i18n bootstrap (Tier 3 Task 3.1)', () => {
  it('registers both supported locales', () => {
    expect(SUPPORTED_LANGUAGES).toEqual(['zh-CN', 'en-US']);
    expect(i18n.hasResourceBundle('zh-CN', 'translation')).toBe(true);
    expect(i18n.hasResourceBundle('en-US', 'translation')).toBe(true);
  });

  it('resolves inbox.title against the current locale', () => {
    void i18n.changeLanguage('zh-CN');
    expect(i18n.t('inbox.title')).toBe('收件箱');
    void i18n.changeLanguage('en-US');
    expect(i18n.t('inbox.title')).toBe('Inbox');
  });

  it('persists the chosen language to localStorage', () => {
    persistLanguage('en-US');
    expect(window.localStorage.getItem('lumen.lang')).toBe('en-US');
  });
});

describe('LanguageSwitcher', () => {
  function renderSwitcher(): void {
    render(
      <I18nextProvider i18n={i18n}>
        <MemoryRouter>
          <LanguageSwitcher />
          <LocaleProbe testId="locale-probe" />
        </MemoryRouter>
      </I18nextProvider>,
    );
  }

  it('renders with the default (zh-CN) label pre-selected', () => {
    renderSwitcher();
    const select = screen.getByTestId(
      'language-switcher-select',
    ) as HTMLSelectElement;
    expect(select.value).toBe('zh-CN');
    expect(screen.getByTestId('locale-probe')).toHaveTextContent('收件箱');
  });

  it('flips the active locale + downstream consumers when changed', async () => {
    renderSwitcher();
    const user = userEvent.setup();
    const select = screen.getByTestId(
      'language-switcher-select',
    ) as HTMLSelectElement;
    await user.selectOptions(select, 'en-US');
    await waitFor(() =>
      expect(screen.getByTestId('locale-probe')).toHaveTextContent('Inbox'),
    );
    expect(select.value).toBe('en-US');
    expect(window.localStorage.getItem('lumen.lang')).toBe('en-US');
  });
});