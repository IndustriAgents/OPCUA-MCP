// The shared tool contract and the package version, both staged into build/ by
// scripts/prepare-build.mjs so the published package is self-contained.
import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

export const BUILD_DIR = dirname(fileURLToPath(import.meta.url));

export const CONTRACT: {
  capabilities: Record<string, { nodeId: string; browseName: string; check: string }>;
  resources: Array<{
    uri: string;
    name: string;
    description: string;
    mimeType: string;
    body: { recordsKey: string; resultShape: string };
  }>;
  tools: Array<{ name: string; capability: string | null; description: string; inputSchema: any }>;
} = JSON.parse(readFileSync(join(BUILD_DIR, "contract.json"), "utf8"));

// Version is single-sourced from package.json and staged into build/version.json
// by scripts/prepare-build.mjs, so it can never drift from what npm publishes.
export const { version: VERSION }: { version: string } = JSON.parse(
  readFileSync(join(BUILD_DIR, "version.json"), "utf8")
);
