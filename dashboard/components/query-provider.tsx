"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

/**
 * Wraps the app with a per-instance TanStack Query client.
 *
 * Lives in a client component because `QueryClientProvider` itself
 * relies on React context. Stays at the top of the tree so every
 * `useQuery` hook in deeper client components sees the same cache.
 *
 * Defaults are set here so individual hooks can stay terse:
 *   - `staleTime` 30s    — most dashboard data is "live-ish"; later
 *                          steps may override per-route.
 *   - `retry` 1          — one quiet retry for transient 503s when a
 *                          scan is mid-write; anything beyond surfaces.
 *   - `refetchOnWindowFocus` true — re-checking on tab return is the
 *                          most common actual interaction.
 */
export function QueryProvider({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30 * 1000,
            retry: 1,
            refetchOnWindowFocus: true,
          },
        },
      }),
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
