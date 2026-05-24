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
  if (site.includes('Land Bank')) return 'Land Bank';
  if (site.includes('Sheriff')) return 'Sheriff';
  if (site.includes('Probate')) return 'Probate';
  if (site.includes('Code')) return 'Code';
  if (site.includes('Fiscal')) return 'Fiscal';
  return site;
}

// Consistent source color classes (navy, gold, slate variants)
export function sourceBadgeClass(site: string): string {
  if (site.includes('Land Bank')) return 'bg-[#1A2B4A]/10 text-[#1A2B4A]';
  if (site.includes('Sheriff')) return 'bg-[#C9A84C]/15 text-[#8B6914]';
  if (site.includes('Probate')) return 'bg-[#5A6A7A]/15 text-[#3D4E5C]';
  if (site.includes('Code')) return 'bg-[#DC2626]/10 text-[#DC2626]';
  if (site.includes('Fiscal')) return 'bg-[#16A34A]/10 text-[#15803D]';
  return 'bg-[#1A2B4A]/10 text-[#1A2B4A]';
}
