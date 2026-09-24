// Rewrite package.json so every runtime dependency is pinned to the floor of its
// declared range — the Node half of the "lowest supported dependency set" that
// .github/workflows/dependency-matrix.yml tests (uv's `--resolution
// lowest-direct` is the Python half). npm has no such resolution mode, so this
// is it: `^2.184.8` becomes `2.184.8`, and the next `npm install` has to fetch
// exactly that.
//
// It edits package.json IN PLACE and is meant for a disposable checkout (the CI
// runner). Run it locally only if you are ready to `git checkout` the manifest
// and lockfile afterwards.
//
// Only `dependencies` are pinned. They are what a user's `npm install` resolves
// from our ranges; devDependencies never reach a user and stay on the lockfile,
// so a failure in the lowest job points at a runtime floor rather than at a
// compiler that happened to move.
//
// A range this cannot reduce to one version (`*`, `>=`, `||`, a tag, a URL)
// fails the run instead of being guessed at: such a range has no tested floor,
// and docs/dependency-policy.md does not allow one on a runtime dependency.
import { readFileSync, writeFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const here = dirname(fileURLToPath(import.meta.url)); // packages/server-node/scripts
const manifestPath = join(here, "..", "package.json");

// `^1.2.3` or `~1.2.3`: one comparator, bounded above by the operator itself.
// Caret on a 0.x version is bounded at the next minor, which is what semver
// treats as the breaking boundary below 1.0, so it passes too.
const BOUNDED_RANGE = /^[\^~](\d+\.\d+\.\d+)$/;

const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
const dependencies = manifest.dependencies ?? {};
const unbounded = [];

for (const [name, range] of Object.entries(dependencies)) {
  const match = BOUNDED_RANGE.exec(range);
  if (!match) {
    unbounded.push(`${name}@${range}`);
    continue;
  }
  dependencies[name] = match[1];
  console.error(`${name}: ${range} -> ${match[1]}`);
}

if (unbounded.length > 0) {
  console.error(
    `cannot pin to a floor (not a ^ or ~ range): ${unbounded.join(", ")}\n` +
      "See docs/dependency-policy.md for the ranges a runtime dependency may use."
  );
  process.exit(1);
}

writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
