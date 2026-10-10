import { describe, it, expect } from 'vitest';
import { signFeishuPayload } from '../src/webhook-sender/sign.js';

describe('signFeishuPayload', () => {
  it('matches the Lumen backend SHA-256(timestamp+nonce+key+body) scheme', () => {
    // Test vector derived from running:
    //   hashlib.sha256(("1234567890" + "abc" + "key123" + "hello").encode()).hexdigest()
    const sig = signFeishuPayload({
      timestamp: '1234567890',
      nonce: 'abc',
      encryptKey: 'key123',
      body: 'hello',
    });
    // Computed once and pinned here — any drift means we diverge from Lumen.
    expect(sig).toBe('a4d083495e8d193425e9b3ab7794a7dc9e670e3c5e0822624bbf3b59f99755bc');
    expect(sig).toHaveLength(64);
    expect(sig).toMatch(/^[0-9a-f]{64}$/);
  });

  it('produces a different signature when the body changes by one byte', () => {
    const a = signFeishuPayload({ timestamp: '1', nonce: 'n', encryptKey: 'k', body: 'foo' });
    const b = signFeishuPayload({ timestamp: '1', nonce: 'n', encryptKey: 'k', body: 'foO' });
    expect(a).not.toBe(b);
  });

  it('handles UTF-8 bodies', () => {
    const sig = signFeishuPayload({ timestamp: '1', nonce: 'n', encryptKey: 'k', body: '你好世界' });
    expect(sig).toHaveLength(64);
  });
});