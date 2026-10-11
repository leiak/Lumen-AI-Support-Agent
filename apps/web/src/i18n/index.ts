/**
 * i18n bootstrap — Tier 3 Task 3.1.
 *
 * Sets up ``react-i18next`` with two locales:
 *   - zh-CN (default — the existing Chinese UI is the source of truth)
 *   - en-US (added in this task)
 *
 * Scope: this is infrastructure only. The first page to actually
 * adopt ``useTranslation`` is ``pages/inbox.tsx`` as a proof; the
 * remaining pages can be migrated incrementally (the keys fall back
 * to the literal string if missing, so old call sites keep working
 * untouched).
 *
 * Persistence: the chosen language is kept in localStorage under
 * ``lumen.lang`` so a returning user keeps their preference across
 * reloads. Default is ``zh-CN`` so the rest of the (un-migrated) SPA
 * keeps rendering in Chinese until more pages opt in.
 */

import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

import enUS from '@/i18n/locales/en-US.json';
import zhCN from '@/i18n/locales/zh-CN.json';

export const SUPPORTED_LANGUAGES = ['zh-CN', 'en-US'] as const;
export type SupportedLanguage = (typeof SUPPORTED_LANGUAGES)[number];

const STORAGE_KEY = 'lumen.lang';

function detectInitialLanguage(): SupportedLanguage {
  if (typeof window === 'undefined') return 'zh-CN';
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored && (SUPPORTED_LANGUAGES as readonly string[]).includes(stored)) {
      return stored as SupportedLanguage;
    }
  } catch {
    // localStorage may be blocked (e.g. private mode) — fall through.
  }
  const nav = window.navigator?.language;
  if (nav && nav.toLowerCase().startsWith('en')) return 'en-US';
  return 'zh-CN';
}

void i18n.use(initReactI18next).init({
  resources: {
    'zh-CN': { translation: zhCN },
    'en-US': { translation: enUS },
  },
  lng: detectInitialLanguage(),
  fallbackLng: 'zh-CN',
  interpolation: {
    escapeValue: false, // React already escapes
  },
  returnEmptyString: false,
});

export function persistLanguage(lang: SupportedLanguage): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, lang);
  } catch {
    // ignore — best-effort persistence
  }
}

export { default as i18n } from 'i18next';