import { Globe } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import {
  persistLanguage,
  SUPPORTED_LANGUAGES,
  type SupportedLanguage,
} from '@/i18n';

/**
 * Language switcher — Tier 3 Task 3.1.
 *
 * A tiny inline `<select>` next to the logout button in the app
 * shell. Renders the *current* language's label, persists the
 * choice to localStorage so a returning user keeps the preference.
 *
 * Switching live-re-renders all components that consume
 * ``useTranslation``; pages that haven't been migrated yet keep
 * their hardcoded strings (no regression).
 */
export function LanguageSwitcher(): JSX.Element {
  const { i18n } = useTranslation();
  const current = (i18n.resolvedLanguage ?? i18n.language ?? 'zh-CN') as SupportedLanguage;

  const handleChange = (event: React.ChangeEvent<HTMLSelectElement>): void => {
    const next = event.target.value as SupportedLanguage;
    void i18n.changeLanguage(next);
    persistLanguage(next);
  };

  return (
    <label
      className="inline-flex items-center gap-1 text-xs text-muted-foreground"
      data-testid="language-switcher"
    >
      <Globe className="h-3 w-3" />
      <select
        aria-label="Language"
        className="rounded border bg-background px-1 py-0.5 text-xs"
        value={SUPPORTED_LANGUAGES.includes(current) ? current : 'zh-CN'}
        onChange={handleChange}
        data-testid="language-switcher-select"
      >
        {SUPPORTED_LANGUAGES.map((lang) => (
          <option key={lang} value={lang}>
            {i18n.getFixedT(lang)('shell.language.' + lang)}
          </option>
        ))}
      </select>
    </label>
  );
}