"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

// Top navigation. Every dashboard page is now real (Steps 3–5
// shipped Overview, Signals, Watchlist, Sell monitor, Analytics, and
// Reports), so the nav config is a flat list — no more disabled
// placeholder branch.

const links = [
  { href: "/", label: "Overview" },
  { href: "/signals", label: "Signals" },
  { href: "/watchlist", label: "Watchlist" },
  { href: "/sell", label: "Sell monitor" },
  { href: "/analytics", label: "Analytics" },
  { href: "/reports", label: "Reports" },
] as const;

function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function Nav() {
  const pathname = usePathname() ?? "/";
  return (
    <header className="border-b border-slate-800 bg-slate-900">
      <div className="mx-auto flex max-w-6xl items-center gap-6 px-4 py-3">
        <Link
          href="/"
          className="text-sm font-semibold tracking-wide text-slate-100 hover:text-white"
        >
          crypto_monitor
        </Link>
        <nav className="flex items-center gap-4 text-sm">
          {links.map((l) => {
            const active = isActive(pathname, l.href);
            return (
              <Link
                key={l.href}
                href={l.href}
                className={
                  active
                    ? "font-medium text-white"
                    : "text-slate-300 hover:text-white"
                }
                aria-current={active ? "page" : undefined}
              >
                {l.label}
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
