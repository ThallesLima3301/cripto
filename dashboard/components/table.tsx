// Lightweight table primitives.
//
// Each per-page table is a thin wrapper over <Table>, <Th>, <Td>. We
// avoid a generic "DataTable" framework — the four pages need
// different columns, different cell renderers, and different links,
// so the abstraction would be more code than just writing them out.

import type { ReactNode } from "react";

export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-md border border-slate-800 bg-slate-900">
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  );
}

export function Thead({ children }: { children: ReactNode }) {
  return <thead className="bg-slate-800/40">{children}</thead>;
}

export function Tbody({ children }: { children: ReactNode }) {
  return <tbody className="divide-y divide-slate-800">{children}</tbody>;
}

type CellProps = {
  children?: ReactNode;
  className?: string;
  colSpan?: number;
  /** Native HTML title (tooltip) — used by sell-signals to surface the
   *  full reason text on hover when truncated. */
  title?: string;
};

export function Th({ children, className = "", title }: CellProps) {
  return (
    <th
      className={`px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-slate-400 ${className}`}
      title={title}
    >
      {children}
    </th>
  );
}

export function Td({ children, className = "", colSpan, title }: CellProps) {
  return (
    <td
      className={`px-3 py-2 text-slate-200 ${className}`}
      colSpan={colSpan}
      title={title}
    >
      {children}
    </td>
  );
}

/** Display when a table has no rows — keeps column alignment intact. */
export function EmptyRow({
  colSpan,
  message,
}: {
  colSpan: number;
  message: string;
}) {
  return (
    <tr>
      <td
        colSpan={colSpan}
        className="px-3 py-6 text-center text-sm text-slate-400"
      >
        {message}
      </td>
    </tr>
  );
}
