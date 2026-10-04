import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { it } from "node:test";
import { StatusCodes } from "node-opcua-client";
import { OpcuaConnection } from "../build/connection.js";
import { OpcuaTools } from "../build/tools.js";
import { message } from "../build/errors.js";
const cases = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/alarm-tool-alias.json", import.meta.url), "utf8")
).cases;
for (const c of cases)
  it(c.name, async () => {
    const calls = [];
    const conn = new OpcuaConnection();
    conn.session = {
      async browse() {
        return { references: [] };
      },
      async call(request) {
        calls.push(request);
        if (c.error) throw new Error("native refused");
        return { statusCode: StatusCodes[c.status] };
      },
    };
    const tools = new OpcuaTools(conn);
    const invoke = () => tools.dispatch(c.tool, c.arguments);
    if (c.error)
      await assert.rejects(invoke, (error) => {
        assert.equal(error.message, message(c.error, c.fields));
        return true;
      });
    else {
      const result = await invoke();
      assert.deepEqual(result.structuredContent.result, c.expected);
    }
    assert.equal(calls.length, 1);
    assert.equal(calls[0].objectId, c.arguments.condition_id);
    assert.equal(calls[0].inputArguments[0].value.toString("hex"), "01");
    assert.equal(calls[0].inputArguments[1].value.text, c.arguments.comment);
  });
