import assert from "node:assert/strict";
import { X509Certificate } from "node:crypto";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { parseSecurityConfig } from "../build/security.js";
import { certificateProblem } from "../build/trust-store.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const fixtures = join(root, "tests/fixtures/trust-store");
const table = JSON.parse(readFileSync(join(root, "tests/fixtures/trust-store-cases.json"), "utf8"));
function populate(store, row) {
  for (const [group, folder] of [
    ["anchors", "trusted/certs"],
    ["issuers", "issuers/certs"],
  ]) {
    const directory = join(store, folder);
    mkdirSync(directory, { recursive: true });
    for (const name of row[group]) copyFileSync(join(fixtures, name), join(directory, name));
  }
  for (const name of row.crls) {
    const directory = join(store, name.startsWith("root-") ? "trusted/crl" : "issuers/crl");
    mkdirSync(directory, { recursive: true });
    copyFileSync(join(fixtures, name), join(directory, name));
  }
}

for (const row of table.cases) {
  test(`CA/CRL shared fixture: ${row.id}`, async () => {
    const store = mkdtempSync(join(tmpdir(), "opcua-trust-"));
    try {
      populate(store, row);
      const certificate = new X509Certificate(readFileSync(join(fixtures, row.certificate))).raw;
      assert.equal(
        await certificateProblem(
          store,
          certificate,
          {
            applicationUri: row.applicationUri,
            advertisedUri: row.advertisedUri,
            endpoint: row.endpoint,
          },
          new Date(table.now)
        ),
        row.expected
      );
    } finally {
      rmSync(store, { recursive: true, force: true });
    }
  });
}

for (const mode of ["oversized-file", "too-many-files", "crl-reload"]) {
  test(`trust material: ${mode}`, async () => {
    const store = mkdtempSync(join(tmpdir(), "opcua-trust-bounds-"));
    const row = table.cases[0];
    const certificate = new X509Certificate(readFileSync(join(fixtures, row.certificate))).raw;
    try {
      populate(store, row);
      const check = () =>
        certificateProblem(
          store,
          certificate,
          {
            applicationUri: row.applicationUri,
            advertisedUri: row.advertisedUri,
            endpoint: row.endpoint,
          },
          new Date(table.now)
        );
      assert.equal(await check(), null);
      if (mode === "oversized-file") {
        writeFileSync(join(store, "trusted/certs/huge.pem"), Buffer.alloc(1024 * 1024 + 1));
      } else if (mode === "too-many-files") {
        for (let index = 0; index < 100; index++) {
          copyFileSync(join(fixtures, "root.pem"), join(store, `trusted/certs/${index}.pem`));
        }
      } else {
        copyFileSync(
          join(fixtures, "issuer-revoked-leaf.crl"),
          join(store, "issuers/crl/issuer-current.crl")
        );
      }
      assert.equal(
        await check(),
        mode === "crl-reload" ? "BadCertificateRevoked" : "BadCertificateInvalid"
      );
    } finally {
      rmSync(store, { recursive: true, force: true });
    }
  });
}

for (const row of table.configuration) {
  test(`invalid trust configuration: ${row.id}`, () => {
    assert.throws(() => parseSecurityConfig(row.env, () => true), { message: row.error });
  });
}
