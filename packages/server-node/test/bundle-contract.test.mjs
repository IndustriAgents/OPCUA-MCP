import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { test } from "node:test";
import { bundle, PKG_ROOT } from "../scripts/bundle.mjs";

for (const format of ["esm", "cjs"]) {
  test(`nested feature contract imports remain self-contained in ${format} bundles`, async () => {
    const directory = mkdtempSync(join(tmpdir(), "opcua-contract-bundle-"));
    try {
      const outfile = join(directory, format === "esm" ? "feature.mjs" : "feature.cjs");
      await bundle({ entry: join(PKG_ROOT, "src", "application", "browse.ts"), outfile, format });
      const result = spawnSync(process.execPath, [outfile], { cwd: directory, encoding: "utf8" });
      assert.equal(result.status, 0, result.stderr);
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });
}
