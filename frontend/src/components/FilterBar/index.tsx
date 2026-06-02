import { useRef, useState, useEffect } from 'react';
import { Search, ChevronDown, X } from 'lucide-react';
import { cn, MARKETS, sourceInCounty } from '../../lib/utils';
import { useSources } from '../../hooks/useSources';

export interface FilterState {
  // county = the active MARKET (ILIKE on property_county). '' = all markets.
  county: string;
  source_site: string;
  status: string;
  has_signals: boolean;
  q: string;
  sort: string;
  order: 'asc' | 'desc';
}

export const defaultFilters: FilterState = {
  county: '',
  source_site: '',
  // Default to active deals so the past/expired sheriff archive doesn't clutter
  // the buyable-listings view. Users can still pick Expired/Cancelled explicitly.
  status: 'active',
  has_signals: false,
  q: '',
  sort: 'first_seen_at',
  order: 'desc',
};

const STATUS_OPTIONS: { label: string; value: string }[] = [
  { label: 'Active', value: 'active' },
  { label: 'Not Listed', value: 'not_listed' },
  { label: 'Expired', value: 'expired' },
  { label: 'Cancelled', value: 'cancelled' },
];

interface SelectProps {
  label: string;
  value: string;
  options: { label: string; value: string }[];
  onChange: (v: string) => void;
  placeholder?: string;
}

function SimpleSelect({ label, value, options, onChange, placeholder = 'All' }: SelectProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handleOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener('mousedown', handleOutside);
    return () => document.removeEventListener('mousedown', handleOutside);
  }, [open]);

  const selected = options.find((o) => o.value === value);

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className={cn(
          'h-9 px-3 pr-2 text-[13px] flex items-center gap-1.5 rounded-full border transition-colors whitespace-nowrap',
          'focus:outline-none',
          value
            ? 'bg-(--color-navy) text-white border-(--color-navy)'
            : 'bg-white text-(--color-slate) border-(--color-border) hover:border-(--color-navy)/30 hover:text-(--color-ink)'
        )}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={label}
      >
        <span>{value ? selected?.label ?? label : label}</span>
        {value ? (
          <span
            role="button"
            tabIndex={0}
            onClick={(e) => { e.stopPropagation(); onChange(''); }}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.stopPropagation(); onChange(''); } }}
            className="opacity-70 hover:opacity-100 cursor-pointer ml-0.5"
            aria-label={`Clear ${label} filter`}
          >
            <X size={11} />
          </span>
        ) : (
          <ChevronDown size={12} className={cn('transition-transform', open && 'rotate-180')} />
        )}
      </button>

      {open && (
        <div
          role="listbox"
          className="absolute top-full left-0 mt-1.5 min-w-[180px] bg-white border border-(--color-border) rounded-xl shadow-lg z-50 py-1.5 overflow-hidden"
        >
          <button
            type="button"
            role="option"
            aria-selected={value === ''}
            onClick={() => { onChange(''); setOpen(false); }}
            className={cn(
              'w-full text-left px-3 py-2 text-[13px] transition-colors',
              value === ''
                ? 'text-(--color-navy) font-medium bg-(--color-bg-subtle)'
                : 'text-(--color-slate) hover:bg-(--color-bg-subtle) hover:text-(--color-ink)'
            )}
          >
            {placeholder}
          </button>
          {options.map((opt) => (
            <button
              key={opt.value}
              type="button"
              role="option"
              aria-selected={value === opt.value}
              onClick={() => { onChange(opt.value); setOpen(false); }}
              className={cn(
                'w-full text-left px-3 py-2 text-[13px] transition-colors',
                value === opt.value
                  ? 'text-(--color-navy) font-medium bg-(--color-bg-subtle)'
                  : 'text-(--color-slate) hover:bg-(--color-bg-subtle) hover:text-(--color-ink)'
              )}
            >
              {opt.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

interface Props {
  filters: FilterState;
  onChange: (f: FilterState) => void;
  onClear: () => void;
}

export function FilterBar({ filters, onChange, onClear }: Props) {
  const { data: sourcesData } = useSources();

  // Source dropdown is derived from /api/v1/sources (deal sources only), scoped to the
  // selected market — so Shelby sources appear automatically once the backend reports
  // them, no hardcoding. When no market is picked, all deal sources show.
  const dealSources = (sourcesData?.sources ?? [])
    .filter((s) => s.category === 'deal')
    .map((s) => s.source_site);
  const sourceOptions = (filters.county
    ? dealSources.filter((s) => sourceInCounty(s, filters.county))
    : dealSources
  ).sort();

  // Switching market clears a source that doesn't belong to the new market.
  function handleMarketChange(county: string) {
    const keepSource =
      !filters.source_site || !county || sourceInCounty(filters.source_site, county);
    onChange({ ...filters, county, source_site: keepSource ? filters.source_site : '' });
  }

  const isFiltered =
    !!filters.county ||
    !!filters.source_site ||
    !!filters.status ||
    filters.has_signals ||
    !!filters.q;

  return (
    <div className="flex flex-wrap items-center gap-2">
      {/* Address search */}
      <div className="relative">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-(--color-muted) pointer-events-none" />
        <input
          type="text"
          value={filters.q}
          onChange={(e) => onChange({ ...filters, q: e.target.value })}
          placeholder="Search address..."
          className="h-9 pl-8 pr-3 text-[13px] rounded-full border border-(--color-border) bg-white text-(--color-ink) placeholder:text-(--color-muted) focus:outline-none focus:border-(--color-navy)/40 transition-colors w-52"
        />
      </div>

      {/* Market selector (Cuyahoga | Shelby–Memphis) */}
      <SimpleSelect
        label="Market"
        value={filters.county}
        options={MARKETS.map((m) => ({ label: m.label, value: m.county }))}
        onChange={handleMarketChange}
        placeholder="All markets"
      />

      {/* Source dropdown (dynamic, market-scoped) */}
      <SimpleSelect
        label="Source"
        value={filters.source_site}
        options={sourceOptions.map((s) => ({ label: s, value: s }))}
        onChange={(v) => onChange({ ...filters, source_site: v })}
      />

      {/* Status dropdown */}
      <SimpleSelect
        label="Status"
        value={filters.status}
        options={STATUS_OPTIONS}
        onChange={(v) => onChange({ ...filters, status: v })}
      />

      {/* Has signals toggle */}
      <button
        type="button"
        onClick={() => onChange({ ...filters, has_signals: !filters.has_signals })}
        aria-pressed={filters.has_signals}
        className={cn(
          'h-9 px-3 text-[13px] rounded-full border transition-colors whitespace-nowrap font-medium',
          filters.has_signals
            ? 'bg-(--color-gold) text-white border-(--color-gold)'
            : 'bg-white text-(--color-slate) border-(--color-border) hover:border-(--color-gold)/50 hover:text-(--color-ink)'
        )}
      >
        Has signals
      </button>

      {/* Clear */}
      {isFiltered && (
        <button
          type="button"
          onClick={onClear}
          className="h-9 px-3 text-[13px] text-(--color-slate) hover:text-(--color-ink) transition-colors flex items-center gap-1"
        >
          <X size={12} />
          Clear
        </button>
      )}
    </div>
  );
}
