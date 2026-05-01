import type { ReactNode } from "react";

type Props = {
  title: string;
  subtitle?: string;
  /** When `true`, render an "insufficient data" panel instead of children. */
  empty?: boolean;
  emptyMessage?: string;
  children?: ReactNode;
};

/**
 * Wrapper card used by every chart on the Analytics page.
 *
 * Consistent header / body / empty-state shape so the four bar charts
 * look like siblings instead of four bespoke layouts. The `empty`
 * branch lets the parent skip rendering the child chart entirely
 * when min_signals filtered the slicing to zero buckets.
 */
export function ChartCard({
  title,
  subtitle,
  empty,
  emptyMessage = "Not enough rows in this slice yet.",
  children,
}: Props) {
  return (
    <section className="rounded-md border border-slate-800 bg-slate-900 p-4">
      <header className="mb-3">
        <h3 className="text-sm font-medium text-slate-200">{title}</h3>
        {subtitle ? (
          <p className="text-xs text-slate-500">{subtitle}</p>
        ) : null}
      </header>
      {empty ? (
        <div className="flex h-48 items-center justify-center text-xs text-slate-500">
          {emptyMessage}
        </div>
      ) : (
        <div className="h-64">{children}</div>
      )}
    </section>
  );
}
