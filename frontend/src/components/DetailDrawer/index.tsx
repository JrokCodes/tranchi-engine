import { useEffect, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { X, AlertTriangle, Flame, MapPin, ExternalLink } from 'lucide-react';
import { useListing } from '../../hooks/useListings';
import { StatusBadge } from '../shared/StatusBadge';
import {
  formatCurrency,
  formatDate,
  formatRelative,
  formatSignalType,
  sourceBadgeClass,
  sourceLabel,
  buildVerifyLink,
} from '../../lib/utils';
import { cn } from '../../lib/utils';

interface Props {
  listingId: string | null;
  onClose: () => void;
}

// ─── Body scroll lock ──────────────────────────────────────────────────────────
function useScrollLock(locked: boolean) {
  useEffect(() => {
    if (!locked) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = prev; };
  }, [locked]);
}

// ─── Section label helper ──────────────────────────────────────────────────────
function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-[10px] uppercase tracking-[0.14em] text-(--color-muted) font-semibold mb-2.5">
      {children}
    </p>
  );
}

// ─── Street View hero ───────────────────────────────────────────────────────
// 16:9 property image at the top of the drawer. Falls back to a MapPin tile when
// there's no Maps key or Google has no Street View coverage for the address.
function StreetViewHero({ url, address }: { url: string; address: string }) {
  const [imgError, setImgError] = useState(false);
  if (imgError) {
    return (
      <div className="rounded-xl overflow-hidden aspect-[16/9] bg-(--color-bg-elevated) flex items-center justify-center">
        <MapPin size={32} className="text-(--color-muted)" />
      </div>
    );
  }
  return (
    <div className="rounded-xl overflow-hidden aspect-[16/9] bg-(--color-bg-elevated)">
      <img
        src={url}
        alt={`Street view of ${address}`}
        className="w-full h-full object-cover"
        onError={() => setImgError(true)}
      />
    </div>
  );
}

// ─── KV row ───────────────────────────────────────────────────────────────────
function KvRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <p className="text-[11px] text-(--color-muted) mb-0.5">{label}</p>
      <p className="text-[13px] text-(--color-ink) font-medium">{value}</p>
    </div>
  );
}

// ─── Skeleton ─────────────────────────────────────────────────────────────────
function DrawerSkeleton() {
  return (
    <div className="flex-1 overflow-y-auto px-6 py-5 flex flex-col gap-5">
      {Array.from({ length: 5 }).map((_, i) => (
        <div key={i} className="h-20 bg-(--color-bg-subtle) animate-pulse rounded-xl" />
      ))}
    </div>
  );
}

