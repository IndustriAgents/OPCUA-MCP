import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { it } from "node:test";
import { OPCUAClient } from "node-opcua-client";
import { CLIENT_APPLICATION_NAME, applicationUriProblem } from "../build/client-identity.js";
import {
  certificateApplicationUri,
  clientSecurityOptions,
  parseSecurityConfig,
} from "../build/security.js";
import { OpcuaConnection } from "../build/connection.js";
import { reconnectConfig } from "../build/config.js";
const fixture = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/client-identity.json", import.meta.url), "utf8")
);
for (const c of fixture.cases)
  it(c.name, () => assert.equal(applicationUriProblem(c.configured, c.certificateUri), c.problem));
it("reads a real certificate and refuses conflicting identities before connecting", () => {
  const cert = new URL("../../../" + fixture.certificate, import.meta.url).pathname;
  assert.equal(certificateApplicationUri(cert), fixture.certificateUri);
  const config = parseSecurityConfig({
    OPCUA_CLIENT_CERT: cert,
    OPCUA_CLIENT_KEY: cert,
    OPCUA_APPLICATION_URI: "urn:plant:other",
  });
  assert.throws(() => clientSecurityOptions(config), { message: fixture.cases.at(-1).problem });
});
it("requests the shared application name and both lifetimes before connecting", async () => {
  const create = OPCUAClient.create;
  let options;
  OPCUAClient.create = (provided) => {
    options = provided;
    throw new Error("stop before opening a socket");
  };
  const log = console.error;
  console.error = () => {};
  try {
    const conn = new OpcuaConnection("opc.tcp://localhost:4840", undefined, {
      ...reconnectConfig(),
      sessionTimeout: fixture.sessionTimeoutMs,
      maxRetry: 0,
    });
    await assert.rejects(() => conn.ensureConnection(), /stop before opening a socket/);
    assert.equal(options.applicationName, CLIENT_APPLICATION_NAME);
    assert.equal(options.applicationName, fixture.applicationName);
    assert.equal(options.requestedSessionTimeout, fixture.sessionTimeoutMs);
    assert.equal(options.defaultSecureTokenLifetime, fixture.secureChannelLifetimeMs);
  } finally {
    OPCUAClient.create = create;
    console.error = log;
  }
});
