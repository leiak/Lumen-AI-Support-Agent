import { login } from './login.js';
import { readTokenCache } from './token-cache.js';
import {
  claimConversation,
  fetchQueue,
  listMessages,
  postReply,
} from './conversations.js';
import { suggestReply } from './suggest.js';

const DEMO_EMAIL = process.env.DEMO_AGENT_EMAIL ?? 'agent@example.com';
const DEMO_PASSWORD = process.env.DEMO_PASSWORD ?? 'Demo123!';

function getToken(): string {
  const cache = readTokenCache();
  if (!cache) {
    throw new Error('No cached token. Run `pnpm api-client:login` first.');
  }
  return cache.accessToken;
}

async function cmdLogin(): Promise<void> {
  const token = await login(DEMO_EMAIL, DEMO_PASSWORD);
  console.log(`logged in as ${DEMO_EMAIL}; token cached.`);
  console.log(`first 32 chars: ${token.slice(0, 32)}…`);
}

async function cmdQueue(): Promise<void> {
  const queue = await fetchQueue(getToken());
  console.log(JSON.stringify(queue, null, 2));
}

async function cmdClaim(convId: string): Promise<void> {
  await claimConversation(getToken(), convId);
  console.log(`claimed ${convId}`);
}

async function cmdReply(convId: string, text: string): Promise<void> {
  const msg = await postReply(getToken(), convId, text);
  console.log(JSON.stringify(msg, null, 2));
}

async function cmdSuggest(convId: string): Promise<void> {
  const out = await suggestReply(getToken(), convId);
  console.log(JSON.stringify(out, null, 2));
}

async function cmdMessages(convId: string): Promise<void> {
  const msgs = await listMessages(getToken(), convId);
  console.log(JSON.stringify(msgs, null, 2));
}

const USAGE = `Usage: tsx src/api-client/cli.ts <command> [args]

Commands:
  login                          POST /auth/login with demo creds
  queue                          GET /agents/queue (PENDING conversations)
  claim <conv_id>                POST /agents/conversations/{id}/claim
  reply <conv_id> <text>         POST /conversations/{id}/messages
  suggest <conv_id>              POST /agents/conversations/{id}/suggest-reply
  messages <conv_id>             GET /conversations/{id}/messages
`;

async function main(): Promise<void> {
  const [, , cmd, ...args] = process.argv;
  if (!cmd) {
    console.error(USAGE);
    process.exit(2);
  }
  try {
    switch (cmd) {
      case 'login':
        await cmdLogin();
        break;
      case 'queue':
        await cmdQueue();
        break;
      case 'claim':
        if (!args[0]) throw new Error('claim requires <conv_id>');
        await cmdClaim(args[0]);
        break;
      case 'reply':
        if (!args[0] || !args.slice(1).join(' ')) throw new Error('reply requires <conv_id> <text…>');
        await cmdReply(args[0], args.slice(1).join(' '));
        break;
      case 'suggest':
        if (!args[0]) throw new Error('suggest requires <conv_id>');
        await cmdSuggest(args[0]);
        break;
      case 'messages':
        if (!args[0]) throw new Error('messages requires <conv_id>');
        await cmdMessages(args[0]);
        break;
      default:
        console.error(`unknown command: ${cmd}\n${USAGE}`);
        process.exit(2);
    }
  } catch (err) {
    console.error(`error: ${err instanceof Error ? err.message : String(err)}`);
    process.exit(1);
  }
}

main();
