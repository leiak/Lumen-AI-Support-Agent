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
  (response) => response,
  (error: AxiosError) => {
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
