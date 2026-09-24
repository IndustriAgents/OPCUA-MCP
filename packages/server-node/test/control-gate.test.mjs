// When control may travel over the OPC UA connection (#134).
//
// The control gate used to check that the channel was *encrypted*, while its
// safety meaning needs the server to be *authenticated*: both client libraries
// encrypt happily to whatever certificate the endpoint presents, so an attacker
// able to answer for the endpoint got an encrypted channel and, with it, the
// control tools. Control now needs a verified server identity — a secured
// channel and a pinned `OPCUA_SERVER_CERT` — or one of two explicit lab
// overrides, each covering exactly one missing property.
//
// `tests/unit/test_control_gate.py` is the Python half and drives the same
// shared table, `tests/fixtures/control-gate.json`.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import {
  CertificatePurpose,
  createSelfSignedCertificate,
  generateKeyPair,
} from "node-opcua-crypto";

import { CONTRACT } from "../build/contract.js";
import { message } from "../build/errors.js";
import {
  ToolPolicy,
  controlGate,
  describePolicy,
  parsePolicyConfig,
  serverIdentityRecord,
} from "../build/policy.js";
import { pinnedCertificateProblem } from "../build/security.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const FIXTURE = JSON.parse(
  readFileSync(join(ROOT, "tests", "fixtures", "control-gate.json"), "utf8")
);
const TOOL = FIXTURE.tool;
const SPEC = CONTRACT.tools.find((tool) => tool.name === TOOL);
const CONTROL_TOOLS = CONTRACT.tools.filter((tool) =>
  ["control", "alarm-action"].includes(tool.accessClass)
);

function policy(env) {
  const subject = new ToolPolicy(parsePolicyConfig(env));
  subject.bindNamespaces(["http://opcfoundation.org/UA/", "urn:one", "urn:plant"]);
  return subject;
}

describe("the control gate, from the shared table", () => {
  for (const testCase of FIXTURE.cases) {
    // The Python half asserts the same fields and the same sentence.
    it(testCase.name, () => {
      const subject = policy(testCase.env);

      assert.equal(controlGate(subject.config), testCase.control);
      assert.deepEqual(serverIdentityRecord(subject.config), testCase.server_identity);
      assert.equal(describePolicy(subject), testCase.startup);
      assert.equal(subject.isVisible(SPEC), testCase.offered);

      if (testCase.refusal === null) {
        subject.authorize(TOOL, FIXTURE.call);
        return;
      }
      assert.throws(
        () => subject.authorize(TOOL, FIXTURE.call),
        (error) => {
          assert.equal(
            error.message,
            message(testCase.refusal, { tool: TOOL, profile: testCase.env.OPCUA_PROFILE })
          );
          return true;
        }
      );
    });
  }

  it("covers every combination, and every state of the gate", () => {
    // A table that quietly lost a row is a rule nobody is checking any more.
    const keys = new Set(
      FIXTURE.cases.map(({ env }) =>
        JSON.stringify([
          env.OPCUA_PROFILE,
          env.OPCUA_SECURITY_POLICY,
          env.OPCUA_SECURITY_MODE ?? null,
          "OPCUA_SERVER_CERT" in env,
          env.OPCUA_ALLOW_INSECURE_CONTROL === "true",
          env.OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL === "true",
        ])
      )
    );
    assert.equal(keys.size, 3 * 5 * 2 * 2);
    assert.equal(FIXTURE.cases.length, keys.size);
    assert.deepEqual(
      new Set(FIXTURE.cases.map((testCase) => testCase.control)),
      new Set(["secured", "INSECURE-OVERRIDE", "UNVERIFIED-OVERRIDE", "blocked"])
    );
  });

  it("opens or closes every control tool together", () => {
    for (const testCase of FIXTURE.cases) {
      const subject = policy({ ...testCase.env, OPCUA_PROFILE: "full" });
      const open = controlGate(subject.config) !== "blocked";
      for (const tool of CONTROL_TOOLS) {
        assert.equal(subject.isVisible(tool), open, `${testCase.name}: ${tool.name}`);
      }
    }
  });
});

