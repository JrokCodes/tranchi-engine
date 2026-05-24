import { motion } from 'framer-motion';
import { ArrowRight, ExternalLink } from 'lucide-react';
import { cn } from '../../lib/utils';
import type { ApiSource } from '../../types';

function minutesToLabel(mins: number): string {
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

// Label for the "active" count varies by source category
function activeLabel(category: ApiSource['category']): string {
  if (category === 'signal') return 'Signals';
  if (category === 'registry') return 'Parcels';
  return 'Active';
}

interface Props {
  source: ApiSource;
  index: number;
}

export function SourceCard({ source, index }: Props) {
  const isOnline = source.online;
  const activeStatLabel = activeLabel(source.category);

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.06, duration: 0.3 }}
      className="bg-(--color-bg-card) rounded-xl border border-(--color-border) p-5 shadow-sm hover:shadow-md transition-shadow"
    >
      {/* Header */}
      <div className="flex items-start justify-between mb-4">
        <div className="flex-1 min-w-0 pr-3">
          <h3
            className="text-[15px] font-semibold text-(--color-navy) leading-snug"
            style={{ fontFamily: 'var(--font-heading)' }}
          >
            {source.source_site}
          </h3>
          <div className="flex items-center gap-2 mt-0.5 flex-wrap">
            <p className="text-[12px] text-(--color-slate)">
              Updated {minutesToLabel(source.minutes_since)}
            </p>
            {source.source_url && (
              <a
                href={source.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-0.5 text-[11px] text-(--color-slate) hover:text-(--color-navy) transition-colors"
                aria-label={`Visit ${source.source_site}`}
                onClick={(e) => e.stopPropagation()}
              >
                <ExternalLink size={10} />
                View source
              </a>
            )}
          </div>
        </div>

        {/* Online dot */}
        <div className="flex items-center gap-1.5 flex-shrink-0 mt-0.5">
          <span
            className={cn(
              'w-2 h-2 rounded-full flex-shrink-0',
              isOnline ? 'bg-(--color-success) shadow-[0_0_6px_rgba(22,163,74,0.5)]' : 'bg-(--color-muted)'
            )}
            aria-label={isOnline ? 'Online' : 'Offline'}
          />
          <span
            className={cn(
              'text-[11px] font-medium',
              isOnline ? 'text-(--color-success)' : 'text-(--color-muted)'
            )}
          >
            {isOnline ? 'Online' : 'Offline'}
          </span>
        </div>
      </div>

      {/* Found → Passed → Active flow */}
      <div className="flex items-center gap-2 mb-4">
        <div className="text-center">
          <p className="text-[22px] font-bold text-(--color-ink) leading-none" style={{ fontFamily: 'var(--font-heading)' }}>
            {source.found.toLocaleString()}
          </p>
          <p className="text-[10px] text-(--color-muted) uppercase tracking-wide mt-0.5">Found</p>
        </div>

        <ArrowRight size={14} className="text-(--color-muted) flex-shrink-0" />

        <div className="text-center">
          <p className="text-[22px] font-bold text-(--color-ink) leading-none" style={{ fontFamily: 'var(--font-heading)' }}>
            {source.passed.toLocaleString()}
          </p>
          <p className="text-[10px] text-(--color-muted) uppercase tracking-wide mt-0.5">Passed</p>
        </div>

        <ArrowRight size={14} className="text-(--color-muted) flex-shrink-0" />

        <div className="text-center">
          <p className="text-[22px] font-bold text-(--color-navy) leading-none" style={{ fontFamily: 'var(--font-heading)' }}>
            {source.active.toLocaleString()}
          </p>
          <p className="text-[10px] text-(--color-muted) uppercase tracking-wide mt-0.5">{activeStatLabel}</p>
        </div>
      </div>

      {/* Breakdown chips */}
      <div className="flex flex-wrap gap-1.5">
        {source.filtered > 0 && (
          <Chip label="filtered" value={source.filtered} />
        )}
        {source.dupes > 0 && (
          <Chip label="dupes" value={source.dupes} />
        )}
        {source.delisted > 0 && (
          <Chip label="delisted" value={source.delisted} />
        )}
        {source.expired > 0 && (
          <Chip label="expired" value={source.expired} />
        )}
        <Chip
          label="new today"
          value={source.new_today}
          highlight={source.new_today > 0}
        />
      </div>

      {/* Error message */}
      {source.error_message && (
        <p className="mt-3 text-[11px] text-(--color-danger) bg-[#DC2626]/5 rounded-md px-2.5 py-1.5 border border-[#DC2626]/15">
          {source.error_message}
        </p>
      )}
    </motion.div>
  );
}

function Chip({ label, value, highlight = false }: { label: string; value: number; highlight?: boolean }) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium border',
        highlight && value > 0
          ? 'bg-(--color-gold-light) text-[#8B6914] border-(--color-gold)/30'
          : 'bg-(--color-bg-subtle) text-(--color-slate) border-(--color-border)'
      )}
    >
      <span className="font-bold">{value}</span>
      <span className="font-normal opacity-80">{label}</span>
    </span>
  );
}
