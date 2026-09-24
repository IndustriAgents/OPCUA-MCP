// Generates the configuration sections of the distribution metadata from the
// canonical schema at /contract/config.json (issue #133).
//
//   packages/server-node/mcpb/manifest.json   user_config + server.mcp_config.env
//   server.json                               packages[0].environmentVariables
//
// Why generated: these were hand-maintained copies of the runtime's variable
// list, and they fell behind it. For the `.mcpb` that is not a documentation
// bug — Claude Desktop passes the server exactly the env the manifest declares,
// so a variable missing there (server-certificate pinning, X.509 user login, the
// audit file) was a capability a bundle user could not reach at all.
//
// Only those sections are generated. Everything else in both files — names,
// prose, links, versions — stays hand-written, and the generator rewrites a file
// by replacing its sections in place, so the files remain the reviewable source
// they were. Each carries a `_meta` note saying which parts are generated, since
// JSON has no comments and both schemas reject unknown top-level keys.
//
//   npm run config:generate   rewrite both files
//   npm run config:check      exit 1 if either differs from what it would write (CI)
//
// The rendering helpers are exported for the next consumers of the schema: the
// --install helpers (#135) and the generated reference tables (#149).
import { readFileSync, writeFileSync } from "fs";
import { dirname, join, relative } from "path";
import { fileURLToPath } from "url";

import * as prettier from "prettier";

const here = dirname(fileURLToPath(import.meta.url)); // packages/server-node/scripts
const PKG_ROOT = join(here, "..");
export const REPO_ROOT = join(PKG_ROOT, "..", "..");
export const SCHEMA_PATH = join(REPO_ROOT, "contract", "config.json");
export const MCPB_MANIFEST_PATH = join(PKG_ROOT, "mcpb", "manifest.json");
export const SERVER_JSON_PATH = join(REPO_ROOT, "server.json");

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
    `${sections} generated from contract/config.json by ${GENERATOR}. ` +
    "Edit the schema and run `npm run config:generate` in packages/server-node; " +
    "CI fails if they drift."
  );
}

/** The manifest with its configuration sections regenerated. */
export function renderMcpbManifest(schema, manifest) {
  const settings = settingsFor(schema, "mcpb");
  const env = {};
  const userConfig = {};
  for (const setting of settings) {
    env[setting.env] = `\${user_config.${mcpbKey(setting)}}`;
    userConfig[mcpbKey(setting)] = mcpbField(setting);
  }
  return {
    ...manifest,
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

/** server.json with every package's environment variables regenerated. */
export function renderServerJson(schema, serverJson) {
  const variables = settingsFor(schema, "registry").map(registryVariable);
  return {
    ...serverJson,
    packages: serverJson.packages.map((pkg) => ({ ...pkg, environmentVariables: variables })),
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
 *  `prettier --check` and a no-op run leaves no diff. Indented before prettier
 *  sees it because prettier keeps an object expanded only if it already was —
 *  which is how the hand-written parts of both files are laid out. */
async function format(document, path) {
  const options = (await prettier.resolveConfig(join(PKG_ROOT, ".prettierrc.json"))) ?? {};
  return prettier.format(JSON.stringify(document, null, 2), { ...options, filepath: path });
}

/** Every generated file, as [path, expected text, current text]. */
export async function renderAll(schema = loadSchema()) {
  const targets = [
    [MCPB_MANIFEST_PATH, renderMcpbManifest],
    [SERVER_JSON_PATH, renderServerJson],
  ];
  return Promise.all(
    targets.map(async ([path, render]) => {
      const current = readFileSync(path, "utf8");
      return [path, await format(render(schema, JSON.parse(current)), path), current];
    })
  );
}

async function main(argv) {
  const check = argv.includes("--check");
  const drifted = [];
  for (const [path, expected, current] of await renderAll()) {
    if (expected === current) continue;
    drifted.push(relative(REPO_ROOT, path));
    if (!check) writeFileSync(path, expected);
  }
  if (check && drifted.length) {
    console.error(
      `Out of date with contract/config.json: ${drifted.join(", ")}\n` +
        "Run `npm run config:generate` in packages/server-node and commit the result."
    );
    process.exit(1);
  }
  console.error(
    check
      ? "config artifacts match contract/config.json"
      : drifted.length
        ? `regenerated ${drifted.join(", ")}`
        : "config artifacts already up to date"
  );
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  await main(process.argv.slice(2));
}
