import { beforeEach, describe, expect, it, vi } from 'vitest';

import { createHeader } from '../header.js';
import type { IframeConfig } from '../protocol.js';

function makeConfig(overrides: Partial<IframeConfig> = {}): IframeConfig {
  return {
    apiBaseUrl: 'https://api.example.com',
    channelId: 'chan_1',
    tenantId: 'tenant_a',
    widgetToken: 'jwt.here',
    title: 'Acme Support',
    subtitle: 'We are here to help',
    externalUserId: 'visitor-1',
    ...overrides,
  };
}

beforeEach(() => {
  document.body.innerHTML = '';
});

describe('createHeader', () => {
  it('renders tenant name from config', () => {
    const parentStub = { postMessage: vi.fn() };
    const targetWindow = { parent: parentStub } as unknown as Window;

    const header = createHeader(document, makeConfig(), targetWindow);
    document.body.appendChild(header.el);

    const titleEl = document.querySelector(
      '[data-lumen-iframe="header-title"]',
    )!;
    const subtitleEl = document.querySelector(
      '[data-lumen-iframe="header-subtitle"]',
    )!;

    expect(titleEl.textContent).toBe('Acme Support');
    expect(subtitleEl.textContent).toBe('We are here to help');
  });

  it('close button dispatches a {type:"close"} postMessage to the parent', () => {
    const parentStub = { postMessage: vi.fn() };
    const targetWindow = { parent: parentStub } as unknown as Window;

    const header = createHeader(document, makeConfig(), targetWindow);
    document.body.appendChild(header.el);

    const closeBtn = document.querySelector<HTMLButtonElement>(
      '[data-lumen-iframe="header-close"]',
    )!;
    closeBtn.click();

    expect(parentStub.postMessage).toHaveBeenCalledTimes(1);
    expect(parentStub.postMessage).toHaveBeenCalledWith(
      { type: 'close' },
      '*',
    );
  });
});