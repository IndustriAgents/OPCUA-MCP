import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { it } from "node:test";
import { OpcuaTools } from "../build/tools.js";
import { OpcuaConnection } from "../build/connection.js";
const cases = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/request-limits.json", import.meta.url), "utf8")
).requests.filter((c) => c.error?.includes("Unicode surrogate"));
for (const c of cases)
  it(c.name + " at the actual invocation boundary", async () => {
    const conn = new OpcuaConnection();
    let connections = 0,
      calls = 0;
    conn.ensureConnection = async () => {
      connections++;
    };
    const tools = new OpcuaTools(conn);
    tools.dispatch = async () => {
      calls++;
      throw new Error("must not invoke a feature");
    };
    const log = console.error;
    console.error = () => {};
    try {
      const result = await tools.callTool({ params: { name: c.tool, arguments: c.arguments } });
      assert.equal(result.isError, true);
      assert.equal(result.content[0].text, c.error);
      assert.equal(calls, 0);
      assert.equal(connections, 0);
    } finally {
      console.error = log;
    }
  });
