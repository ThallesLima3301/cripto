import { ApiError } from "@/lib/api";

type Props = {
  error: Error;
  retry?: () => void;
};

/**
 * Friendly error panel.
 *
 * Singles out two common failure modes:
 *   - status 0   → API process not running; show the launch hint.
 *   - status 503 → the bot is mid-write; the next poll should clear.
 * Everything else falls back to the API's `detail` message.
 */
export function ErrorState({ error, retry }: Props) {
  const isApi = error instanceof ApiError;
  const status = isApi ? error.status : null;

  return (
    <div className="rounded-md border border-red-500/30 bg-red-500/10 p-4 text-sm">
      <div className="font-medium text-red-200">
        {status === 0 ? "Cannot reach the API." : "API request failed."}
      </div>
      <div className="mt-1 text-red-300/80">
        {error.message}
      </div>
      {status === 0 ? (
        <div className="mt-2 text-xs text-slate-400">
          Start the FastAPI adapter:{" "}
          <code className="rounded bg-slate-800 px-1 py-0.5">
            uvicorn crypto_monitor.dashboard.api:app --port 8787
          </code>
        </div>
      ) : status === 503 ? (
        <div className="mt-2 text-xs text-slate-400">
          The bot is briefly holding the database. The next refresh
          should succeed.
        </div>
      ) : null}
      {retry ? (
        <button
          type="button"
          onClick={retry}
          className="mt-3 rounded border border-slate-700 bg-slate-800 px-3 py-1 text-xs text-slate-200 hover:bg-slate-700"
        >
          Retry
        </button>
      ) : null}
    </div>
  );
}
