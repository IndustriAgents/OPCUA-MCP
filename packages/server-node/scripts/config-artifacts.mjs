// Generates everything in the repository that restates a contract or the
// release version, and checks that none of it has drifted:
//
//   packages/server-node/mcpb/manifest.json   user_config, server.mcp_config.env   contract/config.json (#133)
//   server.json                               packages[].environmentVariables      contract/config.json (#133)
//   README.md, packages/*/README.md,          tool and configuration reference     contract/tools.json and
//   docs/examples.md, ROADMAP.md              blocks (reference-docs.mjs)          contract/config.json (#149)
//   both manifests above, both pyproject      the release version                  package.json (#149)
//   files, package-lock.json, uv.lock
//
// Why generated: these were hand-maintained copies of the runtime's variable
// list, and they fell behind it. For the `.mcpb` that is not a documentation
// bug — Claude Desktop passes the server exactly the env the manifest declares,
// so a variable missing there (server-certificate pinning, X.509 user login, the
// audit file) was a capability a bundle user could not reach at all. The docs
// and the version copies drifted the same way (#149): a README counting 13 tools
// beside a table of 15, and a uv.lock still recording the previous release.
//
// Only those sections are generated. Everything else in these files — names,
// prose, links — stays hand-written, and the generator rewrites a file by
// replacing its sections in place, so the files remain the reviewable source
// they were. The two JSON files carry a `_meta` note saying which parts are
// generated, since JSON has no comments and both schemas reject unknown
// top-level keys; the Markdown blocks sit between BEGIN/END GENERATED markers.
//
// The release version has one source, packages/server-node/package.json — the
// file `publish.yml` checks the tag against. A release sets it there and runs
// the generator, which stamps it everywhere else.
//
//   npm run config:generate   rewrite every generated file
//   npm run config:check      exit 1 if any differs from what it would write (CI)
//
// The rendering helpers are exported for the other consumers of the schema: the
// --install helpers (#135) and the reference docs (reference-docs.mjs).
import { readFileSync, writeFileSync } from "fs";
import { dirname, join, relative } from "path";
import { fileURLToPath } from "url";

import * as prettier from "prettier";

import {
  DOC_TARGETS,
  formattedByPrettier,
  loadTools,
  releaseVersion,
  renderDocument,
  repositoryUrl,
  targetPath,
} from "./reference-docs.mjs";

const here = dirname(fileURLToPath(import.meta.url)); // packages/server-node/scripts
const PKG_ROOT = join(here, "..");
export const REPO_ROOT = join(PKG_ROOT, "..", "..");
export const SCHEMA_PATH = join(REPO_ROOT, "contract", "config.json");
export const MCPB_MANIFEST_PATH = join(PKG_ROOT, "mcpb", "manifest.json");
export const SERVER_JSON_PATH = join(REPO_ROOT, "server.json");
const PYPROJECT_PATHS = [
  join(REPO_ROOT, "packages", "server-python", "pyproject.toml"),
  join(REPO_ROOT, "packages", "mock-server", "pyproject.toml"),
];
const NPM_LOCKFILE_PATH = join(PKG_ROOT, "package-lock.json");
const UV_LOCKFILE_PATH = join(REPO_ROOT, "uv.lock");

/** The `_meta` namespace both files carry their "generated" note under. */
const MCPB_META_KEY = "io.github.industriagents/opcua-mcp";
/** The only `_meta` key the registry schema defines for publisher data. */
const REGISTRY_META_KEY = "io.modelcontextprotocol.registry/publisher-provided";

const GENERATOR = "packages/server-node/scripts/config-artifacts.mjs";

export function loadSchema(path = SCHEMA_PATH) {
  return JSON.parse(readFileSync(path, "utf8"));
}

/** The settings offered on one surface, in schema order. */
export function settingsFor(schema, surface) {
  return schema.settings.filter((setting) => setting.surfaces.includes(surface));
}

/** The MCPB `user_config` key for a setting — stable, because an installed
 *  bundle stores the user's answers under it. */
export function mcpbKey(setting) {
  return `opcua_${setting.key}`;
}

/** The value a default renders as in prose. */
function formatDefault(value) {
  return typeof value === "string" ? value : JSON.stringify(value);
}

/** The accepted spellings of an enum, as one sentence. */
function describeChoices(setting) {
  const byCanonical = new Map();
  for (const [alias, canonical] of Object.entries(setting.choiceAliases ?? {})) {
    byCanonical.set(canonical, [...(byCanonical.get(canonical) ?? []), alias]);
  }
  const aliases = [...byCanonical].map(
    ([canonical, names]) =>
      `${names.join(" and ")} ${names.length > 1 ? "mean" : "means"} ${canonical}`
  );
  return `One of: ${setting.choices.join(", ")}${aliases.length ? ` (${aliases.join("; ")})` : ""}.`;
}

