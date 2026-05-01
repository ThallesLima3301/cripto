import Link from "next/link";

/**
 * Custom 404 page — matches the dashboard's dark-card aesthetic
 * instead of Next.js's default white page.
 *
 * Hit when a user types a URL that doesn't map to any of the six
 * implemented routes (e.g. `/sells` instead of `/sell`).
 */
export default function NotFound() {
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900 p-8 text-center">
      <h1 className="text-xl font-semibold text-slate-100">Page not found</h1>
      <p className="mt-1 text-sm text-slate-400">
        The dashboard has six pages: Overview, Signals, Watchlist, Sell
        monitor, Analytics, and Reports.
      </p>
      <Link
        href="/"
        className="mt-4 inline-block rounded border border-slate-700 bg-slate-800 px-3 py-1 text-xs text-slate-200 hover:bg-slate-700"
      >
        ← Back to Overview
      </Link>
    </div>
  );
}
