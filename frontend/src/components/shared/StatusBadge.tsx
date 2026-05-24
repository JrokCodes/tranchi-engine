import { cn } from '../../lib/utils';
import type { ListingStatus } from '../../types';

const CONFIG: Record<ListingStatus, { label: string; className: string }> = {
  active: {
    label: 'Active',
    className: 'bg-[#16A34A]/10 text-[#15803D] border border-[#16A34A]/20',
  },
  not_listed: {
    label: 'Delisted',
    className: 'bg-(--color-bg-subtle) text-(--color-slate) border border-(--color-border)',
  },
  expired: {
    label: 'Expired',
    className: 'bg-(--color-bg-subtle) text-(--color-muted) border border-(--color-border)',
  },
  cancelled: {
    label: 'Cancelled',
    className: 'bg-[#DC2626]/10 text-[#DC2626] border border-[#DC2626]/20',
  },
};

interface Props {
  status: ListingStatus;
}

export function StatusBadge({ status }: Props) {
  const { label, className } = CONFIG[status] ?? CONFIG.active;
  return (
    <span
      className={cn(
        'inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium whitespace-nowrap',
        className
      )}
    >
      {label}
    </span>
  );
}
