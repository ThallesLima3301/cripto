type Props = {
  title?: string;
  message: string;
};

/**
 * Generic "no rows yet" panel for non-table empty states.
 *
 * Tables embed their own EmptyRow inside the table body; this is for
 * pages whose entire view collapses when empty (e.g. `/reports` with
 * no weekly summaries yet).
 */
export function EmptyState({ title, message }: Props) {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900 p-6 text-center text-sm text-slate-400">
      {title ? (
        <div className="mb-1 text-base font-medium text-slate-200">{title}</div>
      ) : null}
      <div>{message}</div>
    </div>
  );
}
