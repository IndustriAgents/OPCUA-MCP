// Unit tests for the Node server's pure logic — no OPC UA server, no MCP
// transport, no build of the mock. Uses the built-in `node:test` runner so the
// package gains no dependency.
//
// Run: npm test   (requires `npm run build` first — these import build/index.js)
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test, { describe } from "node:test";
import { fileURLToPath } from "node:url";
import { existsSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import {
  AttributeIds,
  DataType,
  LocalizedText,
  MessageSecurityMode,
  NodeId,
  QualifiedName,
  SecurityPolicy,
  StatusCodes,
  UserTokenType,
  Variant,
  VariantArrayType,
  coerceNodeId,
} from "node-opcua-client";

import { toDate } from "../build/dates.js";
import { OpcuaConnection } from "../build/connection.js";
import {
  EVENT_DEFAULTS,
  droppedEventsMessage,
  eventSelectClauses,
  refreshTimedOutMessage,
  toEventRecord,
} from "../build/events.js";
import { toHistoryRecords, toIsoUtc, variantToJson } from "../build/records.js";
import {
  clientSecurityOptions,
  describeSecurity,
  parseSecurityConfig,
  securityWarnings,
  userIdentity,
} from "../build/security.js";
import {
  SubscriptionManager,
  resolveOptions,
  terminateFailedMessage,
  unknownSubscriptionMessage,
} from "../build/subscriptions.js";

describe("OpcuaConnection", () => {
  test("concurrent callers share one connection attempt", async () => {
    const connection = new OpcuaConnection();
    let attempts = 0;
    let release;
    connection.open = async () => {
      attempts += 1;
      await new Promise((resolve) => {
        release = resolve;
      });
    };

    const first = connection.connect();
    const second = connection.connect();
    assert.equal(attempts, 1);
    release();
    await Promise.all([first, second]);
    assert.equal(attempts, 1);
  });
});

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");

describe("toDate", () => {
  test("passes through undefined and null (both mean 'unset' over MCP)", () => {
    assert.equal(toDate(undefined), undefined);
    assert.equal(toDate(null), undefined);
  });

  test("returns Date instances unchanged", () => {
    const d = new Date("2026-04-23T17:40:00Z");
    assert.equal(toDate(d), d);
  });

  test("parses ISO-8601 strings, which is how MCP delivers timestamps", () => {
    assert.equal(toDate("2026-04-23T17:40:00Z").toISOString(), "2026-04-23T17:40:00.000Z");
  });

  test("preserves a non-UTC offset rather than reinterpreting the wall time", () => {
    assert.equal(toDate("2026-04-23T19:40:00+02:00").toISOString(), "2026-04-23T17:40:00.000Z");
  });

  // The Python server mirrors this wording verbatim so both runtimes reject bad
  // input identically; see _parse_iso_datetime in opcua_mcp_server.py.
  test("rejects malformed input with the wording shared with the Python server", () => {
    assert.throws(
      () => toDate("not-a-date"),
      (err) => {
        assert.equal(
          err.message,
          'Invalid date/time: "not-a-date". Use ISO 8601, e.g. 2026-04-23T17:40:00Z'
        );
        return true;
      }
    );
  });

  test("rejects an out-of-range month", () => {
    assert.throws(() => toDate("2026-13-01T00:00:00Z"), /Invalid date\/time/);
  });

  // Was a divergence from the Python server: V8 rolls an out-of-range day over
  // into the next month, so this silently returned data for 2026-03-02. Python's
  // datetime.fromisoformat rejects it, and now so does this.
  test("rejects an out-of-range day instead of rolling it over", () => {
    assert.throws(() => toDate("2026-02-30T00:00:00Z"), /Invalid date\/time/);
    assert.throws(() => toDate("2026-04-31T00:00:00Z"), /Invalid date\/time/);
  });

  test("accepts a real leap day and rejects a fake one", () => {
    assert.equal(toDate("2028-02-29T00:00:00Z").toISOString(), "2028-02-29T00:00:00.000Z");
    assert.throws(() => toDate("2026-02-29T00:00:00Z"), /Invalid date\/time/);
  });

  test("accepts a two-digit-looking year (no Date.UTC 1900s remapping)", () => {
    assert.equal(toDate("0026-01-15T00:00:00Z").toISOString(), "0026-01-15T00:00:00.000Z");
  });

  test("a timezone offset that shifts the UTC date is still accepted", () => {
    // 2026-03-01T01:00+02:00 is 2026-02-28T23:00Z — the UTC day differs from the
    // string's day, which must not be mistaken for an impossible date.
    assert.equal(toDate("2026-03-01T01:00:00+02:00").toISOString(), "2026-02-28T23:00:00.000Z");
  });
});

// The canonical history-family record shape (contract -> resultShapes.historyRecords).
// The Python equivalents are in tests/unit/test_records.py.
//
// The per-type value encoding is not restated here — it lives in
// tests/fixtures/value-encoding.json, which both suites read. Each side builds
// the *native* value for a case (that is the whole problem: a Buffer here,
// `bytes` there) and asserts the same JSON comes out.
const FIXTURE = JSON.parse(
  readFileSync(join(ROOT, "..", "..", "tests", "fixtures", "value-encoding.json"), "utf8")
);
const CASES = Object.fromEntries(FIXTURE.cases.map((c) => [c.name, c]));

const scalar = (dataType, value) => new Variant({ dataType, value });
// Int64/UInt64 need an explicit arrayType: their scalar form is itself a
// [high, low] pair, so node-opcua refuses to guess whether an array is one
// value or many. That ambiguity is exactly why the encoder keys on the variant.
const int64 = (dataType, value) =>
  new Variant({ dataType, arrayType: VariantArrayType.Scalar, value });
const array = (dataType, value) =>
  new Variant({ dataType, arrayType: VariantArrayType.Array, value });

// The native node-opcua value for each case in the fixture. The Python suite has
// its own table of the same names holding python-opcua values; the two produce
// the same JSON, which is the point.
const NATIVE = {
  boolean: scalar(DataType.Boolean, true),
  int32: scalar(DataType.Int32, 42),
  int64_small: int64(DataType.Int64, 5),
  int64_negative: int64(DataType.Int64, -5),
  int64_beyond_double: int64(DataType.Int64, [0x200000, 1]), // 2^53 + 1, exact only as a pair
  uint64_max: int64(DataType.UInt64, [0xffffffff, 0xffffffff]),
  double: scalar(DataType.Double, 51.75),
  double_nan: scalar(DataType.Double, NaN),
  double_infinity: scalar(DataType.Double, Infinity),
  string: scalar(DataType.String, "AUTO"),
  datetime: scalar(DataType.DateTime, new Date("2026-09-09T13:36:01.468Z")),
  guid: scalar(DataType.Guid, "72962B91-FA75-4AE6-8D28-B404DC7DAF63"),
  bytestring: scalar(DataType.ByteString, Buffer.from("abc")),
  nodeid: scalar(DataType.NodeId, coerceNodeId("ns=2;i=3")),
  nodeid_namespace_zero: scalar(DataType.NodeId, coerceNodeId("ns=0;i=2253")),
  statuscode: scalar(DataType.StatusCode, StatusCodes.Good),
  qualifiedname: scalar(
    DataType.QualifiedName,
    new QualifiedName({ name: "Temperature", namespaceIndex: 2 })
  ),
  localizedtext: scalar(DataType.LocalizedText, new LocalizedText({ text: "Ambient temperature" })),
  double_array: array(DataType.Double, [1.5, 2.5]),
  byte_array: array(DataType.Byte, [97, 98, 99]),
  int64_array: array(DataType.Int64, [5, [0x200000, 1]]),
  bytestring_array: array(DataType.ByteString, [Buffer.from("ab"), Buffer.from("c")]),
  empty_array: array(DataType.Double, []),
  null: scalar(DataType.Null, null),
};

describe("value encoding (shared fixture)", () => {
  test("every fixture case has a native value", () => {
    // A case with no entry above would pass by never being run.
    assert.deepEqual(Object.keys(NATIVE).sort(), Object.keys(CASES).sort());
  });

  for (const [name, expectation] of Object.entries(CASES)) {
    test(`${name} matches the shared fixture`, () => {
      const encoded = variantToJson(NATIVE[name]);
      if (expectation.expectedPattern) {
        assert.match(String(encoded), new RegExp(expectation.expectedPattern));
      } else {
        assert.deepEqual(encoded, expectation.expected);
      }
    });
  }

  // Regression guards for the divergences that prompted the shared fixture:
  // node-opcua splits an Int64 into a [high, low] pair and carries a ByteString
  // as a Buffer, neither of which resembles what python-opcua hands over.
  test("an Int64 is a number, not node-opcua's [high, low] pair", () => {
    assert.equal(variantToJson(NATIVE.int64_negative), -5);
  });

  test("a ByteString is base64, not a list of byte values", () => {
    assert.equal(variantToJson(NATIVE.bytestring), "YWJj");
  });

  test("a Byte array stays an array of numbers", () => {
    assert.deepEqual(variantToJson(NATIVE.byte_array), [97, 98, 99]);
  });
});

// The canonical record shape (contract -> resultShapes.historyRecords).
describe("history records", () => {
  /** A minimal DataValue stand-in — the fields toHistoryRecord actually reads. */
  const dataValue = (
    variant,
    { timestamp = new Date("2026-09-09T13:36:01.139Z"), status } = {}
  ) => ({
    value: variant,
    sourceTimestamp: timestamp,
    statusCode: status === undefined ? undefined : { name: status },
  });

  test("flattens a DataValue to {value, timestamp, status}", () => {
    assert.deepEqual(toHistoryRecords([dataValue(NATIVE.double, { status: "Good" })]), [
      { value: 51.75, timestamp: "2026-09-09T13:36:01.139Z", status: "Good" },
    ]);
  });

  test("an empty aggregate interval is null, not a stringified placeholder", () => {
    const [record] = toHistoryRecords([dataValue(undefined, { status: "BadNoData" })]);
    assert.equal(record.value, null);
    assert.equal(record.status, "BadNoData");
  });

  test("an absent status code means Good", () => {
    assert.equal(toHistoryRecords([dataValue(NATIVE.int32)])[0].status, "Good");
  });

  test("timestamps are ISO-8601 UTC with a trailing Z", () => {
    assert.equal(toIsoUtc(new Date("2026-09-09T15:36:01.468+02:00")), "2026-09-09T13:36:01.468Z");
    assert.equal(toIsoUtc(undefined), null);
    assert.equal(toIsoUtc(new Date("nope")), null);
  });

  test("no data values is an empty record list", () => {
    assert.deepEqual(toHistoryRecords(undefined), []);
    assert.deepEqual(toHistoryRecords([]), []);
  });
});

// --- connection security -------------------------------------------------------
// The Python server mirrors this configuration layer verbatim; the assertions
// below on shared error wording are duplicated in
// tests/unit/test_security_config.py so the two cannot drift.

// Pretend every configured path exists; path checking is covered separately.
const ALWAYS = () => true;
const CERTS = { OPCUA_CLIENT_CERT: "/pki/client.pem", OPCUA_CLIENT_KEY: "/pki/client.key" };
const parseSecurity = (env) => parseSecurityConfig(env, ALWAYS);

/** Asserts that parsing `env` fails with exactly `message`. */
function assertSecurityError(env, message, exists = ALWAYS) {
  assert.throws(
    () => parseSecurityConfig(env, exists),
    (err) => {
      assert.equal(err.message, message);
      return true;
    }
  );
}

describe("parseSecurityConfig", () => {
  test("defaults to no security, as the hardcoded behaviour did", () => {
    const config = parseSecurity({});
    assert.equal(config.policy, "None");
    assert.equal(config.mode, "None");
    assert.equal(config.username, undefined);
    assert.equal(securityWarnings(config).length, 1);
  });

  test("treats blank values as unset — MCP configs carry empty env entries", () => {
    const config = parseSecurity({ OPCUA_SECURITY_POLICY: "", OPCUA_SECURITY_MODE: "  " });
    assert.equal(config.policy, "None");
    assert.equal(config.mode, "None");
  });

  test("a policy alone implies SignAndEncrypt rather than a silent downgrade", () => {
    const config = parseSecurity({ OPCUA_SECURITY_POLICY: "Basic256Sha256", ...CERTS });
    assert.equal(config.policy, "Basic256Sha256");
    assert.equal(config.mode, "SignAndEncrypt");
    assert.deepEqual(securityWarnings(config), []);
  });

  test("keeps an explicit Sign mode", () => {
    const config = parseSecurity({
      OPCUA_SECURITY_POLICY: "Basic256",
      OPCUA_SECURITY_MODE: "Sign",
      ...CERTS,
    });
    assert.equal(config.mode, "Sign");
  });

  test("accepts the AES policies node-opcua implements", () => {
    const config = parseSecurity({ OPCUA_SECURITY_POLICY: "Aes256_Sha256_RsaPss", ...CERTS });
    assert.equal(config.policy, "Aes256_Sha256_RsaPss");
  });

  test("matches policy and mode names case-insensitively", () => {
    const config = parseSecurity({
      OPCUA_SECURITY_POLICY: "basic256sha256",
      OPCUA_SECURITY_MODE: "signandencrypt",
      ...CERTS,
    });
    assert.equal(config.policy, "Basic256Sha256");
    assert.equal(config.mode, "SignAndEncrypt");
  });

  test("rejects an unknown policy, listing the valid ones", () => {
    assertSecurityError(
      { OPCUA_SECURITY_POLICY: "Basic999" },
      'Invalid OPCUA_SECURITY_POLICY: "Basic999". Use one of: None, Basic128Rsa15, Basic256, ' +
        "Basic256Sha256, Aes128_Sha256_RsaOaep, Aes256_Sha256_RsaPss"
    );
  });

  // Wording shared with the Python server.
  test("rejects an unknown mode", () => {
    assertSecurityError(
      { OPCUA_SECURITY_MODE: "Encrypt" },
      'Invalid OPCUA_SECURITY_MODE: "Encrypt". Use one of: None, Sign, SignAndEncrypt'
    );
  });

  test("rejects a mode without a policy — SecurityPolicy#None only pairs with None", () => {
    assertSecurityError(
      { OPCUA_SECURITY_MODE: "SignAndEncrypt" },
      "OPCUA_SECURITY_MODE=SignAndEncrypt requires OPCUA_SECURITY_POLICY to be set to a policy " +
        "other than None"
    );
  });

  test("rejects a policy combined with mode None", () => {
    assertSecurityError(
      { OPCUA_SECURITY_POLICY: "Basic256Sha256", OPCUA_SECURITY_MODE: "None", ...CERTS },
      "OPCUA_SECURITY_POLICY=Basic256Sha256 cannot be combined with OPCUA_SECURITY_MODE=None; " +
        "use Sign or SignAndEncrypt"
    );
  });

  test("requires a certificate and key for a secure policy", () => {
    const message =
      "OPCUA_SECURITY_POLICY=Basic256Sha256 requires OPCUA_CLIENT_CERT and OPCUA_CLIENT_KEY " +
      "(paths to the client certificate and its private key)";
    for (const partial of [
      {},
      { OPCUA_CLIENT_CERT: "/pki/client.pem" },
      { OPCUA_CLIENT_KEY: "/k" },
    ]) {
      assertSecurityError({ OPCUA_SECURITY_POLICY: "Basic256Sha256", ...partial }, message);
    }
  });

  test("reports a missing certificate file instead of failing in the crypto layer", () => {
    const dir = mkdtempSync(join(tmpdir(), "opcua-mcp-pki-"));
    const key = join(dir, "client.key");
    const missing = join(dir, "client.pem");
    writeFileSync(key, "key");
    assertSecurityError(
      {
        OPCUA_SECURITY_POLICY: "Basic256Sha256",
        OPCUA_CLIENT_CERT: missing,
        OPCUA_CLIENT_KEY: key,
      },
      `OPCUA_CLIENT_CERT does not exist: ${missing}`,
      existsSync // the real check, not the stub the other cases use
    );
  });

  test("accepts certificate files that exist", () => {
    const dir = mkdtempSync(join(tmpdir(), "opcua-mcp-pki-"));
    const cert = join(dir, "client.pem");
    const key = join(dir, "client.key");
    writeFileSync(cert, "cert");
    writeFileSync(key, "key");
    const config = parseSecurityConfig({
      OPCUA_SECURITY_POLICY: "Basic256Sha256",
      OPCUA_CLIENT_CERT: cert,
      OPCUA_CLIENT_KEY: key,
    });
    assert.equal(config.clientCert, cert);
    assert.equal(config.clientKey, key);
  });

  test("carries an application URI through, since servers match it to the certificate", () => {
    assert.equal(parseSecurity({}).applicationUri, undefined);
    assert.equal(
      parseSecurity({ OPCUA_APPLICATION_URI: "urn:plant:mcp-client", ...CERTS }).applicationUri,
      "urn:plant:mcp-client"
    );
  });

  test("reads credentials together", () => {
    const config = parseSecurity({ OPCUA_USERNAME: "operator", OPCUA_PASSWORD: "hunter2" });
    assert.equal(config.username, "operator");
    assert.equal(config.password, "hunter2");
  });

  test("an empty password is an explicit credential, not an unset variable", () => {
    assert.equal(parseSecurity({ OPCUA_USERNAME: "operator", OPCUA_PASSWORD: "" }).password, "");
  });

  test("rejects half a credential", () => {
    assertSecurityError({ OPCUA_USERNAME: "operator" }, "OPCUA_USERNAME requires OPCUA_PASSWORD");
    assertSecurityError({ OPCUA_PASSWORD: "hunter2" }, "OPCUA_PASSWORD requires OPCUA_USERNAME");
  });

  // An empty password *is* a credential when paired with a username (above), but
  // an empty one on its own is how "unset" arrives from an MCP client config —
  // those routinely carry empty `env` entries — and from an MCP bundle, where
  // every optional `user_config` field substitutes as an empty string. Treating
  // it as a usage error made an anonymous connection impossible to express in the
  // bundle: it refused to start until a username *and* password were typed in.
  test("blank credentials mean anonymous, not half a credential", () => {
    const both = parseSecurity({ OPCUA_USERNAME: "", OPCUA_PASSWORD: "" });
    assert.equal(both.username, undefined);
    assert.equal(both.password, undefined);
    assert.equal(parseSecurity({ OPCUA_PASSWORD: "" }).password, undefined);
  });
});

describe("security wiring", () => {
  test("maps the configuration onto node-opcua client options", () => {
    const options = clientSecurityOptions(
      parseSecurity({ OPCUA_SECURITY_POLICY: "Basic256Sha256", ...CERTS })
    );
    assert.equal(options.securityPolicy, SecurityPolicy.Basic256Sha256);
    assert.equal(options.securityMode, MessageSecurityMode.SignAndEncrypt);
    assert.equal(options.certificateFile, CERTS.OPCUA_CLIENT_CERT);
    assert.equal(options.privateKeyFile, CERTS.OPCUA_CLIENT_KEY);
    assert.equal("applicationUri" in options, false);
  });

  test("passes the application URI through when one is configured", () => {
    const options = clientSecurityOptions(
      parseSecurity({
        OPCUA_SECURITY_POLICY: "Basic256Sha256",
        OPCUA_APPLICATION_URI: "urn:plant:mcp-client",
        ...CERTS,
      })
    );
    assert.equal(options.applicationUri, "urn:plant:mcp-client");
  });

  test("leaves the certificate options off entirely when unsecured", () => {
    const options = clientSecurityOptions(parseSecurity({}));
    assert.equal(options.securityPolicy, SecurityPolicy.None);
    assert.equal(options.securityMode, MessageSecurityMode.None);
    assert.equal("certificateFile" in options, false);
    assert.equal("privateKeyFile" in options, false);
  });

  test("logs in anonymously when no username is configured", () => {
    assert.deepEqual(userIdentity(parseSecurity({})), { type: UserTokenType.Anonymous });
  });

  test("logs in with the configured credentials", () => {
    const identity = userIdentity(
      parseSecurity({ OPCUA_USERNAME: "operator", OPCUA_PASSWORD: "hunter2" })
    );
    assert.deepEqual(identity, {
      type: UserTokenType.UserName,
      userName: "operator",
      password: "hunter2",
    });
  });

  test("warns whenever the channel is unencrypted, credentials or not", () => {
    // A username authenticates the session; it does not encrypt anything.
    const warnings = securityWarnings(
      parseSecurity({ OPCUA_USERNAME: "operator", OPCUA_PASSWORD: "hunter2" })
    );
    assert.ok(warnings.some((warning) => warning.includes("traffic is unencrypted")));
    assert.equal(warnings.join(" ").includes("hunter2"), false);
  });

  test("warns that credentials cross that unencrypted channel in clear text", () => {
    const withUser = securityWarnings(
      parseSecurity({ OPCUA_USERNAME: "operator", OPCUA_PASSWORD: "hunter2" })
    );
    assert.ok(withUser.some((warning) => warning.includes("clear text")));
    // ...and that extra warning is specific to having credentials configured.
    const anonymous = securityWarnings(parseSecurity({}));
    assert.equal(
      anonymous.some((warning) => warning.includes("clear text")),
      false
    );
  });

  test("a secured channel warns about nothing", () => {
    const config = parseSecurity({
      OPCUA_SECURITY_POLICY: "Basic256Sha256",
      OPCUA_USERNAME: "operator",
      OPCUA_PASSWORD: "hunter2",
      ...CERTS,
    });
    assert.deepEqual(securityWarnings(config), []);
  });

  test("the startup summary never carries the password", () => {
    const described = describeSecurity(
      parseSecurity({
        OPCUA_SECURITY_POLICY: "Basic256Sha256",
        OPCUA_USERNAME: "operator",
        OPCUA_PASSWORD: "hunter2",
        ...CERTS,
      })
    );
    assert.equal(described, 'policy=Basic256Sha256 mode=SignAndEncrypt user="operator"');
    assert.equal(described.includes("hunter2"), false);
  });

  test("summarises the default connection", () => {
    assert.equal(describeSecurity(parseSecurity({})), "policy=None mode=None user=anonymous");
  });
});

// The Python server resolves the same defaults in `resolve_options`, and its
// unit suite pins the same numbers: they are reported back to the agent in every
// subscription record, so a difference between the runtimes is visible drift.
describe("subscription options", () => {
  test("defaults an omitted request the way the contract documents", () => {
    assert.deepEqual(resolveOptions({}), {
      publishingInterval: 1000,
      samplingInterval: 1000,
      bufferSize: 20,
    });
  });

  test("sampling_interval 0 means 'sample at the publishing interval'", () => {
    assert.equal(resolveOptions({ publishingInterval: 250 }).samplingInterval, 250);
    assert.equal(
      resolveOptions({ publishingInterval: 250, samplingInterval: 0 }).samplingInterval,
      250
    );
  });

  test("keeps a sampling interval faster than the publishing one", () => {
    const { publishingInterval, samplingInterval } = resolveOptions({
      publishingInterval: 1000,
      samplingInterval: 100,
    });
    assert.equal(publishingInterval, 1000);
    assert.equal(samplingInterval, 100);
  });

  test("clamps a publishing interval nobody's OPC UA server would honour", () => {
    assert.equal(resolveOptions({ publishingInterval: 0 }).publishingInterval, 50);
    assert.equal(resolveOptions({ publishingInterval: -100 }).publishingInterval, 50);
  });

  test("clamps the buffer so one subscription cannot grow without bound", () => {
    assert.equal(resolveOptions({ bufferSize: 0 }).bufferSize, 1);
    assert.equal(resolveOptions({ bufferSize: 10_000 }).bufferSize, 1000);
    assert.equal(resolveOptions({ bufferSize: 7.9 }).bufferSize, 7);
  });

  test("falls back on a non-number, which MCP arguments can always be", () => {
    assert.deepEqual(resolveOptions({ publishingInterval: NaN, bufferSize: undefined }), {
      publishingInterval: 1000,
      samplingInterval: 1000,
      bufferSize: 20,
    });
  });
});

describe("SubscriptionManager teardown", () => {
  // A stand-in for node-opcua's ClientSubscription: `terminate` is the only part
  // of it the teardown path touches.
  function fakeSession(terminated) {
    return {
      async createSubscription2() {
        const subscription = {
          async terminate() {
            terminated.push(subscription);
          },
          async monitor() {
            return { on() {}, statusCode: StatusCodes.Good };
          },
        };
        return subscription;
      },
    };
  }

  test("closeAll terminates every subscription and empties the list", async () => {
    const terminated = [];
    const manager = new SubscriptionManager();
    await manager.subscribe(fakeSession(terminated), "ns=2;i=3");
    await manager.subscribe(fakeSession(terminated), "ns=2;i=4");
    assert.deepEqual(
      manager.list().map((r) => r.subscription_id),
      ["sub-1", "sub-2"]
    );

    await manager.closeAll();

    assert.equal(terminated.length, 2);
    assert.deepEqual(manager.list(), []);
  });

  test("unsubscribe terminates one and leaves the rest", async () => {
    const terminated = [];
    const manager = new SubscriptionManager();
    await manager.subscribe(fakeSession(terminated), "ns=2;i=3");
    await manager.subscribe(fakeSession(terminated), "ns=2;i=4");

    const record = await manager.unsubscribe("sub-1");

    assert.equal(record.node_id, "ns=2;i=3");
    assert.equal(terminated.length, 1);
    assert.deepEqual(
      manager.list().map((r) => r.subscription_id),
      ["sub-2"]
    );
  });

  // The stand-in whose terminate always fails, used by the two tests below.
  function refusingSession() {
    return {
      async createSubscription2() {
        return {
          async terminate() {
            throw new Error("session closed");
          },
          async monitor() {
            return { on() {}, statusCode: StatusCodes.Good };
          },
        };
      },
    };
  }

  // An explicit cancel reports what shutdown is right to swallow: the caller
  // asked for something specific, did not fully get it, and no longer holds an
  // ID to retry with. The Python server raises the same sentence.
  test("unsubscribe surfaces a refused terminate", async () => {
    const manager = new SubscriptionManager();
    await manager.subscribe(refusingSession(), "ns=2;i=3");

    await assert.rejects(() => manager.unsubscribe("sub-1"), {
      message: terminateFailedMessage("sub-1", "session closed"),
    });
    assert.deepEqual(manager.list(), [], "the refused terminate left the entry behind");
    assert.equal(
      terminateFailedMessage("sub-1", "session closed"),
      "Cancelled sub-1 here, but the OPC UA server did not accept the delete: session closed. " +
        "It may keep publishing until the subscription's lifetime expires."
    );
  });

  // Shutdown runs against an OPC UA server that has often already dropped the
  // session, so a failing terminate must not turn a tidy exit into a crash.
  test("closeAll survives a subscription that refuses to terminate", async () => {
    const manager = new SubscriptionManager();
    await manager.subscribe(refusingSession(), "ns=2;i=3");

    await manager.closeAll();
    assert.deepEqual(manager.list(), []);
  });

  test("an unknown id is refused with the wording shared with the Python server", async () => {
    const manager = new SubscriptionManager();
    await assert.rejects(() => manager.unsubscribe("sub-9"), {
      message: unknownSubscriptionMessage("sub-9"),
    });
    assert.equal(unknownSubscriptionMessage("sub-9"), "No such subscription: sub-9");
  });
});

// Events and Alarms & Conditions (contract -> events / resultShapes.eventRecords).
// The Python server's equivalents are asserted in tests/unit/test_events.py; the
// two runtimes are compared against each other, live, in tests/e2e.
describe("event filter", () => {
  const CONTRACT = JSON.parse(readFileSync(join(ROOT, "build", "contract.json"), "utf8"));
  const FIELDS = CONTRACT.events.fields;
  const KEYS = FIELDS.map((field) => field.key);

  test("selects one clause per contract field, in the record's own order", () => {
    assert.equal(eventSelectClauses().length, FIELDS.length);
  });

  test("ConditionId is the NodeId attribute of the condition, not a browse path", () => {
    // Part 9's exception: selecting it as a path returns null from every server,
    // and acknowledge_alarm then has nothing to call the Acknowledge method on.
    const clause = eventSelectClauses()[KEYS.indexOf("condition_id")];
    assert.deepEqual(clause.browsePath, []);
    assert.equal(clause.attributeId, AttributeIds.NodeId);
    assert.equal(clause.typeDefinitionId.toString(), CONTRACT.events.conditionTypeNodeId);
  });

  test("every other field is a Value browse path resolved against BaseEventType", () => {
    // Part 4 §7.4.4.5 — which is what lets one filter select `AckedState/Id`
    // from a condition and get null, not an error, from a plain event.
    for (const key of KEYS.filter((k) => k !== "condition_id")) {
      const clause = eventSelectClauses()[KEYS.indexOf(key)];
      assert.equal(clause.attributeId, AttributeIds.Value);
      assert.equal(clause.typeDefinitionId.toString(), CONTRACT.events.baseEventTypeNodeId);
      assert.deepEqual(
        clause.browsePath.map((name) => name.name),
        FIELDS[KEYS.indexOf(key)].path.split(".")
      );
    }
  });

  test("a two-step path stays two qualified names", () => {
    // `AckedState.Id` is the boolean; `AckedState` alone is the display text.
    const clause = eventSelectClauses()[KEYS.indexOf("acked")];
    assert.deepEqual(
      clause.browsePath.map((name) => name.name),
      ["AckedState", "Id"]
    );
  });
});

describe("event records", () => {
  const CONTRACT = JSON.parse(readFileSync(join(ROOT, "build", "contract.json"), "utf8"));
  const KEYS = CONTRACT.events.fields.map((field) => field.key);
  const NULL = new Variant({ dataType: DataType.Null, value: null });

  /** Event field values, in the contract's order; anything unnamed is Null. */
  const fields = (values) => KEYS.map((key) => values[key] ?? NULL);

  const CONDITION = {
    event_id: scalar(DataType.ByteString, Buffer.from([1, 2])),
    event_type: scalar(DataType.NodeId, coerceNodeId("ns=0;i=9341")),
    source_node: scalar(DataType.NodeId, coerceNodeId("ns=1;i=1001")),
    source_name: scalar(DataType.String, "Temperature"),
    time: scalar(DataType.DateTime, new Date("2026-09-09T13:36:01.468Z")),
    message: scalar(DataType.LocalizedText, new LocalizedText({ text: "Condition is High" })),
    severity: scalar(DataType.UInt16, 700),
    condition_id: scalar(DataType.NodeId, coerceNodeId("ns=1;i=1002")),
    condition_name: scalar(DataType.String, "HighTemperatureAlarm"),
    active: scalar(DataType.Boolean, true),
    acked: scalar(DataType.Boolean, false),
    retain: scalar(DataType.Boolean, true),
  };

  test("a condition event maps to the canonical record", () => {
    assert.deepEqual(toEventRecord(fields(CONDITION)), {
      event_id: "AQI=",
      event_type: "ns=0;i=9341",
      source_node: "ns=1;i=1001",
      source_name: "Temperature",
      time: "2026-09-09T13:36:01.468Z",
      message: "Condition is High",
      severity: 700,
      condition_id: "ns=1;i=1002",
      condition_name: "HighTemperatureAlarm",
      active: true,
      acked: false,
      retain: true,
    });
  });

  test("a plain event still carries every field, as null", () => {
    // Null, not absent: one record shape has to describe both kinds of event.
    const record = toEventRecord(
      fields({ event_type: scalar(DataType.NodeId, coerceNodeId("ns=0;i=2041")) })
    );
    assert.deepEqual(Object.keys(record), KEYS);
    assert.equal(record.event_type, "ns=0;i=2041");
    assert.equal(record.condition_id, null);
    assert.equal(record.acked, null);
  });

  // Both sentences are asserted verbatim in tests/unit/test_events.py too, so
  // neither runtime can drift into wording the other does not use.
  test("an unfinished ConditionRefresh is worded the way Python words it", () => {
    assert.equal(
      refreshTimedOutMessage(5, 2),
      "ConditionRefresh did not finish within 5s: the server sent 2 condition(s) " +
        "but no RefreshEnd, so there may be more. Retry with a larger timeout_seconds."
    );
  });

  test("a dropped-event notice is worded the way Python words it", () => {
    assert.equal(
      droppedEventsMessage(1, 2),
      "Note: 1 older event(s) were dropped before this read — the buffer of 2 " +
        "filled up. Raise buffer_size or read more often."
    );
  });

  test("the defaults are the ones the contract promises", () => {
    // The tool descriptions state these numbers; both servers read them here.
    assert.deepEqual(EVENT_DEFAULTS, {
      $comment: EVENT_DEFAULTS.$comment,
      severityMin: 0,
      bufferSize: 100,
      readLimit: 50,
      refreshTimeoutSeconds: 5,
    });
  });
});

describe("build assets", () => {
  test("version.json matches package.json", () => {
    const pkg = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf8"));
    const built = JSON.parse(readFileSync(join(ROOT, "build", "version.json"), "utf8"));
    assert.equal(built.version, pkg.version);
  });

  test("build/contract.json is byte-identical to the canonical contract", () => {
    const canonical = readFileSync(join(ROOT, "..", "..", "contract", "tools.json"), "utf8");
    const staged = readFileSync(join(ROOT, "build", "contract.json"), "utf8");
    assert.equal(staged, canonical, "build/contract.json drifted from contract/tools.json");
  });
});
