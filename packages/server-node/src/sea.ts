// Entry point for the single-file executable (see scripts/build-sea.mjs).
//
// `index.ts` starts the server only when it is the process entry point, which it
// works out by comparing its own module URL to `process.argv[1]`. Inside a
// single-executable application there is no script and no `import.meta`, so that
// check can never be true — hence this file, which just starts it.
//
// `scriptPath: null` tells `--install` that this build *is* the executable, so
// the config it writes names the binary alone rather than `<node> <script>`.
import { runMain } from "./index.js";

runMain({ scriptPath: null });
