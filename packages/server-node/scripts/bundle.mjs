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
import { readFileSync } from "fs";
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
