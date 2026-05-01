import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Severity / regime palettes — kept here so future pages
        // (signals table, sell monitor, regime history chart) all
        // share the same color contract.
        severity: {
          normal: "#fbbf24",       // amber-400
          strong: "#f97316",       // orange-500
          veryStrong: "#ef4444",   // red-500
        },
        regime: {
          on: "#10b981",           // emerald-500
          neutral: "#94a3b8",      // slate-400
          off: "#ef4444",          // red-500
        },
      },
    },
  },
  plugins: [],
};

export default config;
