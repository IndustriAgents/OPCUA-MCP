import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { it } from "node:test";
import { OpcuaTools } from "../build/tools.js";
import { OpcuaConnection } from "../build/connection.js";
import { ToolPolicy, parsePolicyConfig } from "../build/policy.js";
import { ToolFailure } from "../build/errors.js";
const fixture = JSON.parse(
  readFileSync(
    new URL("../../../tests/fixtures/unexpected-tool-errors.json", import.meta.url),
    "utf8"
  )
);
const OPERATOR = {
  OPCUA_PROFILE: "operator",
  OPCUA_ALLOWED_WRITE_NODES: "ns=2;i=5",
  OPCUA_ALLOW_INSECURE_CONTROL: "true",
  OPCUA_ENFORCE_EU_RANGE: "false",
};
function harness(failure) {
  const conn = new OpcuaConnection();
  conn.ensureConnection = async () => {};
  const tools = new OpcuaTools(conn, new ToolPolicy(parsePolicyConfig(OPERATOR)));
  let attempts = 0;
  tools.dispatch = async () => {
    attempts++;
    throw failure;
  };
  tools.getServerStatus = tools.dispatch;
  return { tools, attempts: () => attempts };
}
for (const c of fixture.cases)
  it(c.tool + " crash exposes only the tool name", async () => {
    const { tools, attempts } = harness(new Error(c.internal));
    const records = [];
    const log = console.error;
    console.error = (line) => {
      if (typeof line === "string" && line.startsWith('{"event":"opcua_mcp_policy"'))
        records.push(JSON.parse(line));
    };
    try {
      const result = await tools.callTool({ params: { name: c.tool, arguments: c.arguments } });
      assert.equal(result.isError, true);
      assert.equal(result.content[0].text, c.public);
      assert.equal(attempts(), 1);
      if (c.tool === "write_opcua_nodes") {
        assert.deepEqual(
          records.map((r) => r.decision),
          ["allowed", "failed"]
        );
        assert.equal(records.at(-1).reason, c.public);
      }
    } finally {
      console.error = log;
    }
  });
it("anticipated failures keep their own message", async () => {
  const { tools } = harness(new ToolFailure(fixture.anticipated));
  const c = fixture.cases[0];
  const result = await tools.callTool({ params: { name: c.tool, arguments: c.arguments } });
  assert.equal(result.content[0].text, fixture.anticipated);
});
