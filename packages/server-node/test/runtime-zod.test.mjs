import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { it } from "node:test";

// Exercise the schema implementation imported by the MCP transport, including
// after dependency deduplication changes its installed location.
const sdkRequire = createRequire(import.meta.resolve("@modelcontextprotocol/sdk/server/index.js"));
const z = sdkRequire("zod/v4");

it("the MCP runtime rejects oversized collections before walking their elements", () => {
  let visited = 0;
  const element = z.unknown().superRefine((value, ctx) => {
    visited++;
    ctx.addIssue({ code: "custom", message: "invalid element" });
  });
  const result = z.array(element).max(2).safeParse(Array(1_000).fill(null));
  assert.equal(result.success, false);
  assert.equal(visited, 0, "oversized inputs must be rejected before element validation");
  assert.equal(result.error.issues.length, 1);
  assert.equal(result.error.issues[0].code, "too_big");
});
