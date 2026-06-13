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
  const d = new Date(isoStr);
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
// The dashboard serves three markets. Filtering keys on property_county
// (the API _build_where does an ILIKE on l.property_county).
export interface Market { label: string; county: string; }
export const MARKETS: Market[] = [
  { label: 'Cuyahoga (OH)', county: 'Cuyahoga' },
  { label: 'Shelby–Memphis (TN)', county: 'Shelby' },
  { label: 'Summit–Akron (OH)', county: 'Summit' },
];

// Scope a source_site to a market by name. Shelby/Memphis sources name the county
// or city; Summit/Akron sources name Summit or Akron; everything else is Cuyahoga.
// (No Cuyahoga or Shelby source name contains 'Summit'/'Akron', so this stays disjoint.)
export function sourceInCounty(site: string, county: string): boolean {
  const isShelby = site.includes('Shelby') || site.includes('Memphis') || site.includes('MMLBA');
  const isSummit = site.includes('Summit') || site.includes('Akron');
  if (county === 'Shelby') return isShelby;
  if (county === 'Summit') return isSummit;
  return !isShelby && !isSummit;  // Cuyahoga = neither
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
  return null;
}
