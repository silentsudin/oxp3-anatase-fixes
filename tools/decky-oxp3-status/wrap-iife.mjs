// SPDX-License-Identifier: MIT
// Decky Loader evaluates a plugin's dist/index.js as a classic script and takes
// the completion value as the plugin module. @decky/rollup emits an ES module
// (ending in `export { index as default };`), which this loader rejects with
// "unexpected token export". Other working plugins on the same loader ship an
// IIFE that returns the plugin object, so this step converts the rollup output
// into exactly that shape. It is idempotent and keeps the bundle as a single
// script file.
import { readFileSync, writeFileSync } from "node:fs";

const target = process.argv[2] || new URL("./dist/index.js", import.meta.url).pathname;
let source = readFileSync(target, "utf8");

if (source.includes("/* decky-iife */")) {
    process.stdout.write(`wrap-iife: ${target} already wrapped\n`);
    process.exit(0);
}

const exportRe = /export\s*\{\s*([A-Za-z_$][\w$]*)\s+as\s+default\s*\};?\s*$/m;
const match = source.match(exportRe);
if (!match) {
    process.stderr.write(`wrap-iife: no 'export { X as default }' found in ${target}\n`);
    process.exit(1);
}
source = source.replace(exportRe, `return ${match[1]};`);
source = source.replace(/\/\/# sourceMappingURL=.*$/m, "").trimEnd();

const wrapped = `/* decky-iife */\n(function () {\n"use strict";\n${source}\n})();\n`;
writeFileSync(target, wrapped);
process.stdout.write(`wrap-iife: ${target} wrapped (${wrapped.length} bytes, returns ${match[1]})\n`);
