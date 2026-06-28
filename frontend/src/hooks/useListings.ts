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
  distress_stage?: string;
  has_signals?: boolean;
  min_signals?: number;
  q?: string;
  sort?: string;
  order?: 'asc' | 'desc';
  // Blight pre-distress filters — only meaningful when distress_stage=distress_signal + Wayne market.
  conviction_tier?: 'A' | 'B' | 'C';
  min_balance?: number;
  min_tickets?: number;
  absentee?: boolean;
  // Owner-type filter for pre-distress leads (any market): 'individual' deprioritizes
  // investor/LLC-owned leads; 'entity' shows only them.
  owner_type?: 'individual' | 'entity';
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
  if (filters.distress_stage) p.set('distress_stage', filters.distress_stage);
  if (filters.has_signals != null) p.set('has_signals', String(filters.has_signals));
  if (filters.min_signals != null) p.set('min_signals', String(filters.min_signals));
  if (filters.q) p.set('q', filters.q);
  if (filters.sort) p.set('sort', filters.sort);
  if (filters.order) p.set('order', filters.order);
  if (filters.conviction_tier) p.set('conviction_tier', filters.conviction_tier);
  if (filters.min_balance != null) p.set('min_balance', String(filters.min_balance));
  if (filters.min_tickets != null) p.set('min_tickets', String(filters.min_tickets));
  if (filters.absentee) p.set('absentee', 'true');
  if (filters.owner_type) p.set('owner_type', filters.owner_type);

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
