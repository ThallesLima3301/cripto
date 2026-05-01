import type { ReactNode } from "react";

type Props = {
  title: string;
  subtitle?: ReactNode;
  /** Optional content rendered on the right (filter bar, scope selector, …). */
  actions?: ReactNode;
};

/**
 * Standard page heading used by every route under `/dashboard/app/*`.
 *
 * Centralizing the heading shape (title + optional muted subtitle +
 * optional right-side action slot) means every page renders the same
 * spacing and typography without repeating the layout in five files.
 */
export function PageHeader({ title, subtitle, actions }: Props) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold text-slate-100">{title}</h1>
        {subtitle ? (
          <div className="mt-1 text-xs text-slate-500">{subtitle}</div>
        ) : null}
      </div>
      {actions ? <div className="shrink-0">{actions}</div> : null}
    </div>
  );
}
