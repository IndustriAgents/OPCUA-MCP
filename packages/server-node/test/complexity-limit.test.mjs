import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { test } from "node:test";

const packageRoot = fileURLToPath(new URL("..", import.meta.url));
const binary = join(packageRoot, "node_modules", "oxlint", "bin", "oxlint");

test("the complexity gate accepts 25 paths and refuses 26", () => {
  const directory = mkdtempSync(join(tmpdir(), "opcua-complexity-"));
  try {
    for (const branches of [24, 25]) {
      const source = join(directory, "example.ts");
      const conditions = Array.from(
        { length: branches },
        (_, i) => `if (value === ${i}) return ${i};`
      );
      writeFileSync(
        source,
        `export function example(value: number) {\n${conditions.join("\n")}\nreturn -1;\n}`
      );
      const checked = spawnSync(
        process.execPath,
        [binary, "-c", join(packageRoot, ".oxlintrc.json"), source],
        { encoding: "utf8" }
      );
      assert.equal(checked.status, branches === 24 ? 0 : 1, checked.stdout + checked.stderr);
      if (branches === 25) assert.match(checked.stdout + checked.stderr, /complexity/);
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
