import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { it } from "node:test";
import { fileURLToPath } from "node:url";

const entry = fileURLToPath(new URL("../build/index.js", import.meta.url));

for (const kind of ["audit", "policy"]) {
  it(`invalid ${kind} configuration exits before opening a plant connection`, async () => {
    const dir = mkdtempSync(join(tmpdir(), "opcua-startup-"));
    let connections = 0;
    const plant = createServer((socket) => {
      connections++;
      socket.destroy();
    });
    await new Promise((resolve, reject) => {
      plant.once("error", reject);
      plant.listen(0, "127.0.0.1", resolve);
    });
    try {
      const env = Object.fromEntries(
        Object.entries(process.env).filter(([key]) => !key.startsWith("OPCUA_"))
      );
      env.OPCUA_SERVER_URL = `opc.tcp://127.0.0.1:${plant.address().port}`;
      env.OPCUA_PROFILE = "operator";
      env.OPCUA_ALLOW_INSECURE_CONTROL = "true";
      env.OPCUA_ALLOWED_WRITE_NODES = "ns=2;i=13";
      const audit = join(dir, kind === "audit" ? "missing/audit.jsonl" : "audit.jsonl");
      env.OPCUA_AUDIT_FILE = audit;
      if (kind === "policy") {
        env.OPCUA_POLICY_FILE = join(dir, "policy.json");
        writeFileSync(env.OPCUA_POLICY_FILE, JSON.stringify({ control: "operator" }));
      }
      const { error, stdout, stderr } = await new Promise((resolve) => {
        execFile(
          process.execPath,
          [entry],
          { env, timeout: 15_000, killSignal: "SIGKILL" },
          (error, stdout, stderr) => resolve({ error, stdout, stderr })
        );
      });
      assert.equal(error?.code, 1, stderr);
      assert.equal(stdout, "");
      assert.match(stderr, /^Configuration error:/);
      assert.ok(stderr.includes(kind === "audit" ? "OPCUA_AUDIT_FILE" : "OPCUA_POLICY_FILE"));
      assert.doesNotMatch(stderr, /Server running|Connection resilience:/);
      assert.equal(connections, 0);
      assert.equal(existsSync(audit), false);
    } finally {
      await new Promise((resolve) => plant.close(resolve));
      rmSync(dir, { recursive: true, force: true });
    }
  });
}
