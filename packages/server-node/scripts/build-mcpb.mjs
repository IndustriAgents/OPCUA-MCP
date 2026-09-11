// Builds the MCP bundle (`.mcpb`) — the double-click install for Claude Desktop.
//
// Why this exists: the documented quick start asks the user to find
// `claude_desktop_config.json`, edit JSON, and have Node or Python on PATH. The
// people who run OPC UA servers are automation engineers, often on locked-down
// machines on air-gapped plant networks, and every one of those steps is a wall.
// A `.mcpb` is one file they download and drag into Claude Desktop's Extensions
// pane; Claude Desktop supplies the Node runtime and renders `user_config` as a
// settings form, so there is no config file to edit and no runtime to install.
//
// Layout of the bundle:
//
//   manifest.json     mcpb/manifest.json, with the version stamped from package.json
//   server/index.mjs  the whole server and its dependencies, bundled (see bundle.mjs)
//   README.md         shown in the extension's details pane
//   LICENSE
//
// Run: npm run build:mcpb   ->   dist/opcua-mcp-server-<version>.mcpb
import { spawnSync } from "child_process";
import { copyFileSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "fs";
import { join } from "path";

import { PKG_ROOT, REPO_ROOT, VERSION, bundle, resolveCli } from "./bundle.mjs";

const STAGING = join(PKG_ROOT, "build-mcpb");
const DIST = join(PKG_ROOT, "dist");
const OUTPUT = join(DIST, `opcua-mcp-server-${VERSION}.mcpb`);

/** Stage the manifest, stamping the version so it cannot drift from package.json.
 *
 * The checked-in manifest carries a real version rather than a placeholder — it
 * has to, for `mcpb validate` to be worth running on it — and
 * tests/unit/test_version_manifests.py asserts the two agree. Stamping here as
 * well means a release that forgets the manifest still ships a correct bundle.
 */
function stageManifest() {
  const manifest = JSON.parse(readFileSync(join(PKG_ROOT, "mcpb", "manifest.json"), "utf8"));
  manifest.version = VERSION;
  writeFileSync(join(STAGING, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);
  return manifest;
}

async function main() {
  rmSync(STAGING, { recursive: true, force: true });
  mkdirSync(join(STAGING, "server"), { recursive: true });
  mkdirSync(DIST, { recursive: true });

  const manifest = stageManifest();
  await bundle({ outfile: join(STAGING, manifest.server.entry_point) });
  copyFileSync(join(PKG_ROOT, "README.md"), join(STAGING, "README.md"));
  copyFileSync(join(REPO_ROOT, "LICENSE"), join(STAGING, "LICENSE"));

  // `mcpb pack` validates the manifest against the published schema on the way
  // in, which is the point of using it rather than zipping the directory
  // ourselves: an invalid manifest fails the build instead of failing silently
  // in somebody's Claude Desktop.
  const mcpb = spawnSync(
    process.execPath,
    [resolveCli("@anthropic-ai/mcpb"), "pack", STAGING, OUTPUT],
    { cwd: PKG_ROOT, stdio: "inherit" }
  );
  if (mcpb.status !== 0) {
    throw new Error(`mcpb pack failed with status ${mcpb.status}`);
  }
  console.error(`built ${OUTPUT}`);
}

await main();
