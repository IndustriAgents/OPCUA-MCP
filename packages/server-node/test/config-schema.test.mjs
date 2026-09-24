// The canonical configuration schema, from the Node side (#133).
//
// `tests/unit/test_config_schema.py` is the Python half: it checks the schema's
// shape and safety invariants, scans both runtimes' source for the variables
// they read, and drives the Python parsers through the cases below. This module
// drives the Node parsers through the same cases, derived from the same file,
// and checks that the committed metadata is exactly what the generator writes.
//
// Run: npm test   (requires `npm run build` first — these import build/*.js)
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { relative } from "node:path";
import { describe, it } from "node:test";

import { parseReconnectConfig } from "../build/config.js";
import { configSchema } from "../build/contract.js";
import { parsePolicyConfig } from "../build/policy.js";
import { parseSecurityConfig } from "../build/security.js";
import {
  REPO_ROOT,
  SCHEMA_PATH,
  loadSchema,
  mcpbField,
  registryVariable,
  renderAll,
} from "../scripts/config-artifacts.mjs";

const SCHEMA = loadSchema();

describe("generated configuration metadata", () => {
  it("is exactly what the generator writes from the schema", async () => {
    for (const [path, expected, current] of await renderAll(SCHEMA)) {
      assert.equal(
        current,
        expected,
        `${relative(REPO_ROOT, path)} is stale: run \`npm run config:generate\``
      );
    }
  });

  it("masks every sensitive setting and embeds no secret value", () => {
    for (const setting of SCHEMA.settings) {
      const field = mcpbField(setting);
      const variable = registryVariable(setting);
      assert.equal(field.sensitive === true, setting.sensitive, setting.env);
      assert.equal(variable.isSecret === true, setting.sensitive, setting.env);
      if (setting.secret) {
        assert.equal(field.default, undefined, setting.env);
        assert.equal(variable.default, undefined, setting.env);
        assert.equal(variable.placeholder, undefined, setting.env);
      }
    }
  });

  it("ships the canonical schema in the build", () => {
    assert.deepEqual(configSchema(), JSON.parse(readFileSync(SCHEMA_PATH, "utf8")));
  });
});

// --- the Node parsers agree with the schema -----------------------------------

const exists = () => true;
const secured = (env) => ({
  OPCUA_CLIENT_CERT: "client.pem",
  OPCUA_CLIENT_KEY: "client.key",
  ...env,
});

/** How to observe each typed setting through its parser. Mirrors PROBES in
 *  test_config_schema.py; the first test below fails if the schema outgrows it. */
const PROBES = {
  OPCUA_SECURITY_POLICY: (v) =>
    parseSecurityConfig(secured({ OPCUA_SECURITY_POLICY: v }), exists).policy,
  OPCUA_SECURITY_MODE: (v) =>
    // A mode is only valid beside a policy that agrees with it.
    parseSecurityConfig(
      secured({
        OPCUA_SECURITY_POLICY: v.trim().toLowerCase() === "none" ? "None" : "Basic256Sha256",
        OPCUA_SECURITY_MODE: v,
      }),
      exists
    ).mode,
  OPCUA_PROFILE: (v) => parsePolicyConfig({ OPCUA_PROFILE: v }).profile,
  OPCUA_ALLOW_ACKNOWLEDGE_ALARMS: (v) =>
    parsePolicyConfig({ OPCUA_ALLOW_ACKNOWLEDGE_ALARMS: v }).acknowledgeAlarms,
  OPCUA_ALLOW_INSECURE_CONTROL: (v) =>
    parsePolicyConfig({ OPCUA_ALLOW_INSECURE_CONTROL: v }).allowInsecureControl,
  OPCUA_ALLOW_OUT_OF_RANGE_WRITES: (v) =>
    parsePolicyConfig({ OPCUA_ALLOW_OUT_OF_RANGE_WRITES: v }).allowOutOfRangeWrites,
  OPCUA_RECONNECT_INITIAL_DELAY_MS: (v) =>
    parseReconnectConfig({ OPCUA_RECONNECT_INITIAL_DELAY_MS: v }).initialDelay,
  OPCUA_RECONNECT_MAX_DELAY_MS: (v) =>
    parseReconnectConfig({ OPCUA_RECONNECT_MAX_DELAY_MS: v }).maxDelay,
  OPCUA_RECONNECT_MAX_RETRY: (v) => parseReconnectConfig({ OPCUA_RECONNECT_MAX_RETRY: v }).maxRetry,
  OPCUA_SESSION_TIMEOUT_MS: (v) =>
    parseReconnectConfig({ OPCUA_SESSION_TIMEOUT_MS: v }).sessionTimeout,
};

const TYPED = SCHEMA.settings.filter(
  (s) => ["enum", "boolean", "number"].includes(s.type) && s.runtimes.includes("node")
);

/** [raw, expected] pairs to accept, and raw values to refuse — the same cases
 *  `_cases` builds in test_config_schema.py. */
function cases(setting) {
  if (setting.type === "enum") {
    const allowed = setting.runtimeChoices?.node ?? setting.choices;
    const accept = [
      ...allowed.map((c) => [c, c]),
      ...allowed.map((c) => [c.toUpperCase(), c]),
      ...Object.entries(setting.choiceAliases ?? {}),
    ];
    return [accept, ["not-a-choice", ...setting.choices.filter((c) => !allowed.includes(c))]];
  }
  if (setting.type === "boolean") {
    const values = SCHEMA.booleanValues;
    const accept = [
      ...values.true.map((v) => [v, true]),
      ...values.false.map((v) => [v, false]),
      ...values.true.map((v) => [v.toUpperCase(), true]),
    ];
    return [accept, ["maybe", "2"]];
  }
  const min = setting.minimum;
  return [
    [
      [String(min), min],
      [String(min + 0.5), min + 0.5],
    ],
    [String(min - 1), "abc", "inf"],
  ];
}

describe("the Node parsers agree with contract/config.json", () => {
  it("has a probe for every typed setting", () => {
    assert.deepEqual(Object.keys(PROBES).sort(), TYPED.map((s) => s.env).sort());
  });

  for (const setting of TYPED) {
    const probe = PROBES[setting.env];
    const [accept, refuse] = cases(setting);

    it(`${setting.env} accepts what the schema declares`, () => {
      for (const [raw, expected] of accept) {
        assert.equal(probe(raw), expected, `${setting.env}=${raw}`);
      }
    });

    it(`${setting.env} refuses what the schema does not declare`, () => {
      for (const raw of refuse) {
        assert.throws(() => probe(raw), undefined, `${setting.env}=${raw}`);
      }
      // Named in the message, so an operator knows which line to fix.
      assert.throws(() => probe(refuse[0]), new RegExp(setting.env));
    });

    if (setting.default !== null) {
      it(`${setting.env} reads blank as the declared default`, () => {
        for (const blank of ["", "   "]) {
          assert.equal(probe(blank), setting.default);
        }
      });
    }
  }
});
