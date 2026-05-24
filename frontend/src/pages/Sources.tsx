import { motion } from 'framer-motion';
import { SourceCard } from '../components/shared/SourceCard';
import { SourceCardSkeleton } from '../components/shared/LoadingSkeleton';
import { useSources } from '../hooks/useSources';
import { useListings } from '../hooks/useListings';
import type { ApiSource } from '../types';

// ─── Section wrapper ───────────────────────────────────────────────────────────

interface SectionProps {
  title: string;
  subtitle: React.ReactNode;
  sources: ApiSource[];
  indexOffset: number;
}

function SourceSection({ title, subtitle, sources, indexOffset }: SectionProps) {
  return (
    <div>
      <div className="mb-4">
        <h2
          className="text-[18px] font-semibold text-(--color-navy) leading-snug"
          style={{ fontFamily: 'var(--font-heading)' }}
        >
          {title}
        </h2>
        <p className="text-[13px] text-(--color-slate) mt-0.5">{subtitle}</p>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {sources.map((source, i) => (
          <SourceCard key={source.source_site} source={source} index={indexOffset + i} />
        ))}
      </div>
    </div>
  );
}

// ─── Sources page ──────────────────────────────────────────────────────────────

export default function Sources() {
  const { data, isLoading, isError } = useSources();
  const sources = data?.sources ?? [];
  const onlineCount = sources.filter((s) => s.online).length;

  // Fetch the real listings total (page_size=1 is enough — we only need `total`)
  const { data: listingsData } = useListings({}, 1);
  const listingsTotal = listingsData?.total;

  const dealSources = sources.filter((s) => s.category === 'deal');
  const enrichSources = sources.filter(
    (s) => s.category === 'signal' || s.category === 'registry'
  );

  return (
    <div>
      {/* Page header */}
      <motion.div
        initial={{ opacity: 0, y: -6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.25 }}
        className="mb-8"
      >
        <h1
          className="text-[32px] font-semibold text-(--color-navy) leading-tight"
          style={{ fontFamily: 'var(--font-heading)' }}
        >
          Sources
        </h1>
        <p className="text-[14px] text-(--color-slate) mt-1.5">
          {isLoading
            ? 'Loading scraper status...'
            : `${sources.length} scrapers · ${onlineCount} online`}
        </p>
      </motion.div>

      {/* Error state */}
      {isError && (
        <div className="mb-6 px-4 py-3 bg-[#DC2626]/5 border border-[#DC2626]/20 rounded-xl">
          <p className="text-[13px] text-(--color-danger)">
            Could not load source status — check API connection
          </p>
        </div>
      )}

      {/* Skeleton */}
      {isLoading ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {Array.from({ length: 5 }).map((_, i) => (
            <SourceCardSkeleton key={i} />
          ))}
        </div>
      ) : sources.length === 0 && !isError ? (
        <div className="py-16 text-center">
          <p className="text-[14px] text-(--color-muted)">No source data available yet</p>
        </div>
      ) : (
        <div className="flex flex-col gap-10">
          {/* Deal Sources */}
          {dealSources.length > 0 && (
            <SourceSection
              title="Deal Sources"
              subtitle={
                listingsTotal != null
                  ? `${listingsTotal.toLocaleString()} listings`
                  : 'Active deal listings across Land Bank, Sheriff Sales, and Probate Court'
              }
              sources={dealSources}
              indexOffset={0}
            />
          )}

          {/* Data & Signal Sources */}
          {enrichSources.length > 0 && (
            <SourceSection
              title="Data & Signal Sources"
              subtitle="Distress signals & parcel data that enrich and flag the listings above — not listings themselves."
              sources={enrichSources}
              indexOffset={dealSources.length}
            />
          )}
        </div>
      )}
    </div>
  );
}
