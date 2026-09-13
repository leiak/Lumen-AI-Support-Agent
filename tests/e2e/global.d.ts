// Ambient declarations for browser-globals used inside page.evaluate() callbacks.
// Playwright tests run in Node but page.evaluate bodies execute in the
// page's JS context (browser). tsc can't see those globals without
// these stubs. We deliberately keep them minimal — anything more
// specific belongs in a real lib.dom.d.ts include.
declare const window: {
  localStorage: {
    getItem(key: string): string | null;
    setItem(key: string, value: string): void;
    removeItem(key: string): void;
  };
  location: { pathname: string };
};
declare function fetch(input: string, init?: unknown): Promise<{
  status: number;
  json(): Promise<unknown>;
  text(): Promise<string>;
}>;
