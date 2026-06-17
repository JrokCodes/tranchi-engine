import { type ClassValue, clsx } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatCurrency(value: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 0,
  }).format(value);
}

export function formatDate(isoStr: string | null | undefined): string {
  if (!isoStr) return '—';
  // A date-only value ('YYYY-MM-DD', e.g. a sale_date) parses as UTC midnight, which
  // toLocaleDateString then shifts back a day in any negative-offset zone (e.g. ET) —
  // a sale_date of 2026-08-07 would render 'Aug 6'. Append local midnight so date-only
  // values are interpreted in the viewer's timezone. Full ISO timestamps (with a 'T',
  // e.g. started_at/observed_at) are left exactly as-is.
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(isoStr);
  const d = new Date(dateOnly ? isoStr + 'T00:00:00' : isoStr);
  if (isNaN(d.getTime())) return '—';
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

export function formatSaleDate(isoStr: string | null | undefined): string {
  if (!isoStr) return '—';
  // Sale dates come as YYYY-MM-DD; parse without timezone shift
  const d = new Date(isoStr + 'T00:00:00');
  if (isNaN(d.getTime())) return '—';
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

export function formatRelative(isoStr: string | null | undefined): string {
  if (!isoStr) return '—';
  const diff = Date.now() - new Date(isoStr).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return formatDate(isoStr);
}

export function formatSignalType(raw: string): string {
  return raw
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// Source → short badge label
export function sourceLabel(site: string): string {
  if (site.includes('MMLBA')) return 'MMLBA';
  if (site.includes('Land Bank')) return 'Land Bank';
  if (site.includes('Tax Sale')) return 'Tax Sale';
  if (site.includes('Foreclosure')) return 'Foreclosure';
  if (site.includes('Sheriff')) return 'Sheriff';
  if (site.includes('Probate')) return 'Probate';
  if (site.includes('Code')) return 'Code';
  if (site.includes('Fiscal') || site.includes('Parcels')) return 'Registry';
  return site;
}

// Consistent source color classes (navy, gold, slate variants)
export function sourceBadgeClass(site: string): string {
  if (site.includes('Land Bank') || site.includes('MMLBA')) return 'bg-[#1A2B4A]/10 text-[#1A2B4A]';
  if (site.includes('Tax Sale')) return 'bg-[#C9A84C]/15 text-[#8B6914]';
  if (site.includes('Foreclosure') || site.includes('Sheriff')) return 'bg-[#C9A84C]/15 text-[#8B6914]';
  if (site.includes('Probate')) return 'bg-[#5A6A7A]/15 text-[#3D4E5C]';
  if (site.includes('Code')) return 'bg-[#DC2626]/10 text-[#DC2626]';
  if (site.includes('Fiscal') || site.includes('Parcels')) return 'bg-[#16A34A]/10 text-[#15803D]';
  return 'bg-[#1A2B4A]/10 text-[#1A2B4A]';
}

// ─── Markets ────────────────────────────────────────────────────────────────
// The dashboard serves four markets. Filtering keys on property_county
// (the API _build_where does an ILIKE on l.property_county).
export interface Market { label: string; county: string; }
export const MARKETS: Market[] = [
  { label: 'Cuyahoga (OH)', county: 'Cuyahoga' },
  { label: 'Shelby–Memphis (TN)', county: 'Shelby' },
  { label: 'Summit–Akron (OH)', county: 'Summit' },
  { label: 'Wayne–Detroit (MI)', county: 'Wayne' },
];

// Scope a source_site to a market by name. Shelby/Memphis sources name the county
// or city; Summit/Akron name Summit or Akron; Wayne/Detroit name Wayne or Detroit;
// everything else is Cuyahoga. (Names stay disjoint — no Cuyahoga/Shelby/Summit source
// contains 'Wayne'/'Detroit', and vice-versa.)
export function sourceInCounty(site: string, county: string): boolean {
  const isShelby = site.includes('Shelby') || site.includes('Memphis') || site.includes('MMLBA');
  const isSummit = site.includes('Summit') || site.includes('Akron');
  const isWayne = site.includes('Wayne') || site.includes('Detroit');
  if (county === 'Shelby') return isShelby;
  if (county === 'Summit') return isSummit;
  if (county === 'Wayne') return isWayne;
  return !isShelby && !isSummit && !isWayne;  // Cuyahoga = none of the above
}

// External "verify" link per market. Shelby's universal verifier is the County Trustee
// parcel page, which ONLY accepts the native spaced PARCELID (not our 14-char canonical);
// stubs without one fall back to the Trustee address search. Cuyahoga verify links are
// not built yet (returns null → no link shown).
export function buildVerifyLink(
  county: string | null,
  nativeParcelId: string | null,
  address: string | null,
): { label: string; href: string } | null {
  if (county === 'Shelby') {
    if (nativeParcelId) {
      return {
        label: 'Verify on County Trustee',
        href: `https://apps2.shelbycountytrustee.com/Parcel?parcel=${encodeURIComponent(nativeParcelId)}`,
      };
    }
    if (address) {
      return {
        label: 'Find on County Trustee',
        href: `https://apps2.shelbycountytrustee.com/search?search=${encodeURIComponent(address)}`,
      };
    }
  }
  if (county === 'Summit') {
    // Summit's current-owner authority is the Fiscal Office property search (no stable
    // per-parcel deep-link param confirmed — opens the search page; the parcel/source
    // links from the API cover the per-listing source verification).
    return {
      label: 'Find on Summit Fiscal Office',
      href: 'https://fiscaloffice.summitoh.net/index.php/property-search',
    };
  }
  if (county === 'Wayne') {
    // Wayne/Detroit current-owner + live-delinquency authority is the Treasurer's pto
    // portal (search by parcel/address; no stable per-parcel deep-link param). NEVER use a
    // Zillow "sold" banner as a kill — pto + the Detroit assessor are the truth. The
    // per-listing source links from the API cover source verification.
    return {
      label: 'Verify on Wayne County (pto)',
      href: 'https://pto.waynecounty.com/',
    };
  }
  return null;
}
