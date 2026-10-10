/**
 * Fixture Feishu `im.message.receive_v1` event payload.
 *
 * Field shape must satisfy `apps/api/src/channel/feishu/adapter.py:parse_inbound`.
 * The backend persists this via the channel-agnostic `process_inbound_envelope`,
 * which creates/updates a Conversation row keyed on (tenant_id, channel_id,
 * sender_id.open_id).
 *
 * NOTE on `message.content`: the adapter at adapter.py:46 calls
 * `json.loads(content)` on this field — i.e. it expects a JSON-encoded
 * STRING, not a nested object. We serialize here so the HTTP body the
 * webhook sender POSTs decodes correctly end-to-end.
 */
export interface FeishuMessageEvent {
  schema: '2.0';
  header: {
    event_id: string;
    event_type: 'im.message.receive_v1';
    create_time: string;
    app_id: string;
    tenant_key: string;
  };
  event: {
    sender: {
      sender_id: { open_id: string };
      sender_type: 'user';
    };
    chat: { chat_id: string; chat_type: 'p2p' };
    message: {
      message_id: string;
      chat_id: string;
      message_type: 'text';
      content: string;
    };
  };
}

export function buildFeishuMessageEvent(text: string): FeishuMessageEvent {
  return {
    schema: '2.0',
    header: {
      event_id: `evt_${Date.now()}`,
      event_type: 'im.message.receive_v1',
      create_time: String(Math.floor(Date.now() / 1000)),
      app_id: 'demo-feishu-app-001',
      tenant_key: 'demo',
    },
    event: {
      sender: {
        sender_id: { open_id: `ou_demo_${Math.random().toString(36).slice(2, 10)}` },
        sender_type: 'user',
      },
      chat: { chat_id: 'oc_demo_chat', chat_type: 'p2p' },
      message: {
        message_id: `om_${Date.now()}`,
        chat_id: 'oc_demo_chat',
        message_type: 'text',
        content: JSON.stringify({ text }),
      },
    },
  };
}