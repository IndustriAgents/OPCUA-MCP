// Renders the reference parts of the Markdown documentation from the contracts
// (issue #149), for config-artifacts.mjs to write and check with everything else
// it generates.
//
//   tool-reference     tool table, count and access classes    contract/tools.json
//   tool-index         the same, linked to docs/examples.md    contract/tools.json
//   config-reference   OPCUA_* tables, grouped by category     contract/config.json
//   version            the release version, inline             packages/server-node/package.json
//
// Why generated: the READMEs said "13 tools" beside a table of 15, three
// configuration tables were kept in step by hand (and a unit test that could only
// say a name was missing, not that a default or a choice was wrong), and the
// roadmap quoted a version two releases old. Counts, names, defaults and
// versions are facts the contracts already hold; copying them is how they drift.
//
// Only what sits between a pair of markers is generated:
//
//   <!-- BEGIN GENERATED: config-reference ... -->
//   ...
//   <!-- END GENERATED: config-reference -->
//
// Everything around them — why a setting exists, how to choose a profile, the
// worked examples — stays hand-written, because it is guidance rather than fact.
// A file listed in DOC_TARGETS must carry exactly the blocks listed for it, so
// deleting a pair of markers is drift too, not a way to opt out.
import { readFileSync } from "fs";
import { dirname, join, posix, relative } from "path";
import { fileURLToPath } from "url";

const here = dirname(fileURLToPath(import.meta.url)); // packages/server-node/scripts
const PKG_ROOT = join(here, "..");
const REPO_ROOT = join(PKG_ROOT, "..", "..");
export const TOOLS_PATH = join(REPO_ROOT, "contract", "tools.json");
export const PACKAGE_JSON_PATH = join(PKG_ROOT, "package.json");

export function loadTools(path = TOOLS_PATH) {
  return JSON.parse(readFileSync(path, "utf8"));
}

/** The one release version. package.json holds it because `publish.yml` checks
 *  the tag against it; every other copy is stamped from it. */
export function releaseVersion(path = PACKAGE_JSON_PATH) {
  return JSON.parse(readFileSync(path, "utf8")).version;
}

/** `https://github.com/<owner>/<repo>`, from package.json's repository field. */
export function repositoryUrl(path = PACKAGE_JSON_PATH) {
  const { repository } = JSON.parse(readFileSync(path, "utf8"));
  return repository.url.replace(/^git\+/, "").replace(/\.git$/, "");
}

// --- Markdown helpers -----------------------------------------------------------

/** Text that is safe inside a table cell: a `|` would end the cell, and a `<`
 *  would open an HTML tag that swallows `nsu=<namespace-uri>` on GitHub. */
function cell(text) {
  return String(text).replace(/\|/g, "\\|").replace(/</g, "&lt;");
}

function code(text) {
  return `\`${text}\``;
}

function table(header, rows) {
  return [
    `| ${header.join(" | ")} |`,
    `|${header.map(() => "---").join("|")}|`,
    ...rows.map((row) => `| ${row.map(cell).join(" | ")} |`),
  ].join("\n");
}

/** A heading's anchor as GitHub computes it. */
export function slugify(heading) {
  return heading
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s_-]/gu, "")
    .replace(/\s/g, "-");
}

/** The first sentence of a tool description: its summary, in the model's own words. */
function firstSentence(text) {
  const match = /^(.+?[.!?])(?=\s|$)/s.exec(text.trim());
  return (match ? match[1] : text).replace(/\s+/g, " ");
}

const NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight"];

// --- tool reference ---------------------------------------------------------------

/** Every access class this renderer knows how to explain. An unknown one is a
 *  contract change the legend below has not caught up with, so it stops the
 *  generator rather than being rendered without an explanation. */
const ACCESS_ORDER = ["read", "monitor", "alarm-action", "control"];

function hints(annotations) {
  const parts = [];
  if (annotations.readOnlyHint) parts.push("read-only");
  if (annotations.destructiveHint) parts.push("destructive");
  if (annotations.idempotentHint) parts.push("idempotent");
  return parts.length ? parts.join(", ") : "—";
}

/**
 * The tool table.
 *
 * `link(repoPath)` turns a repository path into a link from the target file.
 * `anchors`, when given, maps a tool name to the section documenting it, and
 * makes each name a link there.
 */