/**
 * The description a surface shows for a setting.
 *
 * Built from parts rather than stored whole so the facts that have their own
 * field — choices, default, security consequence — cannot disagree with the
 * prose around them. `renderedDefault` says whether the surface shows the default
 * in a field of its own; when it does not, the prose has to say it.
 */
export function describeSetting(setting, { listChoices, renderedDefault }) {
  const parts = [setting.description];
  if (listChoices && setting.type === "enum") parts.push(describeChoices(setting));
  if (setting.securityNote) parts.push(setting.securityNote);
  if (!renderedDefault && setting.default !== null && setting.default !== undefined) {
    parts.push(`Leave blank for ${formatDefault(setting.default)}.`);
  } else if (setting.defaultDescription && (setting.default ?? null) === null) {
    parts.push(`Leave blank for ${setting.defaultDescription}.`);
  }
  return parts.join(" ");
}

/**
 * The MCPB form field for a setting.
 *
 * Numbers are `string` fields, as the hand-written manifest had them: an unset
 * optional field reaches the server as a blank string, which both runtimes read
 * as "use the default", so the form can leave the default to the runtime rather
 * than restating it. Only a path that must already exist is a `file` picker —
 * the audit file is one the server creates, which a picker cannot choose.
 */
export function mcpbField(setting) {
  const type =
    setting.type === "boolean"
      ? "boolean"
      : setting.type === "path" && setting.mustExist
        ? "file"
        : "string";
  // A default is shown in the form only where it cannot be left to the runtime:
  // a checkbox always submits a value, and a required field must start filled.
  const showDefault =
    setting.default !== null && (setting.type === "boolean" || setting.required === true);
  const field = {
    type,
    title: setting.title,
    description: describeSetting(setting, { listChoices: true, renderedDefault: showDefault }),
  };
  if (showDefault) field.default = setting.default;
  if (setting.sensitive) field.sensitive = true;
  field.required = setting.required === true;
  return field;
}

const REGISTRY_FORMATS = {
  string: "string",
  enum: "string",
  list: "string",
  boolean: "boolean",
  number: "number",
  path: "filepath",
};

/** The MCP Registry `environmentVariables` entry for a setting. */
export function registryVariable(setting) {
  const hasDefault = setting.default !== null && setting.default !== undefined;
  const entry = {
    name: setting.env,
    description: describeSetting(setting, { listChoices: false, renderedDefault: true }),
    format: REGISTRY_FORMATS[setting.type],
  };
  // The registry's defaults are strings whatever the format.
  if (hasDefault) entry.default = String(setting.default);
  if (setting.type === "enum") entry.choices = [...setting.choices];
  if (setting.example !== undefined && !setting.sensitive) entry.placeholder = setting.example;
  if (setting.required) entry.isRequired = true;
  if (setting.sensitive) entry.isSecret = true;
  return entry;
}

function generatedNote(sections) {
  return (
    `${sections} generated from contract/config.json by ${GENERATOR}, and the ` +
    "version is stamped from packages/server-node/package.json. " +
    "Edit those and run `npm run config:generate` in packages/server-node; " +
    "CI fails if they drift."
  );
}

/** The manifest with its version and configuration sections regenerated. */
export function renderMcpbManifest(schema, manifest, version = manifest.version) {
  const settings = settingsFor(schema, "mcpb");
  const env = {};
  const userConfig = {};
  for (const setting of settings) {
    env[setting.env] = `\${user_config.${mcpbKey(setting)}}`;
    userConfig[mcpbKey(setting)] = mcpbField(setting);
  }
  return {
    ...manifest,
    version,
    server: {
      ...manifest.server,
      mcp_config: { ...manifest.server.mcp_config, env },
    },
    user_config: userConfig,
    _meta: {
      ...manifest._meta,
      [MCPB_META_KEY]: {
        generated: generatedNote("user_config and server.mcp_config.env are"),
      },
    },
  };
}

/** server.json with its versions and every package's environment variables
 *  regenerated. It carries the version twice, and the registry reads both. */
export function renderServerJson(schema, serverJson, version = serverJson.version) {
  const variables = settingsFor(schema, "registry").map(registryVariable);
  return {
    ...serverJson,
    version,
    packages: serverJson.packages.map((pkg) => ({
      ...pkg,
      version,
      environmentVariables: variables,
    })),
    _meta: {
      ...serverJson._meta,
      [REGISTRY_META_KEY]: {
        ...serverJson._meta?.[REGISTRY_META_KEY],
        generated: generatedNote("packages[].environmentVariables are"),
      },
    },
  };
}

/** Serialise as the repository formats it, so a regenerated file passes
 *  `prettier --check` and a no-op run leaves no diff. A JSON document is
 *  indented before prettier sees it because prettier keeps an object expanded
 *  only if it already was — which is how the hand-written parts of both files
 *  are laid out. Markdown arrives as text. */
async function format(document, path) {
  const options = (await prettier.resolveConfig(join(PKG_ROOT, ".prettierrc.json"))) ?? {};
  const source = typeof document === "string" ? document : JSON.stringify(document, null, 2);
  return prettier.format(source, { ...options, filepath: path });
}

