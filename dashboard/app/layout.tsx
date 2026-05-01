import type { Metadata } from "next";

import { Nav } from "@/components/nav";
import { QueryProvider } from "@/components/query-provider";

import "./globals.css";

export const metadata: Metadata = {
  title: "crypto_monitor",
  description: "Read-only dashboard for the crypto_monitor bot.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <QueryProvider>
          <Nav />
          <main className="mx-auto max-w-6xl px-4 py-6">{children}</main>
        </QueryProvider>
      </body>
    </html>
  );
}