export function renderToolReference(contract, { link, anchors }) {
  const tools = contract.tools;
  for (const tool of tools) {
    if (!ACCESS_ORDER.includes(tool.accessClass)) {
      throw new Error(`${tool.name}: access class ${tool.accessClass} has no explanation here`);
    }
  }
  const byClass = ACCESS_ORDER.map((access) => [
    access,
    tools.filter((t) => t.accessClass === access).length,
  ]).filter(([, count]) => count > 0);
  const breakdown = byClass.map(([access, count]) => `${count} ${access}`);
  const last = breakdown.pop();

  // Grouped by access class, least to most consequential; contract order within one.
  const ordered = ACCESS_ORDER.flatMap((access) => tools.filter((t) => t.accessClass === access));
  const rows = ordered.map((tool) => {
    const gated = tool.capabilities.length > 0 ? " †" : "";
    const name = anchors ? `[${code(tool.name)}](#${anchors[tool.name]})` : code(tool.name);
    return [
      name + gated,
      tool.accessClass,
      hints(tool.annotations),
      firstSentence(tool.description),
    ];
  });

  const gated = tools
    .filter((tool) => tool.capabilities.length > 0)
    .map((tool) => {
      const nodes = tool.capabilities.map((c) => code(contract.capabilities[c].browseName));
      return `${code(tool.name)} on ${nodes.join(" or ")}`;
    });

  return [
    `Both runtimes expose the same **${tools.length} tools** — ` +
      `${breakdown.join(", ")} and ${last} — defined once in ` +
      `[${code("contract/tools.json")}](${link("contract/tools.json")}).`,
    "",
    // cell() escapes a `|` inside a link target too, which is harmless there.
    table(["Tool", "Access", "Hints", "What it does"], rows),
    "",
    "**Access** decides which `OPCUA_PROFILE` offers a tool. `read` and `monitor` " +
      "tools are offered under every profile, including the default `observe`. " +
      "`control` tools need `operator`, which offers them only for allowlisted " +
      "targets, or `full`; `alarm-action` tools need `operator` with " +
      "`OPCUA_ALLOW_ACKNOWLEDGE_ALARMS`, or `full`. Both also need a secured channel " +
      "unless `OPCUA_ALLOW_INSECURE_CONTROL` is set. **Hints** are the MCP tool " +
      "annotations each tool advertises.",
    ...(gated.length
      ? [
          "",
          `† **Capability-gated**: listed only when the connected server advertises ` +
            `what it needs — ${gated.join("; ")}.`,
        ]
      : []),
  ].join("\n");
}

/** The anchor of each tool's section in a Markdown document: the first heading
 *  that names the tool in code. A tool with no section is an error, because
 *  docs/examples.md is where CONTRIBUTING says every tool is documented. */
