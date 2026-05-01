type Props = {
  /** Tailwind size/utility classes — e.g. ``"h-4 w-32"`` or ``"h-20 w-full"``. */
  className?: string;
};

/**
 * One pulsing rectangle. Composed by the page-shaped loading states
 * (overview / table / analytics) to mirror the eventual layout, so
 * data arriving doesn't cause a jarring shift.
 */
export function Skeleton({ className = "" }: Props) {
  return (
    <div
      className={`animate-pulse rounded bg-slate-800/80 ${className}`}
      aria-hidden
    />
  );
}
