// Thin fetch wrapper around the FastAPI adapter.
//
// The frontend must NEVER reach SQLite directly. Every call goes
// through this module so the base URL is configured in exactly one
// place (`NEXT_PUBLIC_API_BASE_URL`) and error shapes are consistent.

import type { Envelope } from "./types";

const BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8787";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
  ) {
    super(`api ${status}: ${detail}`);
    this.name = "ApiError";
  }
}

/**
 * Fetch a `data + meta` envelope from the dashboard API.
 *
 * The response is parsed but NOT validated against a schema; the
 * Pydantic layer on the server is the source of truth for shape, so
 * by the time bytes reach this function the contract has held. The
 * caller's TypeScript type controls how the unwrapped value is read.
 */
export async function apiGet<T>(path: string): Promise<Envelope<T>> {
  const url = `${BASE_URL}${path}`;
  let res: Response;
  try {
    res = await fetch(url, {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
  } catch (err) {
    // Network error / CORS / API process not running. Surface a stable
    // status code (0) so the UI can render a clean "unreachable" state.
    throw new ApiError(0, (err as Error).message ?? "network error");
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // Non-JSON error body — keep statusText.
    }
    throw new ApiError(res.status, detail);
  }

  return (await res.json()) as Envelope<T>;
}

/**
 * Build a `?key=value&...` query string from a flat params object.
 *
 * Skips entries whose value is `undefined`, `null`, or the empty
 * string so callers can pass partial filter objects without filtering
 * them themselves. Returns `""` (no leading `?`) when every entry was
 * skipped.
 */
export function buildQuery(
  params: Record<string, string | number | undefined | null>,
): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    sp.set(k, String(v));
  }
  const out = sp.toString();
  return out ? `?${out}` : "";
}
