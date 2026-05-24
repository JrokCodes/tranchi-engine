export function TableRowSkeleton({ cols = 8 }: { cols?: number }) {
  return (
    <tr className="border-b border-(--color-border-subtle) h-14">
      {Array.from({ length: cols }).map((_, i) => (
        <td key={i} className="px-4">
          <div className="h-3.5 bg-(--color-bg-elevated) animate-pulse rounded w-4/5" />
        </td>
      ))}
    </tr>
  );
}

export function SourceCardSkeleton() {
  return (
    <div className="bg-(--color-bg-card) rounded-xl border border-(--color-border) p-5 shadow-sm animate-pulse">
      <div className="flex items-start justify-between mb-4">
        <div className="flex-1">
          <div className="h-4 bg-(--color-bg-elevated) rounded w-3/5 mb-1.5" />
          <div className="h-3 bg-(--color-bg-elevated) rounded w-2/5" />
        </div>
        <div className="w-14 h-4 bg-(--color-bg-elevated) rounded" />
      </div>
      <div className="flex items-center gap-4 mb-4">
        <div className="h-8 bg-(--color-bg-elevated) rounded w-12" />
        <div className="h-8 bg-(--color-bg-elevated) rounded w-12" />
        <div className="h-8 bg-(--color-bg-elevated) rounded w-12" />
      </div>
      <div className="flex gap-1.5">
        <div className="h-5 bg-(--color-bg-elevated) rounded-full w-16" />
        <div className="h-5 bg-(--color-bg-elevated) rounded-full w-20" />
      </div>
    </div>
  );
}