/** A pyproject.toml with its `[project]` version stamped. A targeted rewrite
 *  rather than a TOML round trip, which would reformat the whole file. */
export function stampPyproject(text, version) {
  const project = /^(\[project\]\n(?:(?!\[)[^\n]*\n)*?version = ")[^"]*(")/m;
  if (!project.test(text)) throw new Error("pyproject.toml has no [project] version");
  return text.replace(project, `$1${version}$2`);
}

/** package-lock.json with the root package's version stamped. npm records it
 *  twice, and editing package.json updates neither; the next `npm install`
 *  would, as a version diff inside some unrelated change. */
export function stampNpmLockfile(text, version) {
  const lock = JSON.parse(text);
  lock.version = version;
  lock.packages[""].version = version;
  return `${JSON.stringify(lock, null, 2)}\n`;
}

/** uv.lock with the workspace's own two packages at the release version. uv
 *  rewrites these on the next `uv sync`, which is how a stale copy (0.5.0,
 *  through the whole 0.5.1 release) went unnoticed. */
export function stampUvLockfile(text, version) {
  const local =
    /(\[\[package\]\]\nname = "[^"]+"\nversion = ")[^"]*("\nsource = \{ editable = "packages\/(?:server-python|mock-server)" \})/g;
  const found = text.match(local)?.length ?? 0;
  if (found !== 2) throw new Error(`uv.lock: expected 2 workspace packages, found ${found}`);
  return text.replace(local, `$1${version}$2`);
}

/** `rendered` (LF) in the line ending `current` already uses. */
function inEolOf(current, rendered) {
  return current.includes("\r\n") ? rendered.replace(/\n/g, "\r\n") : rendered;
}

/** `text` with LF line endings. The repository stores these files with LF, but
 *  a Windows checkout with `core.autocrlf` hands them over as CRLF — the same
 *  content, and not drift. */
export function normalizeEol(text) {
  return text.replace(/\r\n/g, "\n");
}

/**
 * Every generated file, as [path, expected text, current text].
 *
 * `expected` is rendered in whichever line ending the file on disk already uses,
 * so a regeneration on a CRLF checkout rewrites only what changed rather than
 * every line; git normalises it back to LF on commit either way. Compare the two
 * with `normalizeEol` on both sides, as `main` does.
 */
export async function renderAll(schema = loadSchema(), contract = loadTools()) {
  const version = releaseVersion();
  const repoUrl = repositoryUrl();
  const jsonTargets = [
    [MCPB_MANIFEST_PATH, (doc) => renderMcpbManifest(schema, doc, version)],
    [SERVER_JSON_PATH, (doc) => renderServerJson(schema, doc, version)],
  ];
  // Rendered from LF text, whatever the checkout hands over.
  const textTargets = [
    ...PYPROJECT_PATHS.map((path) => [path, (text) => stampPyproject(text, version)]),
    [NPM_LOCKFILE_PATH, (text) => stampNpmLockfile(text, version)],
    [UV_LOCKFILE_PATH, (text) => stampUvLockfile(text, version)],
    ...DOC_TARGETS.map((target) => [
      targetPath(target),
      async (text) => {
        const rendered = renderDocument(target, text, { schema, contract, version, repoUrl });
        // A file `npm run format:check` covers must come out as prettier leaves it.
        return formattedByPrettier(target) ? format(rendered, targetPath(target)) : rendered;
      },
    ]),
  ];
  return Promise.all([
    ...jsonTargets.map(async ([path, render]) => {
      const current = readFileSync(path, "utf8");
      const rendered = await format(render(JSON.parse(current)), path);
      return [path, inEolOf(current, rendered), current];
    }),
    ...textTargets.map(async ([path, render]) => {
      const current = readFileSync(path, "utf8");
      const rendered = await render(normalizeEol(current));
      return [path, inEolOf(current, rendered), current];
    }),
  ]);
}

async function main(argv) {
  const check = argv.includes("--check");
  const drifted = [];
  let rendered;
  try {
    rendered = await renderAll();
  } catch (error) {
    // A missing marker pair or an undocumented tool: nothing to write, only to fix.
    console.error(error.message);
    process.exit(1);
  }
  for (const [path, expected, current] of rendered) {
    if (normalizeEol(expected) === normalizeEol(current)) continue;
    drifted.push(relative(REPO_ROOT, path));
    if (!check) writeFileSync(path, expected);
  }
  if (check && drifted.length) {
    console.error(
      `Out of date with the contracts or the release version: ${drifted.join(", ")}\n` +
        "Run `npm run config:generate` in packages/server-node and commit the result."
    );
    process.exit(1);
  }
  console.error(
    check
      ? "generated files match the contracts and the release version"
      : drifted.length
        ? `regenerated ${drifted.join(", ")}`
        : "generated files already up to date"
  );
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  await main(process.argv.slice(2));
}
