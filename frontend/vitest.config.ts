import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

// Unit tests for the pure logic under `lib/` — no jsdom, no React, no browser.
//
// The split is deliberate and matches what already exists rather than adding a second way to
// test the same thing. `npm run smoke` (`scripts/smoke.mjs`) drives the built page in
// headless Chrome and is the check that the app RENDERS; `tsc` proves the types line up.
// Neither of those exercises a branch, which is what this runs: comparators, status mappings,
// funnel arithmetic, and input validation, each a pure function of its arguments.
//
// `environment: "node"` keeps it that way. A jsdom environment would make it possible to
// import a component here, and the first one that got imported would pull in React, the query
// client, and the reason this file says "no e2e" in its own config.
export default defineConfig({
  // The same `@/*` -> `./*` alias `tsconfig.json` declares and Next resolves at build time.
  // Restated here because vitest does not read `tsconfig.json`'s `paths`, and without it any
  // module that imports a sibling as `@/lib/...` — which most of `lib/crawls/` does — fails
  // to resolve.
  resolve: {
    alias: { "@": fileURLToPath(new URL(".", import.meta.url)) },
  },
  test: {
    environment: "node",
    include: ["lib/**/*.test.ts"],
  },
});
