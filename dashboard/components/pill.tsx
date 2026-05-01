import type { ReactNode } from "react";

type Props = {
  className?: string;
  title?: string;
  children: ReactNode;
};

/**
 * Tiny status pill used by every "categorical badge" on the dashboard
 * (severity, regime, watchlist status, sell rule, verdict).
 *
 * The caller passes the color classes via `className`; pill shape +
 * size are fixed here so all variants line up visually.
 */
export function Pill({ className = "", title, children }: Props) {
  return (
    <span
      title={title}
      className={`inline-block rounded border px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide ${className}`}
    >
      {children}
    </span>
  );
}
