# Lumen Web Widget SDK

Embeddable JavaScript widget for customer websites. A single, self-contained
`<script>` tag drops a floating chat button onto any page; clicking it opens
a chat window backed by the Lumen AI Support Agent WebSocket gateway.

## Build

```bash
pnpm install
pnpm type-check
pnpm build    # produces dist/lumen-widget.js
```

The build output is a single IIFE bundle. No React, no framework runtime —
just vanilla DOM and a few KB of CSS. Target bundle size: **< 50 KB**.

## Embed

```html
<script>
  window.LumenAICustomerConfig = {
    apiBaseUrl: 'https://api.example.com',
    channelId: '01HZ...',          // required: the widget channel id
    tenantId: '01HZ...',           // required: your tenant id
    widgetTokenEndpoint: '/api/v1/widget/token', // optional, default shown
    accentColor: '#0ea5e9',        // optional
    position: 'bottom-right',      // or 'bottom-left'
    title: '需要帮助?',             // optional, default shown
    subtitle: '我们的支持团队随时在线',
    locale: 'zh-CN',               // optional, default 'zh-CN'
    externalUserId: 'visitor-123', // optional: stable visitor id; auto-generated if omitted
  };
</script>
<script src="https://cdn.example.com/lumen-widget.js" async></script>
```

If `externalUserId` is omitted the SDK generates a random visitor id on
first load and persists it in `localStorage` so the same visitor keeps
their conversation history across reloads.

## Behaviour

- Floating button (bottom-right by default) — click to open the chat.
- Click outside or the close button — minimize back to the floating button.
- Open/closed state is persisted in `localStorage` so a returning visitor
  sees the same state.

## API

The SDK exposes a tiny global API:

```js
window.LumenAICustomer.open();   // open the chat window
window.LumenAICustomer.close();  // minimize back to the floating button
window.LumenAICustomer.destroy(); // remove all DOM + close WS
```

## CSP

The SDK does not use `eval`, `new Function`, or inline event handlers.
CSS is injected via a `<style>` tag (acceptable for most CSPs). No
external network calls besides your `apiBaseUrl`.

## Auth model

The SDK mints a short-lived (30 min) widget JWT on init by POSTing
`{channel_id, external_user_id}` to `widgetTokenEndpoint`. The JWT is
then passed to the WebSocket endpoint as `?token=...` (WebSocket clients
cannot set arbitrary headers, matching the pattern used elsewhere in
the platform).
