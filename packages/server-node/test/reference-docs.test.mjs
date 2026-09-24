// The generated reference docs and release-version copies (#149).
//
// config-schema.test.mjs already checks that every generated file is exactly
// what the generator writes. This module checks that the generator notices the
// kinds of drift #149 was opened for — a stale tool count, a missing variable, a
// deleted marker pair, a version bumped in one place — rather than only that the
// committed files happen to agree with it today.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import {
  loadSchema,
  normalizeEol,
  stampNpmLockfile,
  stampPyproject,
  stampUvLockfile,
} from "../scripts/config-artifacts.mjs";
import {
  DOC_TARGETS,
  loadTools,
  releaseVersion,
  renderDocument,
  repositoryUrl,
  slugify,
  targetPath,
} from "../scripts/reference-docs.mjs";

const SCHEMA = loadSchema();
const CONTRACT = loadTools();
const CONTEXT = {
  schema: SCHEMA,
  contract: CONTRACT,
  version: releaseVersion(),
  repoUrl: repositoryUrl(),
};

const README = DOC_TARGETS.find((target) => target.file === "README.md");
const readme = () => normalizeEol(readFileSync(targetPath(README), "utf8"));

describe("generated reference docs", () => {
  it("rewrites a stale tool count", () => {
    const current = readme();
    const count = `**${CONTRACT.tools.length} tools**`;
    assert.ok(current.includes(count), "the README states the contract's tool count");
    const stale = current.replace(count, "**13 tools**");
    // What `config:check` compares: the file as it is against what it should be.
    assert.notEqual(stale, renderDocument(README, stale, CONTEXT));
    assert.equal(renderDocument(README, stale, CONTEXT), current);
  });

  it("restores a variable deleted from a configuration table", () => {
    const current = readme();
    const row = current.split("\n").find((line) => line.startsWith("| `OPCUA_AUDIT_FILE` |"));
    assert.ok(row, "the README's table has an OPCUA_AUDIT_FILE row");
    const stale = current.replace(`${row}\n`, "");
    assert.equal(renderDocument(README, stale, CONTEXT), current);
  });

  it("documents every docs-surface setting, and only those a runtime reads", () => {
    for (const target of DOC_TARGETS) {
      const spec = target.blocks["config-reference"]?.config;
      if (!spec) continue;
      const text = normalizeEol(readFileSync(targetPath(target), "utf8"));
      for (const setting of SCHEMA.settings) {
        // A prettier-formatted table pads the cell.
        const documented = new RegExp(`^\\| \`${setting.env}\` +\\|`, "m").test(text);
        const expected =
          setting.surfaces.includes("docs") &&
          (!spec.runtime || setting.runtimes.includes(spec.runtime));
        assert.equal(documented, expected, `${target.file}: ${setting.env}`);
      }
    }
  });

  it("refuses a file whose marker pair was deleted", () => {
    const stale = readme().replace(/<!-- (BEGIN|END) GENERATED: tool-reference[^>]*-->\n?/g, "");
    assert.throws(
      () => renderDocument(README, stale, CONTEXT),
      /no generated tool-reference block/
    );
  });

  it("refuses a generated block the file does not declare", () => {
    const extra = `${readme()}\n<!-- BEGIN GENERATED: version --><!-- END GENERATED: version -->\n`;
    assert.throws(() => renderDocument(README, extra, CONTEXT), /unexpected generated block/);
  });

  it("refuses a tool docs/examples.md has no section for", () => {
    const examples = DOC_TARGETS.find((target) => target.file === "docs/examples.md");
    const text = normalizeEol(readFileSync(targetPath(examples), "utf8"));
    const withoutSection = text.replace(/^### `act_on_alarm`$/m, "### Acting on alarms");
    assert.throws(() => renderDocument(examples, withoutSection, CONTEXT), /act_on_alarm/);
  });

  it("links a package README absolutely and everything else relatively", () => {
    // A package README is read on npmjs.com and pypi.org, where a relative link
    // resolves against the registry rather than the repository.
    for (const target of DOC_TARGETS) {
      const text = readFileSync(targetPath(target), "utf8");
      const link = /\[`contract\/tools\.json`\]\(([^)]+)\)/.exec(text)?.[1];
      if (!link) continue;
      if (target.file.startsWith("packages/")) {
        assert.equal(link, `${CONTEXT.repoUrl}/blob/main/contract/tools.json`, target.file);
      } else {
        assert.ok(!link.includes("://"), `${target.file} links ${link}`);
      }
    }
  });

  it("computes heading anchors as GitHub does", () => {
    assert.equal(slugify("`read_opcua_history` (both servers)"), "read_opcua_history-both-servers");
    assert.equal(slugify("Resource: `opcua://subscriptions`"), "resource-opcuasubscriptions");
  });
});

describe("the release version has one source", () => {
  const next = "9.8.7";

  it("stamps a pyproject's [project] version and nothing else", () => {
    const text = '[project]\nname = "x"\nversion = "0.5.1"\n\n[tool.x]\nversion = "keep"\n';
    assert.equal(
      stampPyproject(text, next),
      '[project]\nname = "x"\nversion = "9.8.7"\n\n[tool.x]\nversion = "keep"\n'
    );
  });

  it("stamps both copies of the root version in package-lock.json", () => {
    const path = new URL("../package-lock.json", import.meta.url);
    const lock = JSON.parse(stampNpmLockfile(readFileSync(path, "utf8"), next));
    assert.equal(lock.version, next);
    assert.equal(lock.packages[""].version, next);
  });

  it("stamps the workspace's own packages in uv.lock, and no dependency", () => {
    const path = new URL("../../../uv.lock", import.meta.url);
    const before = normalizeEol(readFileSync(path, "utf8"));
    const after = stampUvLockfile(before, next);
    const changed = after.split("\n").filter((line, i) => line !== before.split("\n")[i]);
    assert.deepEqual(changed, [`version = "${next}"`, `version = "${next}"`]);
  });

  it("is idempotent, so a release that has already been stamped is not drift", () => {
    const text = '[project]\nversion = "0.5.1"\n';
    assert.equal(stampPyproject(stampPyproject(text, next), next), stampPyproject(text, next));
  });
});