// ─── Drawer content ───────────────────────────────────────────────────────────
function DrawerContent({ listingId, onClose }: { listingId: string; onClose: () => void }) {
  const { data, isLoading, isError } = useListing(listingId);

  if (isLoading) return <DrawerSkeleton />;
  if (isError || !data) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center px-6 py-10 gap-3">
        <AlertTriangle size={28} className="text-(--color-muted)" />
        <p className="text-[14px] text-(--color-slate)">Failed to load listing</p>
        <button
          onClick={onClose}
          className="text-[13px] text-(--color-navy) underline underline-offset-2"
        >
          Close
        </button>
      </div>
    );
  }

  const { listing, parcel, signals } = data;

  return (
    <div
      className="flex-1 overflow-y-auto px-6 py-5 flex flex-col gap-5"
      style={{ animation: 'drawerFadeIn 0.15s ease-out' }}
    >
      {/* Close + header */}
      <div>
        <button
          onClick={onClose}
          className="flex items-center gap-1.5 text-[13px] text-(--color-slate) hover:text-(--color-ink) transition-colors mb-3"
          aria-label="Close detail drawer"
        >
          <X size={14} />
          Close
        </button>

        {/* Street View hero */}
        {listing.street_view_url && (
          <div className="mb-4">
            <StreetViewHero url={listing.street_view_url} address={listing.property_address} />
          </div>
        )}

        <div className="flex items-start gap-3">
          <div className="flex-1 min-w-0">
            <h2
              className="text-[20px] font-semibold text-(--color-navy) leading-tight"
              style={{ fontFamily: 'var(--font-heading)' }}
            >
              {listing.property_address}
            </h2>
            <p className="text-[13px] text-(--color-slate) mt-1">
              {listing.property_city}, {listing.property_state} {listing.property_zip} &mdash; {listing.property_county} County
            </p>
          </div>
          <div className="flex flex-col items-end gap-1.5 flex-shrink-0">
            <StatusBadge status={listing.status} />
            {listing.is_hot && (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-(--color-gold-light) text-[#8B6914] rounded-full text-[11px] font-medium border border-(--color-gold)/25">
                <Flame size={10} />
                HOT
              </span>
            )}
          </div>
        </div>

        {/* Source badge */}
        <span
          className={cn(
            'inline-flex items-center mt-2 px-2 py-0.5 rounded-full text-[11px] font-medium',
            sourceBadgeClass(listing.source_site)
          )}
        >
          {listing.source_site}
        </span>
      </div>

      {/* Signals summary */}
      <div className="bg-(--color-bg-subtle) rounded-xl p-4 border border-(--color-border)">
        <div className="flex items-center justify-between mb-2.5">
          <SectionLabel>Signals</SectionLabel>
          {listing.is_hot && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 bg-(--color-gold-light) text-[#8B6914] rounded-full text-[11px] font-medium border border-(--color-gold)/25">
              <Flame size={10} />
              HOT
            </span>
          )}
        </div>
        {listing.signal_types.length === 0 ? (
          <p className="text-[13px] text-(--color-muted)">No signals detected</p>
        ) : (
          <div className="flex items-start gap-3 flex-wrap">
            <span
              className="inline-flex items-center justify-center w-10 h-10 rounded-full bg-(--color-navy) text-white text-[18px] font-bold flex-shrink-0"
              style={{ fontFamily: 'var(--font-heading)' }}
            >
              {listing.signal_count}
            </span>
            <div className="flex flex-wrap gap-1.5">
              {listing.signal_types.map((st) => (
                <span
                  key={st.label}
                  className="inline-flex items-center gap-0.5 px-2.5 py-1 bg-white rounded-full text-[12px] text-(--color-ink) border border-(--color-border) font-medium"
                >
                  {st.label}
                  {st.count > 1 && (
                    <span className="text-(--color-slate) font-normal"> ×{st.count}</span>
                  )}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Probate case (probate listings only) — decedent vs current owner is the verification value */}
      {listing.decedent_name && (
        <div>
          <SectionLabel>Probate Case</SectionLabel>
          <div className="bg-white rounded-xl border border-(--color-border) p-4 grid grid-cols-2 gap-x-6 gap-y-3">
            <KvRow label="Decedent" value={listing.decedent_name} />
            {listing.case_title && <KvRow label="Case Title" value={listing.case_title} />}
            {listing.case_status && <KvRow label="Case Status" value={listing.case_status} />}
            {listing.match_confidence && (
              <KvRow
                label="Match"
                value={
                  <span
                    className={cn(
                      'inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium capitalize',
                      listing.match_confidence === 'confirmed'
                        ? 'bg-(--color-success)/10 text-(--color-success)'
                        : 'bg-(--color-gold-light) text-[#8B6914] border border-(--color-gold)/25'
                    )}
                  >
                    {listing.match_confidence}
                  </span>
                }
              />
            )}
          </div>
          <p className="text-[11px] text-(--color-muted) mt-1.5">
            Confirm the decedent matches the current registry owner below.
          </p>
        </div>
      )}

      {/* TN tax-deed redemption (tax_deed only; shows once a sale is confirmed — dormant pre-sale) */}
      {listing.signal_type === 'tax_deed' &&
        (listing.redemption_status || listing.redemption_ends) && (
          <div>
            <SectionLabel>Redemption (TN redeemable tax deed)</SectionLabel>
            <div className="bg-white rounded-xl border border-(--color-border) p-4 grid grid-cols-2 gap-x-6 gap-y-3">
              {listing.redemption_status && (
                <KvRow
                  label="Status"
                  value={
                    <span
                      className={cn(
                        'inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium capitalize',
                        listing.redemption_status === 'pending'
                          ? 'bg-(--color-gold-light) text-[#8B6914] border border-(--color-gold)/25'
                          : 'bg-(--color-bg-elevated) text-(--color-slate)'
                      )}
                    >
                      {listing.redemption_status === 'pending' ? 'Redeemable (speculative)' : listing.redemption_status}
                    </span>
                  }
                />
              )}
              {listing.confirmation_order_date && (
                <KvRow label="Sale Confirmed" value={formatDate(listing.confirmation_order_date)} />
              )}
              {listing.redemption_ends && (
                <KvRow label="Redeemable Until" value={formatDate(listing.redemption_ends)} />
              )}
              <KvRow label="Statutory Interest" value="up to 12%" />
            </div>
          </div>
        )}

      {/* Parcel section */}
      <div>
        <SectionLabel>Parcel</SectionLabel>
        {parcel ? (
          <div className="bg-white rounded-xl border border-(--color-border) p-4 grid grid-cols-2 gap-x-6 gap-y-3">
            {parcel.owner_name && (
              <KvRow label="Owner" value={parcel.owner_name} />
            )}
            {parcel.situs_address && (
              <KvRow label="Situs Address" value={parcel.situs_address} />
            )}
            {parcel.owner_mailing_address && (
              <KvRow label="Mailing Address" value={parcel.owner_mailing_address} />
            )}
            {parcel.parcel_number && (
              <KvRow label="Parcel Number" value={parcel.parcel_number} />
            )}
            {parcel.current_market_value != null ? (
              <KvRow label="Market Value" value={formatCurrency(parcel.current_market_value)} />
            ) : (
              <KvRow label="Market Value" value={<span className="text-(--color-muted) font-normal">Not available</span>} />
            )}
            {parcel.current_tax_balance != null ? (
              <KvRow label="Tax Balance" value={formatCurrency(parcel.current_tax_balance)} />
            ) : (
              <KvRow label="Tax Balance" value={<span className="text-(--color-muted) font-normal">Not available</span>} />
            )}
            <KvRow
              label="Delinquent"
              value={
                parcel.delinquent_flag ? (
                  <span className="text-(--color-danger) font-semibold">Yes</span>
                ) : (
                  <span className="text-(--color-success)">No</span>
                )
              }
            />
            {parcel.year_built != null && <KvRow label="Year Built" value={String(parcel.year_built)} />}
            {parcel.sq_ft != null && <KvRow label="Sq Ft" value={parcel.sq_ft.toLocaleString()} />}
            {parcel.beds != null && <KvRow label="Beds / Baths" value={`${parcel.beds} / ${parcel.baths ?? '?'}`} />}
            {parcel.last_sale_date && (
              <KvRow
                label="Last Sale"
                value={`${formatDate(parcel.last_sale_date)}${parcel.last_sale_price ? ` · ${formatCurrency(parcel.last_sale_price)}` : ''}`}
              />
            )}
          </div>
        ) : (
          <div className="bg-(--color-bg-subtle) rounded-xl border border-(--color-border) px-4 py-5 text-center">
            <p className="text-[13px] text-(--color-muted)">Not enriched yet</p>
            <p className="text-[12px] text-(--color-muted) opacity-70 mt-1">Parcel data will appear once the enrichment job runs</p>
          </div>
        )}
      </div>

      {/* Listing details */}
      <div>
        <SectionLabel>Listing Details</SectionLabel>
        <div className="bg-white rounded-xl border border-(--color-border) p-4 grid grid-cols-2 gap-x-6 gap-y-3">
          <KvRow label="Source" value={sourceLabel(listing.source_site)} />
          {listing.signal_type && <KvRow label="Signal Type" value={formatSignalType(listing.signal_type)} />}
          {listing.case_number && <KvRow label="Case #" value={listing.case_number} />}
          {listing.trustee_name && <KvRow label="Trustee" value={listing.trustee_name} />}
          {listing.sale_date && <KvRow label="Sale Date" value={formatDate(listing.sale_date)} />}
          {listing.sec_sale_date && <KvRow label="Re-offer Date" value={formatDate(listing.sec_sale_date)} />}
          {listing.opening_bid_usd != null && <KvRow label="Opening Bid" value={formatCurrency(listing.opening_bid_usd)} />}
          {listing.appraised_value_usd != null && <KvRow label="Appraised Value" value={formatCurrency(listing.appraised_value_usd)} />}
          {listing.deposit_usd != null && <KvRow label="Deposit" value={formatCurrency(listing.deposit_usd)} />}
          {listing.auction_status && <KvRow label="Auction Status" value={listing.auction_status} />}
          {listing.source_listing_id && <KvRow label="Source ID" value={listing.source_listing_id} />}
          <KvRow label="First Seen" value={formatRelative(listing.first_seen_at)} />
          <KvRow label="Last Seen" value={formatRelative(listing.last_seen_at)} />
        </div>
      </div>

      {/* Verify — external one-click confirmation on the authoritative county source */}
      {(() => {
        const link = buildVerifyLink(
          listing.property_county,
          parcel?.native_parcel_id ?? null,
          listing.property_address,
        );
        if (!link) return null;
        return (
          <div>
            <SectionLabel>Verify</SectionLabel>
            <a
              href={link.href}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 px-3 py-2 bg-(--color-navy)/5 text-(--color-navy) rounded-lg border border-(--color-navy)/15 hover:bg-(--color-navy)/10 transition-colors text-[13px] font-medium"
            >
              <ExternalLink size={14} />
              {link.label}
            </a>
          </div>
        );
      })()}

      {/* Signals detail list */}
      {signals.length > 0 && (
        <div>
          <SectionLabel>Signal Detail</SectionLabel>
          <div className="flex flex-col gap-2">
            {signals.map((sig, i) => (
              <div key={i} className="bg-white rounded-xl border border-(--color-border) px-4 py-3">
                <div className="flex items-start justify-between">
                  <div>
                    <p className="text-[13px] font-semibold text-(--color-ink)">
                      {formatSignalType(sig.signal_type)}
                    </p>
                    <p className="text-[11px] text-(--color-slate) mt-0.5">{sig.source}</p>
                  </div>
                  <span className="text-[11px] text-(--color-muted)">
                    {formatDate(sig.observed_at)}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Public export ─────────────────────────────────────────────────────────────
export function DetailDrawer({ listingId, onClose }: Props) {
  useScrollLock(!!listingId);

  return (
    <AnimatePresence>
      {listingId && (
        <>
          <motion.div
            key="backdrop"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="fixed inset-0 bg-black/25 z-40"
            onClick={onClose}
          />

          <motion.div
            key="drawer"
            initial={{ x: '100%' }}
            animate={{ x: 0 }}
            exit={{ x: '100%' }}
            transition={{ duration: 0.22, ease: [0.32, 0.72, 0, 1] }}
            style={{ willChange: 'transform' }}
            className="fixed right-0 top-0 h-full w-full md:w-[520px] bg-(--color-bg-base) border-l border-(--color-border) z-50 flex flex-col shadow-2xl"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-label="Listing detail"
          >
            <DrawerContent listingId={listingId} onClose={onClose} />
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
