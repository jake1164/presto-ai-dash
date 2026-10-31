// Execute the generated redirect scripts against the published URL inventory.
// No browser, network, packages or provider accounts are involved.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const root = fileURLToPath(new URL("../", import.meta.url));
const site = path.resolve(process.argv[2] || path.join(root, "build/pages"));
const { routes } = JSON.parse(readFileSync(path.join(root, "tools/pages_legacy_urls.json"), "utf8"));
const base = "https://example.test/presto-ai-dash/";
let checked = 0;

function runRedirect(file, current) {
  const html = readFileSync(path.join(site, file), "utf8");
  const match = html.match(/<script id="legacy-docs-redirect">([\s\S]*?)<\/script>/);
  assert.ok(match, `${file}: missing compatibility redirect`);
  const url = new URL(current);
  let result = null;
  const handlers = {};
  vm.runInNewContext(match[1], {
    location: {
      search: url.search,
      hash: url.hash,
      replace(target) { result = new URL(target, current).href; },
    },
    window: { addEventListener(event, handler) { handlers[event] = handler; } },
  }, { timeout: 1000 });
  checked++;
  return result;
}

for (const [old, entry] of Object.entries(routes)) {
  const directory = old.replace(/index\.html$/, "");
  const target = entry.page.replace(/index\.html$/, "");
  for (const fragment of ["", ...entry.anchors.map(anchor => `#${encodeURIComponent(anchor)}`)]) {
    for (const source of new Set([directory, old])) {
      const current = base + source + "?from=legacy" + fragment;
      const result = runRedirect(old, current);
      if (old === "index.html" && !fragment) {
        assert.equal(result, null, "the product homepage must remain accessible");
      } else {
        assert.equal(result, base + "docs/" + target + "?from=legacy" + fragment, current);
      }
    }
  }
}
assert.equal(runRedirect("index.html", base + "#future-product-section"), null);
assert.equal(runRedirect("index.html", base + "#%invalid"), null);
assert.equal(runRedirect("landing/index.html", base + "landing/?from=legacy"), base + "?from=legacy");
console.log(`Verified ${checked} redirects, including heading fragments, queries and explicit index.html URLs.`);
