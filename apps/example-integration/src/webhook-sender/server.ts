import crypto from 'node:crypto';
import Fastify from 'fastify';
import { config } from '../shared/config.js';
import { signFeishuPayload } from './sign.js';
import { buildFeishuMessageEvent } from './feishu-event.js';

// M1 stub key — see apps/api/src/channel/feishu/webhook.py:31.
const FEISHU_STUB_ENCRYPT_KEY = 'M1_STUB_ENCRYPT_KEY_REPLACE_IN_TASK_4_13';

const PORT = Number(process.env.WEBHOOK_PORT ?? 4567);

async function postFeishuWebhook(body: object): Promise<{ status: number; text: string }> {
  const ts = String(Math.floor(Date.now() / 1000));
  const nonce = crypto.randomUUID();
  const bodyStr = JSON.stringify(body);
  const signature = signFeishuPayload({
    timestamp: ts,
    nonce,
    encryptKey: FEISHU_STUB_ENCRYPT_KEY,
    body: bodyStr,
  });

  const url = `${config.apiBaseUrl}/api/v1/channel/feishu/webhook/${config.feishuAppId}`;
  const resp = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Lark-Request-Timestamp': ts,
      'X-Lark-Request-Nonce': nonce,
      'X-Lark-Signature': signature,
    },
    body: bodyStr,
  });
  return { status: resp.status, text: await resp.text() };
}

export async function runSimulate(text = 'How do I reset my password?'): Promise<void> {
  const event = buildFeishuMessageEvent(text);
  const { status, text: respText } = await postFeishuWebhook(event);
  console.log(JSON.stringify({ status, response: safeJson(respText) }, null, 2));
  if (status !== 200) {
    process.exitCode = 1;
  }
}

function safeJson(s: string): unknown {
  try {
    return JSON.parse(s);
  } catch {
    return s;
  }
}

async function main(): Promise<void> {
  const simulateOnce = process.argv.includes('--simulate-once');
  if (simulateOnce) {
    await runSimulate();
    return;
  }

  const fastify = Fastify({ logger: true });

  fastify.post('/simulate-feishu', async (req, reply) => {
    const body = (req.body ?? {}) as { text?: string };
    const text = body.text ?? 'How do I reset my password?';
    const event = buildFeishuMessageEvent(text);
    try {
      const { status, text: respText } = await postFeishuWebhook(event);
      // Surface upstream non-2xx as 502 so callers can distinguish forward-success
      // from forward-failure (matches runSimulate's exitCode behavior).
      return reply
        .status(status >= 200 && status < 300 ? status : 502)
        .send({ status, response: safeJson(respText) });
    } catch (err) {
      req.log.error({ err }, 'webhook forward failed');
      return reply.status(502).send({ error: 'forward_failed', message: String(err) });
    }
  });

  fastify.get('/health', async () => ({ ok: true }));

  await fastify.listen({ port: PORT, host: '127.0.0.1' });
  console.log(`webhook-sender listening on http://127.0.0.1:${PORT}`);
  console.log(`  POST /simulate-feishu { text?: string }  → forwards to Lumen`);
  console.log(`  POST /simulate-feishu (--simulate-once)  → fire one and exit`);
}

// Entry-point guard: only run main() when this module is the script being
// executed. tsx invokes the module directly so process.argv[1] is the path
// to this file; importing it from a test must NOT start the server.
const isMain = process.argv[1] && process.argv[1].endsWith('server.ts');
if (isMain) {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}

export { postFeishuWebhook };