export function toolAnchors(contract, markdown, file) {
  const headings = [...markdown.matchAll(/^#{2,6}\s+(.+)$/gm)].map((m) => m[1]);
  const anchors = {};
  const missing = [];
  for (const { name } of contract.tools) {
    const heading = headings.find((h) => h.includes(code(name)));
    if (heading) anchors[name] = slugify(heading);
    else missing.push(name);
  }
  if (missing.length) {
    throw new Error(`${file} has no section heading for ${missing.join(", ")}`);
  }
  return anchors;
}

// --- configuration reference ------------------------------------------------------

function renderDefault(setting) {
  if (setting.default !== null && setting.default !== undefined) {
    return code(
      typeof setting.default === "string" ? setting.default : JSON.stringify(setting.default)
    );
  }
  return setting.defaultDescription ?? "—";
}

function choicesFor(setting, runtime) {
  return (runtime && setting.runtimeChoices?.[runtime]) || setting.choices;
}

/** A setting's description as a reference table shows it. The same parts as the
 *  bundle form and the registry entry (config-artifacts.mjs's describeSetting),
 *  with the facts that have fields of their own marked up rather than restated. */
function describeForDocs(setting, runtime) {
  const parts = [];
  if (setting.secret) parts.push("**Secret.**");
  if (!runtime && setting.runtimes.length === 1) {
    const only = setting.runtimes[0] === "node" ? "Node" : "Python";
    parts.push(`**${only} runtime only.**`);
  }
  parts.push(setting.description);
  if (setting.type === "enum") {
    const choices = choicesFor(setting, runtime).map(code).join(", ");
    const aliases = Object.entries(setting.choiceAliases ?? {}).map(
      ([alias, canonical]) => `${code(alias)} means ${code(canonical)}`
    );
    parts.push(`One of ${choices}${aliases.length ? ` (${aliases.join("; ")})` : ""}.`);
  }
  if (setting.securityNote) parts.push(setting.securityNote);
  return parts.join(" ");
}

/**
 * The configuration tables, one per category in the schema's order.
 *
 * `runtime` narrows the tables to what one runtime reads (a package README);
 * without it they cover both, and mark what only one of them reads.
 */
export function renderConfigReference(schema, { runtime }) {
  const settings = schema.settings.filter(
    (s) => s.surfaces.includes("docs") && (!runtime || s.runtimes.includes(runtime))
  );
  const sections = [];
  for (const [key, category] of Object.entries(schema.categories)) {
    const inCategory = settings.filter((s) => s.category === key);
    if (!inCategory.length) continue;
    sections.push(
      `**${category.title}** — ${category.description}\n\n` +
        table(
          ["Variable", "Default", "Description"],
          inCategory.map((s) => [code(s.env), renderDefault(s), describeForDocs(s, runtime)])
        )
    );
  }
  const count = settings.length;
  const groups = sections.length;
  return [
    `${count} settings in ${NUMBER_WORDS[groups] ?? groups} groups. A blank value means ` +
      "the default, whatever the type; a boolean accepts " +
      `${schema.booleanValues.true.map(code).join(", ")} and ` +
      `${schema.booleanValues.false.map(code).join(", ")}.`,
    "",
    sections.join("\n\n"),
  ].join("\n");
}

// --- where each block goes ----------------------------------------------------------

/** How a document links to a repository path. A package README ships inside its
 *  npm or PyPI artifact and is read on the registry's page, where only an
 *  absolute URL resolves; everything else links relatively, so a link checker
 *  can follow it on a checkout. */
function linker(file, repoUrl) {
  if (file.startsWith("packages/")) return (path) => `${repoUrl}/blob/main/${path}`;
  const from = posix.dirname(file);
  return (path) => posix.relative(from, path);
}

/**
 * Every Markdown file with generated blocks, and what each block renders.
 * Paths are repository-relative, in POSIX form.
 */
export const DOC_TARGETS = [
  {
    file: "README.md",
    blocks: { "tool-reference": { tools: {} }, "config-reference": { config: {} } },
  },
  {
    file: "packages/server-node/README.md",
    blocks: {
      "tool-reference": { tools: {} },
      "config-reference": { config: { runtime: "node" } },
    },
  },
  {
    file: "packages/server-python/README.md",
    blocks: {
      "tool-reference": { tools: {} },
      "config-reference": { config: { runtime: "python" } },
    },
  },
  { file: "docs/examples.md", blocks: { "tool-index": { tools: { linked: true } } } },
  { file: "ROADMAP.md", blocks: { version: { version: true } } },
];

const GENERATOR = "packages/server-node/scripts/config-artifacts.mjs";

const SOURCES = {
  "tool-reference": "contract/tools.json",
  "tool-index": "contract/tools.json",
  "config-reference": "contract/config.json",
};

/** A generated block, and the marker pair that delimits it. Inline blocks (the
 *  version inside a sentence) have no room for the regeneration hint. */
const BLOCK = /<!-- BEGIN GENERATED: ([a-z-]+)[^>]*-->([\s\S]*?)<!-- END GENERATED: ([a-z-]+) -->/g;

function wrap(name, content, inline) {
  if (inline) return `<!-- BEGIN GENERATED: ${name} -->${content}<!-- END GENERATED: ${name} -->`;
  const begin =
    `<!-- BEGIN GENERATED: ${name} from ${SOURCES[name]} by ${GENERATOR}. ` +
    "Do not edit by hand: edit the source, then run `npm run config:generate` in packages/server-node. -->";
  return `${begin}\n\n${content}\n\n<!-- END GENERATED: ${name} -->`;
}

/**
 * `markdown` (LF line endings) with every generated block re-rendered.
 * Throws if the file does not carry exactly the blocks `target` lists.
 */
export function renderDocument(target, markdown, { schema, contract, version, repoUrl }) {
  const link = linker(target.file, repoUrl);
  const expected = Object.keys(target.blocks);
  const seen = [];
  const rendered = markdown.replace(BLOCK, (whole, name, _body, endName) => {
    if (name !== endName) {
      throw new Error(`${target.file}: block ${name} is closed by an END marker for ${endName}`);
    }
    const spec = target.blocks[name];
    if (!spec) throw new Error(`${target.file}: unexpected generated block ${name}`);
    if (seen.includes(name)) throw new Error(`${target.file}: block ${name} appears twice`);
    seen.push(name);
    if (spec.version) return wrap(name, `**${version}**`, true);
    if (spec.config) return wrap(name, renderConfigReference(schema, spec.config), false);
    const anchors = spec.tools.linked ? toolAnchors(contract, markdown, target.file) : undefined;
    return wrap(name, renderToolReference(contract, { link, anchors }), false);
  });
  const missing = expected.filter((name) => !seen.includes(name));
  if (missing.length) {
    throw new Error(
      `${target.file} has no generated ${missing.join(", ")} block. Add the marker pair ` +
        `(<!-- BEGIN GENERATED: ${missing[0]} --> ... <!-- END GENERATED: ${missing[0]} -->) ` +
        "where it belongs and run `npm run config:generate`."
    );
  }
  return rendered;
}

export function targetPath(target) {
  return join(REPO_ROOT, ...target.file.split("/"));
}

/** Whether prettier formats the file, as `npm run format:check` would. */
export function formattedByPrettier(target) {
  return !relative(PKG_ROOT, targetPath(target)).startsWith("..");
}
