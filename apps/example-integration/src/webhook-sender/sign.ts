import { createHash } from 'node:crypto';

export interface SignFeishuPayloadParams {
  timestamp: string;
  nonce: string;
  encryptKey: string;
  body: string;
}

/**
 * Compute the Feishu webhook signature.
 *
 * Lumen's verification (`apps/api/src/channel/feishu/signature.py:47-61`) uses:
 *   sha256(timestamp + nonce + encrypt_key + body).hexdigest()
 *
 * The encrypt_key for M1 is the literal stub
 * `M1_STUB_ENCRYPT_KEY_REPLACE_IN_TASK_4_13` (see webhook.py:31).
 */
export function signFeishuPayload(params: SignFeishuPayloadParams): string {
  const { timestamp, nonce, encryptKey, body } = params;
  const stringToSign = timestamp + nonce + encryptKey + body;
  return createHash('sha256').update(stringToSign, 'utf8').digest('hex');
}