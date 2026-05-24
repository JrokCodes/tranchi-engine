import { useQuery } from '@tanstack/react-query';
import { apiGet } from '../lib/api';
import type { SourcesResponse } from '../types';

export function useSources() {
  return useQuery({
    queryKey: ['sources'],
    queryFn: () => apiGet<SourcesResponse>('/sources'),
    staleTime: 30_000,
  });
}
