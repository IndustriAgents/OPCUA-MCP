// Every `--install` case in tests/fixtures/install-cases.json, through the Node
// parser and planner in-process (#135).
//
// tests/unit/test_install_cases.py runs the same table through both command
// lines as subprocesses; this is the fast, debuggable half for the Node runtime,
// and it checks the *unredacted* configuration, which the subprocess half can
// only see through the redacted preview.
//
// Run: npm test   (requires `npm run build` first — these import build/install.js)
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, sep } from "node:path";
import { describe, test } from "node:test";
import { fileURLToPath } from "node:url";

import { InstallRefusal, parseArgs, planInstall } from "../build/install.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const { cases: CASES } = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "install-cases.json"), "utf8")
);

/** The scratch directory the table's `{pki}` stands for. */
function makePki() {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "opcua-install-cases-")));
  for (const name of ["server.pem", "client.pem", "client_key.pem", "user.pem", "user_key.pem"]) {
    writeFileSync(join(root, name), "placeholder\n");
  }
  const policy = { version: 1, profile: "operator", control: { writable_nodes: ["ns=2;i=5"] } };
  writeFileSync(join(root, "policy-operator.json"), JSON.stringify(policy));
  writeFileSync(join(root, "policy-bad.json"), "{ not json");
  mkdirSync(join(root, "audit"));
  return root;
}

function substitute(value, pki) {
  if (typeof value === "string")
    return value.replaceAll("{pki}/", pki + sep).replaceAll("{pki}", pki);
  if (Array.isArray(value)) return value.map((v) => substitute(v, pki));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, substitute(v, pki)]));
  }
  return value;
}

/** What the Node runtime does with one case: {exit, error, message, plan}. */
function outcome(testCase, pki) {
  const client = testCase.client ?? "claude-desktop";
  const args = ["--install", client, "--dry-run", ...substitute(testCase.args, pki)];
  const action = parseArgs(args, "opc.tcp://localhost:4840");
  if (action.kind === "error") return { exit: 2, error: null, message: action.message };
  assert.equal(action.kind, "install");
  try {
    const plan = planInstall(action.options, { env: testCase.env ?? {}, cwd: pki });
    return { exit: 0, plan };
  } catch (error) {
    if (!(error instanceof InstallRefusal)) throw error;
    return { exit: error.exitCode, error: error.code, message: error.message };
  }
}

describe("install cases (shared table)", () => {
  for (const testCase of CASES) {
    test(testCase.name, () => {
      const pki = makePki();
      const expect = substitute({ ...testCase.expect, ...(testCase.byRuntime?.node ?? {}) }, pki);
      const result = outcome(testCase, pki);

      assert.equal(result.exit, expect.exit, result.message);
      const printed = [result.message ?? "", ...(result.plan?.summary ?? [])]
        .concat((result.plan?.warnings ?? []).map((w) => w.message))
        .join("\n");
      for (const secret of testCase.neverPrinted ?? []) {
        assert.ok(!printed.includes(secret), `printed ${secret}`);
      }
      if (expect.exit !== 0) {
        assert.equal(result.error, expect.error ?? null);
        return;
      }
      assert.deepEqual(result.plan.env, expect.env);
      assert.deepEqual(
        Object.keys(result.plan.env),
        Object.keys(expect.env),
        "not in schema order"
      );
      assert.deepEqual(result.plan.envVars, expect.envVars ?? []);
      assert.deepEqual(
        result.plan.warnings.map((w) => w.code),
        expect.warnings
      );
    });
  }
});
