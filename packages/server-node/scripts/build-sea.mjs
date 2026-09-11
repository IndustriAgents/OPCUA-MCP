// Builds a single-file executable: one download, no Node, no npm, no network.
//
// This is the artifact for the case the `.mcpb` does not cover — an MCP client
// other than Claude Desktop, or a machine with no Node runtime and no way to get
// one, which on an air-gapped plant network is the normal case rather than the
// exception.
//
// Uses Node's built-in single-executable-application support: bundle the server
// into one CommonJS file, turn that into a blob, and inject the blob into a copy
// of the `node` binary running this script. Consequences worth knowing:
//
//   * The executable embeds *this* Node, so it is platform-specific and cannot
//     be cross-compiled. CI builds one per OS (.github/workflows/release.yml).
//   * It is ~110 MB, nearly all of it Node itself.
//   * macOS requires the copied binary's signature to be stripped before
//     injection and re-applied after, or the result will not launch at all.
//     The ad-hoc signature it gets here is enough for that, but not enough for
//     Gatekeeper — first launch still needs the quarantine flag cleared. See
//     docs/install.md.
//
// Requires Node 20+ to build (`--experimental-sea-config`); the *server* still
// supports Node 18, and the npm package is unaffected by any of this.
//
// Run: npm run build:sea   ->   dist/opcua-mcp-server-node-<platform>-<arch>[.exe]
import { spawnSync } from "child_process";
import { chmodSync, copyFileSync, mkdirSync, renameSync, rmSync, writeFileSync } from "fs";
import { join } from "path";

import { PKG_ROOT, bundle, resolveCli } from "./bundle.mjs";

const STAGING = join(PKG_ROOT, "build-sea");
const DIST = join(PKG_ROOT, "dist");

const IS_WINDOWS = process.platform === "win32";
const IS_MACOS = process.platform === "darwin";
// Named by runtime as well as platform: the Python runtime builds its own
// executable for the same platforms, and the two land side by side on a GitHub
// release. `process.arch` supplies the canonical `x64`/`arm64` spelling, which
// the PyInstaller spec mirrors.
const OUTPUT = join(
  DIST,
  `opcua-mcp-server-node-${process.platform}-${process.arch}${IS_WINDOWS ? ".exe" : ""}`
);

/** Run a command, failing the build with its output rather than a bare status. */
function run(cmd, args, opts = {}) {
  const proc = spawnSync(cmd, args, { stdio: "inherit", ...opts });
  if (proc.error) throw proc.error;
  if (proc.status !== 0) {
    throw new Error(`${cmd} ${args.join(" ")} exited with status ${proc.status}`);
  }
}

/** Reduce a macOS universal binary to the architecture we are building for.
 *
 * The official macOS Node installer ships a universal (x86_64 + arm64) binary.
 * Every string in it therefore appears twice — including the fuse `postject`
 * looks for, which makes it refuse to inject with "Multiple occurences of
 * sentinel". Thinning to the current slice fixes that and halves the output.
 *
 * A no-op on the single-architecture builds CI produces.
 */
function thinUniversalBinary(path) {
  const archs = spawnSync("lipo", ["-archs", path], { encoding: "utf8" });
  if (archs.status !== 0) return; // not a Mach-O universal binary; nothing to thin
  const slices = archs.stdout.trim().split(/\s+/).filter(Boolean);
  if (slices.length < 2) return;

  const slice = { arm64: "arm64", x64: "x86_64" }[process.arch];
  if (!slice) throw new Error(`no known macOS slice name for arch ${process.arch}`);

  const thinned = `${path}.thin`;
  run("lipo", ["-thin", slice, path, "-output", thinned]);
  renameSync(thinned, path);
  chmodSync(path, 0o755);
}

async function main() {
  const [major] = process.versions.node.split(".").map(Number);
  if (major < 20) {
    throw new Error(
      `building a single-file executable needs Node 20+, got ${process.versions.node}`
    );
  }

  rmSync(STAGING, { recursive: true, force: true });
  mkdirSync(STAGING, { recursive: true });
  mkdirSync(DIST, { recursive: true });

  // CommonJS: Node's SEA loader does not accept an ES module as the main script.
  const main = join(STAGING, "server.cjs");
  await bundle({
    entry: join(PKG_ROOT, "src", "sea.ts"),
    outfile: main,
    format: "cjs",
    target: "node20",
  });

  const configPath = join(STAGING, "sea-config.json");
  writeFileSync(
    configPath,
    `${JSON.stringify(
      {
        main,
        output: join(STAGING, "sea-prep.blob"),
        // The warning is aimed at developers evaluating the feature, not at the
        // plant engineer running the binary, and it would land on stderr in the
        // middle of the MCP stdio session.
        disableExperimentalSEAWarning: true,
      },
      null,
      2
    )}\n`
  );

  run(process.execPath, ["--experimental-sea-config", configPath]);

  copyFileSync(process.execPath, OUTPUT);
  chmodSync(OUTPUT, 0o755);

  if (IS_MACOS) {
    thinUniversalBinary(OUTPUT);
    // macOS refuses to run a binary whose contents no longer match its
    // signature, and injection changes the contents. Strip first, re-sign after.
    run("codesign", ["--remove-signature", OUTPUT]);
  }

  // Run postject's CLI directly rather than through `npx`: it is a declared
  // devDependency, so a build must never reach the network to fetch it, and on
  // Windows `npx` is a `.cmd` that `spawnSync` cannot execute at all.
  run(process.execPath, [
    resolveCli("postject"),
    OUTPUT,
    "NODE_SEA_BLOB",
    join(STAGING, "sea-prep.blob"),
    "--sentinel-fuse",
    "NODE_SEA_FUSE_fce680ab2cc467b6e072b8b5df1996b2",
    ...(IS_MACOS ? ["--macho-segment-name", "NODE_SEA"] : []),
  ]);

  if (IS_MACOS) run("codesign", ["--sign", "-", OUTPUT]);

  console.error(`built ${OUTPUT}`);
}

await main();
