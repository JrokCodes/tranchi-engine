import axios, { type AxiosRequestConfig } from 'axios';

const API_BASE = import.meta.env.VITE_API_URL || '/api/v1';

export const apiClient = axios.create({
  baseURL: API_BASE,
  headers: { 'Content-Type': 'application/json' },
});

export function apiGet<T = unknown>(
  path: string,
  config?: AxiosRequestConfig
): Promise<T> {
  return apiClient.get<T>(path, config).then((r) => r.data);
}
