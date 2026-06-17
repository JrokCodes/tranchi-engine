import { useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import { AnimatePresence, motion } from 'framer-motion';
import { ChevronLeft, ChevronRight, ChevronUp, ChevronDown, Flame, MapPin } from 'lucide-react';
import { FilterBar, defaultFilters, type FilterState } from '../components/FilterBar';
import { DetailDrawer } from '../components/DetailDrawer';
import { StatusBadge } from '../components/shared/StatusBadge';
import { TableRowSkeleton } from '../components/shared/LoadingSkeleton';
import { useListings } from '../hooks/useListings';
import {
  cn,
  formatSaleDate,
  formatRelative,
  sourceBadgeClass,
  sourceLabel,
} from '../lib/utils';
import type { ApiListingItem } from '../types';

// ─── Table columns ─────────────────────────────────────────────────────────────

const COLUMNS = [
  { key: 'thumb', label: '', sortable: false, width: 'w-20' },
  { key: 'property_address', label: 'Address', sortable: true, width: 'min-w-[200px]' },
  { key: 'property_city', label: 'City', sortable: false, width: 'w-32' },
  { key: 'source_site', label: 'Source', sortable: false, width: 'w-32' },
  { key: 'conviction_tier', label: 'Tier', sortable: false, width: 'w-24' },
  { key: 'signal_count', label: 'Signals', sortable: true, width: 'w-28' },
  { key: 'is_hot', label: 'HOT', sortable: false, width: 'w-16' },
  { key: 'status', label: 'Status', sortable: false, width: 'w-28' },
  { key: 'sale_date', label: 'Sale Date', sortable: true, width: 'w-28' },
  { key: 'first_seen_at', label: 'First Seen', sortable: true, width: 'w-28' },
] as const;

type SortKey = 'property_address' | 'signal_count' | 'sale_date' | 'first_seen_at';

// ─── Listings page ─────────────────────────────────────────────────────────────

export default function Listings() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [selectedId, setSelectedId] = useState<string | null>(searchParams.get('listing'));
  const [page, setPage] = useState(1);
  const [sortField, setSortField] = useState<SortKey>('first_seen_at');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc');

  const [filters, setFilters] = useState<FilterState>(() => {
    return {
      ...defaultFilters,
      county: searchParams.get('county') ?? '',
      source_site: searchParams.get('source_site') ?? '',
      status: searchParams.get('status') ?? 'active',
      distress_stage: searchParams.get('distress_stage') ?? 'buy_now',
      has_signals: searchParams.get('has_signals') === 'true',
      q: searchParams.get('q') ?? '',
      sort: (searchParams.get('sort') as SortKey) ?? 'first_seen_at',
      order: (searchParams.get('order') as 'asc' | 'desc') ?? 'desc',
      conviction_tier: (searchParams.get('conviction_tier') as 'A' | 'B' | 'C') ?? '',
      min_balance: searchParams.get('min_balance') ?? '',
      min_tickets: searchParams.get('min_tickets') ?? '',
      absentee: searchParams.get('absentee') === 'true',
    };
  });

  // Sync ?listing= param with drawer
  useEffect(() => {
    const param = searchParams.get('listing');
    if (param && param !== selectedId) setSelectedId(param);
  }, [searchParams]);

  useEffect(() => {
    if (!selectedId && searchParams.get('listing')) {
      const next = new URLSearchParams(searchParams);
      next.delete('listing');
      setSearchParams(next, { replace: true });
    }
  }, [selectedId]);

  function handleFilterChange(newFilters: FilterState) {
    setFilters(newFilters);
    setPage(1);
    const next = new URLSearchParams();
    if (newFilters.county) next.set('county', newFilters.county);
    if (newFilters.source_site) next.set('source_site', newFilters.source_site);
    if (newFilters.status) next.set('status', newFilters.status);
    if (newFilters.distress_stage && newFilters.distress_stage !== 'buy_now')
      next.set('distress_stage', newFilters.distress_stage);
    if (newFilters.has_signals) next.set('has_signals', 'true');
    if (newFilters.q) next.set('q', newFilters.q);
    if (newFilters.conviction_tier) next.set('conviction_tier', newFilters.conviction_tier);
    if (newFilters.min_balance) next.set('min_balance', newFilters.min_balance);
    if (newFilters.min_tickets) next.set('min_tickets', newFilters.min_tickets);
    if (newFilters.absentee) next.set('absentee', 'true');
    setSearchParams(next, { replace: true });
  }

  function handleClearFilters() {
    setFilters(defaultFilters);
    setPage(1);
    setSearchParams({}, { replace: true });
  }

  function handleSort(key: SortKey) {
    if (sortField === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortField(key);
      setSortDir('desc');
    }
    setPage(1);
  }

  function handleRowClick(id: string) {
    setSelectedId(id);
    const next = new URLSearchParams(searchParams);
    next.set('listing', id);
    setSearchParams(next, { replace: true });
  }

  const apiFilters = {
    county: filters.county || undefined,
    source_site: filters.source_site || undefined,
    status: filters.status || undefined,
    distress_stage: filters.distress_stage || undefined,
    has_signals: filters.has_signals || undefined,
    q: filters.q || undefined,
    sort: sortField,
    order: sortDir,
    // Blight params — only forwarded when set; API ignores them on non-Wayne markets.
    conviction_tier: (filters.conviction_tier as 'A' | 'B' | 'C' | undefined) || undefined,
    min_balance: filters.min_balance ? Number(filters.min_balance) : undefined,
    min_tickets: filters.min_tickets ? Number(filters.min_tickets) : undefined,
    absentee: filters.absentee || undefined,
  };

  const { data, isLoading, isError } = useListings(apiFilters, page);
  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const totalPages = data?.total_pages ?? 1;

  return (
    <>
      <div>
        {/* Page header */}
        <motion.div
          initial={{ opacity: 0, y: -6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.25 }}
          className="mb-6"
        >
          <h1
            className="text-[32px] font-semibold text-(--color-navy) leading-tight"
            style={{ fontFamily: 'var(--font-heading)' }}
          >
            Listings
          </h1>
          <p className="text-[14px] text-(--color-slate) mt-1.5">
            {isLoading ? 'Loading...' : `${total.toLocaleString()} listings`}
          </p>
        </motion.div>

        {/* Filter bar */}
        <div className="mb-5">
          <FilterBar
            filters={filters}
            onChange={handleFilterChange}
            onClear={handleClearFilters}
          />
        </div>

        {/* Error */}
        {isError && (
          <div className="mb-4 px-4 py-3 bg-[#DC2626]/5 border border-[#DC2626]/20 rounded-xl">
            <p className="text-[13px] text-(--color-danger)">
              Could not load listings — check API connection
            </p>
          </div>
        )}

        {/* Table */}
        <div className="bg-white rounded-xl border border-(--color-border) shadow-sm overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full" aria-label="Property listings">
              <thead>
                <tr className="bg-(--color-bg-subtle) border-b border-(--color-border)">
                  {COLUMNS.map((col) => (
                    <th
                      key={col.key}
                      className={cn(
                        'px-4 h-11 text-left text-[11px] uppercase tracking-wider text-(--color-muted)',
                        'font-semibold whitespace-nowrap select-none',
                        col.width,
                        col.sortable && 'cursor-pointer hover:text-(--color-ink) transition-colors'
                      )}
                      onClick={col.sortable ? () => handleSort(col.key as SortKey) : undefined}
                    >
                      <span className="flex items-center gap-1">
                        {col.label}
                        {col.sortable && (
                          <span
                            className={cn(
                              'flex flex-col',
                              sortField === col.key ? 'text-(--color-navy)' : 'opacity-30'
                            )}
                            aria-hidden="true"
                          >
                            {sortField === col.key ? (
                              sortDir === 'asc' ? <ChevronUp size={11} /> : <ChevronDown size={11} />
                            ) : (
                              <span className="text-[10px] leading-none">↕</span>
                            )}
                          </span>
                        )}
                      </span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {isLoading ? (
                  Array.from({ length: 10 }).map((_, i) => (
                    <TableRowSkeleton key={i} cols={COLUMNS.length} />
                  ))
                ) : items.length === 0 ? (
                  <tr>
                    <td colSpan={COLUMNS.length} className="px-4 py-16 text-center">
                      <p className="text-[14px] text-(--color-muted)">
                        No listings match the current filters
                      </p>
                    </td>
                  </tr>
                ) : (
                  <AnimatePresence mode="wait">
                    {items.map((item, i) => (
                      <ListingRow
                        key={item.id}
                        item={item}
                        index={i}
                        onClick={() => handleRowClick(item.id)}
                      />
                    ))}
                  </AnimatePresence>
                )}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          <div className="flex items-center justify-between px-4 py-3 border-t border-(--color-border) bg-(--color-bg-subtle)">
            <span className="text-[13px] text-(--color-slate)">
              {total === 0
                ? 'No results'
                : `Showing ${(page - 1) * 50 + 1}–${Math.min(page * 50, total)} of ${total.toLocaleString()}`}
            </span>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page === 1}
                className="flex items-center gap-1 h-8 px-3 text-[13px] rounded-full border border-(--color-border) text-(--color-slate) hover:text-(--color-ink) hover:border-(--color-navy)/25 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                aria-label="Previous page"
              >
                <ChevronLeft size={14} />
                Prev
              </button>
              <span className="text-[13px] text-(--color-muted) tabular-nums px-2">
                {page} / {Math.max(1, totalPages)}
              </span>
              <button
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={page >= totalPages}
                className="flex items-center gap-1 h-8 px-3 text-[13px] rounded-full border border-(--color-border) text-(--color-slate) hover:text-(--color-ink) hover:border-(--color-navy)/25 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                aria-label="Next page"
              >
                Next
                <ChevronRight size={14} />
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Detail drawer */}
      <DetailDrawer listingId={selectedId} onClose={() => setSelectedId(null)} />
    </>
  );
}

// ─── Conviction tier badge ─────────────────────────────────────────────────────
// Rendered in the table row (and reused in the detail drawer) for Wayne–Detroit
// blight pre-distress leads. A = high conviction (red), B = medium (gold), C = watch (slate).

const TIER_STYLE: Record<'A' | 'B' | 'C', string> = {
  A: 'bg-[#DC2626]/10 text-[#DC2626] border-[#DC2626]/20',
  B: 'bg-(--color-gold-light) text-[#8B6914] border-(--color-gold)/25',
  C: 'bg-(--color-bg-elevated) text-(--color-slate) border-(--color-border)',
};

const TIER_LABEL: Record<'A' | 'B' | 'C', string> = {
  A: 'A — High',
  B: 'B — Med',
  C: 'C — Watch',
};

export function ConvictionTierBadge({ tier }: { tier: 'A' | 'B' | 'C' }) {
  return (
    <span
      className={cn(
        'inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold border whitespace-nowrap',
        TIER_STYLE[tier]
      )}
      title={`Conviction tier ${tier}`}
    >
      {TIER_LABEL[tier]}
    </span>
  );
}

// ─── Listing row ───────────────────────────────────────────────────────────────

interface RowProps {
  item: ApiListingItem;
  index: number;
  onClick: () => void;
}

// Small street-view thumbnail with a graceful MapPin fallback (no key / no coverage).
function ListingThumb({ url }: { url: string | null }) {
  const [imgError, setImgError] = useState(false);
  if (!url || imgError) {
    return (
      <div className="h-10 w-14 rounded-md bg-(--color-bg-elevated) flex items-center justify-center">
        <MapPin size={14} className="text-(--color-muted)" />
      </div>
    );
  }
  return (
    <img
      src={url}
      alt=""
      loading="lazy"
      onError={() => setImgError(true)}
      className="h-10 w-14 rounded-md object-cover bg-(--color-bg-elevated)"
    />
  );
}

function ListingRow({ item, index, onClick }: RowProps) {
  return (
    <motion.tr
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ delay: index * 0.015 }}
      onClick={onClick}
      className="border-b border-(--color-border-subtle) h-14 cursor-pointer hover:bg-(--color-bg-subtle) transition-colors group"
    >
      {/* Thumbnail */}
      <td className="pl-4 pr-1">
        <ListingThumb url={item.street_view_url} />
      </td>

      {/* Address + parcel·owner + county.
          The parcel·owner sub-line disambiguates distinct parcels that share a
          non-distinctive street (e.g. many lots on "Cordova Ave", or land-bank "0"
          addresses) so they stop reading as duplicates. Data already served by the
          API (source_listing_id = parcel, owner_name). */}
      <td className="px-4">
        <p className="text-[13px] font-medium text-(--color-ink) group-hover:text-(--color-navy) transition-colors truncate max-w-[240px]">
          {item.property_address}
          {item.address_status === 'no_street_number' && (
            <span
              className="ml-1.5 inline-flex items-center px-1.5 py-px rounded-full text-[10px] font-medium bg-(--color-bg-elevated) text-(--color-muted) border border-(--color-border) align-middle"
              title="No house number on file — verify this parcel by parcel number, not street address"
            >
              no #
            </span>
          )}
        </p>
        {item.source_listing_id && (
          <p className="text-[11px] text-(--color-muted) truncate max-w-[240px]">
            parcel {item.source_listing_id}
            {item.owner_name && item.owner_name !== '(Owner not found)' && (
              <span> · {item.owner_name}</span>
            )}
          </p>
        )}
        <p className="text-[11px] text-(--color-muted)">{item.property_county} Co.</p>
      </td>

      {/* City */}
      <td className="px-4 text-[13px] text-(--color-slate) whitespace-nowrap">
        {item.property_city}
      </td>

      {/* Source badge */}
      <td className="px-4">
        <span
          className={cn(
            'inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium whitespace-nowrap',
            sourceBadgeClass(item.source_site)
          )}
        >
          {sourceLabel(item.source_site)}
        </span>
      </td>

      {/* Conviction tier (Wayne–Detroit blight leads only; empty cell on all other listings) */}
      <td className="px-4">
        {item.conviction_tier && (
          <ConvictionTierBadge tier={item.conviction_tier} />
        )}
      </td>

      {/* Signals */}
      <td className="px-4 py-2">
        {item.signal_types.length > 0 ? (
          <div className="flex flex-wrap gap-1">
            {item.signal_types.slice(0, 2).map((st) => (
              <span
                key={st.label}
                className="inline-flex items-center gap-0.5 px-2 py-0.5 bg-(--color-bg-elevated) text-(--color-ink) rounded-full text-[11px] font-medium border border-(--color-border) whitespace-nowrap"
              >
                {st.label}
                {st.count > 1 && <span className="text-(--color-slate)"> ×{st.count}</span>}
              </span>
            ))}
            {item.signal_types.length > 2 && (
              <span className="text-[11px] text-(--color-muted) self-center">
                +{item.signal_types.length - 2}
              </span>
            )}
          </div>
        ) : (
          <span className="text-[13px] text-(--color-muted)">—</span>
        )}
      </td>

      {/* HOT */}
      <td className="px-4">
        {item.is_hot && (
          <span
            className="inline-flex items-center gap-1 px-2 py-0.5 bg-(--color-gold-light) text-[#8B6914] rounded-full text-[11px] font-medium border border-(--color-gold)/25"
            title="High-signal property"
          >
            <Flame size={10} />
            HOT
          </span>
        )}
      </td>

      {/* Status */}
      <td className="px-4">
        <StatusBadge status={item.status} />
      </td>

      {/* Sale date */}
      <td className="px-4 text-[13px] text-(--color-slate) whitespace-nowrap">
        {item.sale_date ? formatSaleDate(item.sale_date) : '—'}
      </td>

      {/* First seen */}
      <td className="px-4 text-[13px] text-(--color-muted) whitespace-nowrap">
        {formatRelative(item.first_seen_at)}
      </td>
    </motion.tr>
  );
}