describe("a control refusal", () => {
  it("names the variable that would open it", () => {
    // A bare "disabled", with no way forward, sends an operator to the source.
    const unsecured = message("controlNeedsSecureChannel", { tool: TOOL });
    assert.match(unsecured, /OPCUA_SECURITY_POLICY/);
    assert.match(unsecured, /OPCUA_ALLOW_INSECURE_CONTROL=true/);
    const unverified = message("controlNeedsVerifiedServer", { tool: TOOL });
    assert.match(unverified, /OPCUA_SERVER_CERT/);
    assert.match(unverified, /OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL=true/);
  });

  it("does not let the insecure override vouch for an unverified server", () => {
    const subject = policy({
      OPCUA_PROFILE: "full",
      OPCUA_SECURITY_POLICY: "Basic256Sha256",
      OPCUA_ALLOW_INSECURE_CONTROL: "true",
    });
    assert.equal(controlGate(subject.config), "blocked");
    assert.throws(
      () => subject.authorize(TOOL, FIXTURE.call),
      /OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL/
    );
  });

  it("is not blamed on the channel for a tool the allowlist excludes", () => {
    const subject = policy({
      OPCUA_PROFILE: "full",
      OPCUA_SECURITY_POLICY: "Basic256Sha256",
      OPCUA_ALLOWED_TOOLS: "read_opcua_nodes",
    });
    assert.throws(
      () => subject.authorize(TOOL, FIXTURE.call),
      (error) => error.message === message("toolDisabled", { tool: TOOL, profile: "full" })
    );
  });
});

describe("the unverified-server override", () => {
  it("can come from the policy file, and the environment overrides it", () => {
    const path = join(mkdtempSync(join(tmpdir(), "opcua-gate-")), "policy.json");
    writeFileSync(
      path,
      JSON.stringify({ version: 1, profile: "full", allow_unverified_server_control: true })
    );
    const base = { OPCUA_POLICY_FILE: path, OPCUA_SECURITY_POLICY: "Basic256Sha256" };
    assert.equal(controlGate(policy(base).config), "UNVERIFIED-OVERRIDE");
    assert.equal(
      controlGate(policy({ ...base, OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL: "false" }).config),
      "blocked"
    );
  });

  it("must be a boolean", () => {
    assert.throws(
      () => parsePolicyConfig({ OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL: "maybe" }),
      /OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL must be true or false/
    );
  });
});

describe("a pin outside its validity window", () => {
  const DAY = 24 * 60 * 60 * 1000;

  /** A self-signed certificate with the given window, written as PEM. */
  async function certificate(notBefore, notAfter) {
    const { privateKey } = await generateKeyPair();
    const { cert } = await createSelfSignedCertificate({
      privateKey,
      notBefore,
      notAfter,
      subject: "CN=server",
      applicationUri: "urn:test:server",
      purpose: CertificatePurpose.ForApplication,
    });
    const path = join(mkdtempSync(join(tmpdir(), "opcua-pin-")), "server.pem");
    writeFileSync(path, cert);
    return path;
  }

  /** What the refusal says the bound is: to the second, as Python words it. */
  const second = (date) => new Date(Math.floor(date.getTime() / 1000) * 1000);
  const iso = (date) =>
    second(date)
      .toISOString()
      .replace(/\.\d{3}Z$/, "Z");

  it("has no problem while current", async () => {
    const path = await certificate(new Date(Date.now() - DAY), new Date(Date.now() + DAY));
    assert.equal(pinnedCertificateProblem(path), null);
  });

  it("is refused with its expiry once expired", async () => {
    const expiredAt = new Date(Date.now() - DAY);
    const path = await certificate(new Date(Date.now() - 30 * DAY), expiredAt);
    const problem = pinnedCertificateProblem(path);
    assert.ok(problem);
    assert.ok(problem.includes(`OPCUA_SERVER_CERT ${path} expired on ${iso(expiredAt)}`), problem);
  });

  it("is refused until it becomes valid", async () => {
    const starts = new Date(Date.now() + DAY);
    const path = await certificate(starts, new Date(Date.now() + 30 * DAY));
    const problem = pinnedCertificateProblem(path);
    assert.ok(problem);
    assert.ok(problem.includes(`not valid until ${iso(starts)}`), problem);
  });

  it("leaves an unreadable file to the library, which refuses it in its own words", () => {
    const path = join(mkdtempSync(join(tmpdir(), "opcua-pin-")), "server.pem");
    writeFileSync(path, "not a certificate");
    assert.equal(pinnedCertificateProblem(path), null);
  });
});
