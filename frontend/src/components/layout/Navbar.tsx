import { useState } from 'react';
import { NavLink } from 'react-router-dom';
import { motion } from 'framer-motion';
import { Clock } from 'lucide-react';
import { cn } from '../../lib/utils';
import { useSources } from '../../hooks/useSources';
import { formatRelative } from '../../lib/utils';

const NAV_LINKS = [
  { to: '/', label: 'Listings', end: true },
  { to: '/sources', label: 'Sources', end: false },
];

function LastScraped() {
  const { data } = useSources();
  if (!data?.sources.length) return null;

  // Most recent started_at across all sources
  const latest = data.sources.reduce((best, s) =>
    !best || new Date(s.started_at) > new Date(best) ? s.started_at : best,
    '' as string
  );
  if (!latest) return null;

  return (
    <div className="flex items-center gap-1.5 text-[12px] text-(--color-slate)">
      <Clock size={12} className="opacity-60" />
      <span>Last scraped {formatRelative(latest)}</span>
    </div>
  );
}

export default function Navbar() {
  const [logoError, setLogoError] = useState(false);

  return (
    <nav
      className="fixed top-0 left-0 right-0 z-40 h-14 bg-(--color-bg-card) border-b border-(--color-border)"
      role="navigation"
      aria-label="Main navigation"
    >
      <div className="max-w-[1400px] mx-auto h-full px-6 lg:px-10 flex items-center justify-between">
        {/* Logo + wordmark */}
        <NavLink to="/" className="flex items-center gap-2.5 select-none">
          {!logoError && (
            <img
              src="/logo.png"
              alt=""
              className="h-8 w-auto"
              onError={() => setLogoError(true)}
              aria-hidden="true"
            />
          )}
          <span
            className="font-[family-name:var(--font-heading)] font-semibold text-[17px] tracking-[-0.01em] text-(--color-navy)"
            style={{ fontFamily: 'var(--font-heading)' }}
          >
            TRANCHI
          </span>
        </NavLink>

        {/* Nav links */}
        <div className="flex items-center gap-1">
          {NAV_LINKS.map(({ to, label, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  'relative px-4 py-1.5 text-[13px] font-medium transition-colors duration-150',
                  'font-[family-name:var(--font-body)]',
                  isActive
                    ? 'text-(--color-navy)'
                    : 'text-(--color-slate) hover:text-(--color-ink)'
                )
              }
            >
              {({ isActive }) => (
                <>
                  {label}
                  {isActive && (
                    <motion.div
                      layoutId="nav-underline"
                      className="absolute bottom-0 left-4 right-4 h-0.5 bg-(--color-navy) rounded-full"
                      transition={{ type: 'spring', stiffness: 500, damping: 40 }}
                    />
                  )}
                </>
              )}
            </NavLink>
          ))}
        </div>

        {/* Last scraped indicator */}
        <LastScraped />
      </div>
    </nav>
  );
}
