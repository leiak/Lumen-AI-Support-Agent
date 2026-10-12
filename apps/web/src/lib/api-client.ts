import axios, { type AxiosError, type AxiosInstance } from 'axios';

export const JWT_STORAGE_KEY = 'lumen.jwt';

const baseURL = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? 'http://localhost:8000';

export const apiClient: AxiosInstance = axios.create({
  baseURL,
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
    Accept: 'application/json',
  },
});

apiClient.interceptors.request.use((config) => {
  const token = window.localStorage.getItem(JWT_STORAGE_KEY);
  if (token) {
    config.headers.set('Authorization', `Bearer ${token}`);
  }
  return config;
});

apiClient.interceptors.response.use(
  (response) => {
    // M6.6 — surface the server-side trace_id so support engineers
    // can grep Tempo by this id when a customer reports a bug. Cheap
    // single-line log; never throws.
    const traceId = response.headers['x-trace-id'] ?? response.headers['X-Trace-Id'];
    if (traceId) {
      // eslint-disable-next-line no-console
      console.info('Trace:', traceId);
    }
    return response;
  },
  (error: AxiosError) => {
    // Same on the error path: log the trace_id for cross-system
    // correlation when a 4xx/5xx surfaces to the SPA.
    const traceId =
      error.response?.headers['x-trace-id'] ?? error.response?.headers['X-Trace-Id'];
    if (traceId) {
      // eslint-disable-next-line no-console
      console.warn('Trace (error):', traceId);
    }
    if (error.response?.status === 401) {
      window.localStorage.removeItem(JWT_STORAGE_KEY);
      if (window.location.pathname !== '/login') {
        window.location.assign('/login');
      }
    }
    return Promise.reject(error);
  },
);

export function clearAuthToken(): void {
  window.localStorage.removeItem(JWT_STORAGE_KEY);
}
