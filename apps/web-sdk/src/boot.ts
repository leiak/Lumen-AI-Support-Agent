/**
 * IIFE entry point. Boots the SDK and auto-initializes.
 *
 * esbuild bundles this as `globalName: LumenAICustomer` so the embedding
 * site can call `window.LumenAICustomer.open()` etc. after the script
 * tag finishes loading.
 */
import { boot } from './sdk.js';

const api = boot();
api.init();

export default api;
