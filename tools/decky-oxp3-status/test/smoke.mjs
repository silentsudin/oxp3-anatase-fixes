/**
 * Headless load/render smoke test for the built plugin.
 *
 * Decky Loader evaluates dist/index.js in the Steam frontend, where the loader
 * has already published the React bundles and the frontend library as globals
 * (SP_REACT, SP_JSX, SP_REACTDOM, DFL) and the api handshake object on window.
 * This harness provides the same globals with the real React 18 runtime and a
 * small DFL shim, so `dist/index.js` is imported and rendered for real instead
 * of only being type-checked.
 *
 * It is a build-time check only; the shim is not shipped in the plugin zip.
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const require = createRequire(import.meta.url);
const React = require("react");
const jsxRuntime = require("react/jsx-runtime");
const ReactDOMServer = require("react-dom/server");

const calls = [];
let connected = false;

const report = {
  schema: 1,
  version: "1.0.0",
  generated_at: "2026-11-09T00:00:00Z",
  overall: "warn",
  counts: { ok: 3, warn: 1, fail: 0, unknown: 0, na: 0 },
  host: { user: "fixture", home: "/home/fixture", kernel: "7.2.7-an28.fixture" },
  game: null,
  env_sources: [],
  groups: [
    {
      id: "demo",
      title: "Demo",
      state: "warn",
      indicators: [
        { id: "demo.ok", label: "an ok row", state: "ok", value: "value", detail: "detail" },
        {
          id: "demo.warn",
          label: "a warn row",
          state: "warn",
          value: "value",
          detail: null,
          source: "fixture",
        },
      ],
    },
  ],
  notes: [],
};

// --- Decky loader globals -------------------------------------------------
globalThis.SP_REACT = React;
globalThis.SP_JSX = jsxRuntime;
globalThis.SP_REACTDOM = require("react-dom/client");

const passthrough = (props) => jsxRuntime.jsx("div", { children: props.children });
globalThis.DFL = {
  Spinner: () => jsxRuntime.jsx("span", { children: "spinner" }),
  PanelSection: (props) =>
    jsxRuntime.jsxs("div", { children: [props.title ?? null, props.children] }),
  PanelSectionRow: passthrough,
  Field: (props) =>
    jsxRuntime.jsxs("div", {
      children: [props.label ?? null, props.description ?? null, props.children],
    }),
  ButtonItem: (props) => jsxRuntime.jsx("button", { children: props.children }),
  Focusable: passthrough,
  staticClasses: { Title: "title" },
  definePlugin: (factory) => factory(),
};

globalThis.window = globalThis;
window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit = {
  connect: () => {
    connected = true;
    return {
      _version: 2,
      call: async (name) => {
        calls.push(name);
        return report;
      },
      toaster: { toast: () => {} },
    };
  },
};

// The loader evaluates dist/index.js as a classic script and takes the
// completion value as the plugin object; evaluate the wrapped IIFE the same
// way instead of importing it as an ES module (which would have no default).
const plugin = vm.runInThisContext(
  readFileSync(new URL("../dist/index.js", import.meta.url), "utf8"),
  { filename: "dist/index.js" }
);

assert.ok(plugin && typeof plugin === "object", "default export must be a plugin object");
assert.equal(plugin.name, "OXP3 Status");
assert.ok(plugin.content, "plugin must expose content");
assert.ok(plugin.titleView, "plugin must expose titleView");
assert.ok(connected, "the bundled @decky/api client must connect to the loader handshake");

const title = ReactDOMServer.renderToStaticMarkup(plugin.titleView);
assert.match(title, /OXP3 Status/);

const html = ReactDOMServer.renderToStaticMarkup(plugin.content);
assert.match(html, /OXP3 Status/, "panel must render its section title");
assert.match(html, /loading/, "effect has not run under server rendering: loading row expected");
assert.match(html, /Verification/, "panel must render the verification row");
assert.match(html, /Run quick verification/, "the quick verification button must be present");
assert.match(html, /Checking OXP3 fixes\.\.\./, "the health line must render first");
assert.match(html, /Checking the running game\.\.\./, "the XeSS line must render second");
assert.ok(
  html.indexOf("Checking OXP3 fixes") < html.indexOf("Overall"),
  "the health line must sit above the detailed matrix"
);
assert.ok(
  html.indexOf("Checking the running game") < html.indexOf("Overall"),
  "the XeSS line must sit above the detailed matrix"
);
assert.match(html, /More verification modes/, "the invasive modes must stay behind a second step");
assert.ok(
  !/Run full verification/.test(html),
  "invasive mode buttons must stay hidden until More verification modes is opened"
);
assert.ok(
  !/Confirm and run/.test(html),
  "no invasive confirm action may be reachable without the explicit step"
);

// --- backend verification action: stub verifier fixtures -------------------
// The Python fixture suite creates throw-away harness scripts that emit
// synthetic schema-1 reports (pass / fail / skip / timeout / bad JSON / capped
// output / escalation refusal / busy) and drives run_verification_once against
// them. Running it from the smoke gate keeps the new Decky method covered by
// the same build step. A full run of tools/oxp3-verify.sh is never performed.
const pluginDir = fileURLToPath(new URL("..", import.meta.url));
const driver = `
import asyncio, importlib.util, json, sys
spec = importlib.util.spec_from_file_location("decky_main", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
sys.modules["decky_main"] = mod
spec.loader.exec_module(mod)
total, failures = mod.verification_selftest()
overview_total, overview_failures = mod.overview_selftest()
plugin = mod.Plugin()
last = asyncio.run(plugin.get_verification())
bad = asyncio.run(plugin.run_verification("bogus", False))
print(json.dumps({
    "total": total,
    "failures": failures,
    "overviewTotal": overview_total,
    "overviewFailures": overview_failures,
    "lastAvailable": bool(last.get("available")),
    "methods": sorted(name for name in dir(plugin) if not name.startswith("_")),
    "badMode": (bad.get("error") or {}).get("code"),
}))
`;
const backend = spawnSync(
  "python3",
  ["-c", driver, fileURLToPath(new URL("../main.py", import.meta.url))],
  {
    cwd: pluginDir,
    encoding: "utf8",
    timeout: 120000,
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  }
);
if (backend.status !== 0) {
  process.stderr.write(backend.stdout ?? "");
  process.stderr.write(backend.stderr ?? "");
}
assert.equal(backend.status, 0, "the stub-verifier fixture driver must exit 0");
const fixtures = JSON.parse(backend.stdout.trim());
assert.deepEqual(
  fixtures.failures,
  [],
  `stub-verifier fixtures failed: ${JSON.stringify(fixtures.failures)}`
);
assert.ok(fixtures.total >= 20, `expected >=20 fixture checks, got ${fixtures.total}`);
assert.deepEqual(
  fixtures.overviewFailures,
  [],
  `overview fixtures failed: ${JSON.stringify(fixtures.overviewFailures)}`
);
assert.ok(
  fixtures.overviewTotal >= 30,
  `expected >=30 overview fixture checks, got ${fixtures.overviewTotal}`
);
assert.equal(fixtures.lastAvailable, true, "get_verification must report the last run");
assert.ok(fixtures.methods.includes("run_verification"), "Plugin.run_verification must exist");
assert.ok(fixtures.methods.includes("get_verification"), "Plugin.get_verification must exist");
assert.ok(fixtures.methods.includes("get_overview"), "Plugin.get_overview must exist");
assert.equal(fixtures.badMode, "bad-mode", "the backend must return an error object, not raise");

console.log(
  "smoke: dist/index.js loaded and rendered (%d bytes of markup, api handshake ok), " +
    "%d stub-verifier + %d overview backend checks passed",
  html.length,
  fixtures.total,
  fixtures.overviewTotal
);


// The Decky loader evaluates dist/index.js as a classic script, so the shipped
// bundle must be an IIFE expression and must not contain a top-level ESM export.
{
  const bundle = readFileSync(new URL("../dist/index.js", import.meta.url), "utf8");
  if (!bundle.includes("/* decky-iife */")) throw new Error("dist/index.js is not IIFE-wrapped");
  if (/^export\s/m.test(bundle)) throw new Error("dist/index.js still has a top-level export");
}
