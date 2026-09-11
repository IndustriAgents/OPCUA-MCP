// Bundles the server and its whole dependency tree into a single file.
//
// The npm package does not need this — `npm install` brings node_modules with
// it. The two *downloadable* artifacts do:
//
//   * the MCP bundle (`build-mcpb.mjs`), where 7 MB beats shipping a 90 MB
//     node_modules inside a zip a plant engineer has to download;
//   * the single-file executable (`build-sea.mjs`), which by definition is one
//     file.
//
// Two things about node-opcua make this less routine than it sounds, and both
// are handled below rather than left to bite at runtime:
//
//   1. Its dependency tree is CommonJS, so an ESM bundle has to be handed a
//      working `require`, `__filename` and `__dirname` (BANNER).
//   2. `contract.ts` locates `contract.json` and `version.json` relative to its
//      own module URL, which stops being a real directory once bundled. So the
//      contract and version are inlined at build time instead (inlineContract),
//      which also makes the output genuinely self-contained.
//
// Bundling is verified end to end, not assumed: tests/smoke/test_artifacts.py
// drives the packed .mcpb against a live OPC UA server over MCP.
import * as esbuild from "esbuild";
import { existsSync, readFileSync } from "fs";
import { createRequire } from "module";
import { dirname, join } from "path";
import { fileURLToPath } from "url";

const here = dirname(fileURLToPath(import.meta.url)); // packages/server-node/scripts
export const PKG_ROOT = join(here, "..");
export const REPO_ROOT = join(PKG_ROOT, "..", "..");
export const CONTRACT_PATH = join(REPO_ROOT, "contract", "tools.json");

/** The version every generated artifact is stamped with. */
export const VERSION = JSON.parse(readFileSync(join(PKG_ROOT, "package.json"), "utf8")).version;

// The CommonJS globals node-opcua's dependencies expect. Without `require` the
// bundle dies on "Dynamic require of \"os\" is not supported"; without
// `__filename` it dies in node-opcua-factory's debug-log setup. Only meaningful
// for the ESM output — a CJS bundle has all three already.
const BANNER = [
  'import { createRequire as __createRequire } from "node:module";',
  'import { fileURLToPath as __fileURLToPath } from "node:url";',
  'import { dirname as __dirname_of } from "node:path";',
  "const require = __createRequire(import.meta.url);",
  "const __filename = __fileURLToPath(import.meta.url);",
  "const __dirname = __dirname_of(__filename);",
].join("\n");

/** esbuild plugin: replace `./contract.js` with the contract inlined as literals.
 *
 * Reads the canonical `/contract/tools.json` and `package.json` at build time,
 * so a bundle can no more drift from the contract than the npm package can.
 */
function inlineContract() {
  return {
    name: "inline-contract",
    setup(build) {
      build.onResolve({ filter: /^\.\/contract\.js$/ }, () => ({
        path: "contract",
        namespace: "opcua-inline",
      }));
      build.onLoad({ filter: /.*/, namespace: "opcua-inline" }, () => ({
        // Deliberately does not re-export `BUILD_DIR`: there is no build
        // directory in a bundle, and a missing export is a build error rather
        // than a path that resolves to nonsense at runtime.
        contents: [
          `export const CONTRACT = ${readFileSync(CONTRACT_PATH, "utf8")};`,
          `export const VERSION = ${JSON.stringify(VERSION)};`,
        ].join("\n"),
        loader: "js",
      }));
    },
  };
}

/**
 * Bundle `src/index.ts` into `outfile`.
 *
 * @param {{ outfile: string, format?: "esm" | "cjs", target?: string, entry?: string }} opts
 */
export async function bundle({
  outfile,
  format = "esm",
  target = "node18",
  entry = join(PKG_ROOT, "src", "index.ts"),
}) {
  await esbuild.build({
    entryPoints: [entry],
    outfile,
    bundle: true,
    platform: "node",
    format,
    target,
    banner: format === "esm" ? { js: BANNER } : undefined,
    // `index.ts` reads `import.meta.url` to decide whether it is the process
    // entry point. A CommonJS bundle has no `import.meta`, and esbuild warns
    // about that unless it is given a value; the single-file build reaches the
    // server through `sea.ts` instead, so any inert placeholder will do.
    define: format === "cjs" ? { "import.meta.url": '"file:///sea"' } : undefined,
    plugins: [inlineContract()],
    logLevel: "warning",
  });
  return outfile;
}

/** Absolute path to a dependency's CLI entry point, from its declared `bin`.
 *
 * Used instead of `npx <name>`, which cannot be spawned without a shell on
 * Windows: npm installs those as `.cmd` shims and `spawnSync` rejects them with
 * ENOENT. Handing the resolved JavaScript file to `process.execPath` sidesteps
 * the shell, `PATH`, and any question about a path with a space in it — the same
 * reasoning as the absolute paths `--install` writes into a client config.
 *
 * Walks up from the package's main entry rather than resolving the bin path
 * directly, because a package may declare an `exports` map that refuses deep
 * subpaths — `@anthropic-ai/mcpb` does — and may move its bin between versions.
 */
export function resolveCli(packageName) {
  const require_ = createRequire(import.meta.url);
  let dir = dirname(require_.resolve(packageName));

  for (;;) {
    const manifestPath = join(dir, "package.json");
    if (existsSync(manifestPath)) {
      const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
      if (manifest.name === packageName) {
        const bin =
          typeof manifest.bin === "string" ? manifest.bin : Object.values(manifest.bin)[0];
        if (!bin) throw new Error(`${packageName} declares no bin entry`);
        return join(dir, bin);
      }
    }
    const parent = dirname(dir);
    if (parent === dir) throw new Error(`could not find the package root for ${packageName}`);
    dir = parent;
  }
}
