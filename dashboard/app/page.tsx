// Server-component shell for the home route. The actual data view is
// a client component below it because TanStack Query needs the
// browser. Keeping the shell minimal lets Next.js statically pre-render
// the layout and only hydrate the data section.

import { OverviewView } from "./overview-view";

export default function Page() {
  return <OverviewView />;
}
