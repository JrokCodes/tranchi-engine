import { useQuery } from '@tanstack/react-query';
import { apiClient, apiGet } from '../lib/api';
import type { ListingsResponse, ApiListingDetail } from '../types';

// ─── Filter params shape ───────────────────────────────────────────────────────

export interface ListingFilters {
  source_site?: string;
  status?: string;
  county?: string;
  city?: string;
  signal_type?: string;
  has_signals?: boolean;
  min_signals?: number;
  q?: string;
  sort?: string;
  order?: 'asc' | 'desc';
}

// ─── Build URLSearchParams ─────────────────────────────────────────────────────

export function buildParams(filters: ListingFilters, page: number, pageSize = 50): URLSearchParams {
  const p = new URLSearchParams();
  p.set('page', String(page));
  p.set('page_size', String(pageSize));

  if (filters.source_site) p.set('source_site', filters.source_site);
  if (filters.status) p.set('status', filters.status);
  if (filters.county) p.set('county', filters.county);
  if (filters.city) p.set('city', filters.city);
  if (filters.signal_type) p.set('signal_type', filters.signal_type);
  if (filters.has_signals != null) p.set('has_signals', String(filters.has_signals));
  if (filters.min_signals != null) p.set('min_signals', String(filters.min_signals));
  if (filters.q) p.set('q', filters.q);
  if (filters.sort) p.set('sort', filters.sort);
  if (filters.order) p.set('order', filters.order);

  return p;
}

// ─── useListings ──────────────────────────────────────────────────────────────

export function useListings(filters: ListingFilters = {}, page = 1) {
  const params = buildParams(filters, page);

  return useQuery({
    queryKey: ['listings', filters, page],
    queryFn: () =>
      apiClient
        .get<ListingsResponse>('/listings', { params })
        .then((r) => r.data),
    staleTime: 30_000,
  });
}

// ─── useListing (detail) ──────────────────────────────────────────────────────

export function useListing(id: string | null) {
  return useQuery({
    queryKey: ['listing', id],
    queryFn: () => apiGet<ApiListingDetail>(`/listings/${id}`),
    enabled: !!id,
    staleTime: 30_000,
  });
}
