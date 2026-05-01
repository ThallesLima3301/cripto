"use client";

import type { PageMeta } from "@/lib/types";

type Props = {
  meta: PageMeta;
  onPage: (offset: number) => void;
};

/**
 * Prev / Next pagination strip driven by the API's PageMeta block.
 *
 * Stays read-only on the URL: the parent component is responsible for
 * pushing the new offset into the URL search params (so the page is
 * deep-linkable).
 */
export function Pagination({ meta, onPage }: Props) {
  const { total, limit, offset, next_offset } = meta;
  const page = Math.floor(offset / limit) + 1;
  const totalPages = Math.max(1, Math.ceil(total / Math.max(1, limit)));
  const prevOffset = Math.max(0, offset - limit);
  const isFirst = offset === 0;
  const isLast = next_offset === null;

  return (
    <div className="flex items-center justify-between gap-4 text-xs text-slate-400">
      <span>
        {total} {total === 1 ? "row" : "rows"} · page {page} / {totalPages}
      </span>
      <div className="flex gap-2">
        <button
          type="button"
          disabled={isFirst}
          onClick={() => onPage(prevOffset)}
          className="rounded border border-slate-700 bg-slate-800 px-3 py-1 text-slate-200 enabled:hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
        >
          ‹ Prev
        </button>
        <button
          type="button"
          disabled={isLast}
          onClick={() => next_offset !== null && onPage(next_offset)}
          className="rounded border border-slate-700 bg-slate-800 px-3 py-1 text-slate-200 enabled:hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Next ›
        </button>
      </div>
    </div>
  );
}
