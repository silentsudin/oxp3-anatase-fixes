/**
 * Two-line overview render test.
 *
 * The backend contract and the health/xess fixtures are covered by
 * `python3 main.py --selftest`; this file proves the shipped IIFE actually
 * renders those payloads in the right order at the top of the panel. The
 * bundle is evaluated the way Decky evaluates it (classic script, completion
 * value), and the headless renderer is seeded through the same test-only
 * global the smoke harness uses. No backend, /proc, network or Decky needed.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const require = createRequire(import.meta.url);
const React = require("react");
const jsxRuntime = require("react/jsx-runtime");
const ReactDOMServer = require("react-dom/server");

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
let connections = 0;
window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit = {
  connect: () => {
    connections += 1;
    return {
      _version: 2,
      call: async (name) => ({ schema: 1, method: name }),
      toaster: { toast: () => {} },
    };
  },
};

const bundlePath = fileURLToPath(new URL("../dist/index.js", import.meta.url));
const bundle = readFileSync(bundlePath, "utf8");

function render(overview, { open = false } = {}) {
  globalThis.__OXP3_STATUS_OVERVIEW__ = overview;
  globalThis.__OXP3_STATUS_OVERVIEW_OPEN__ = open;
  const plugin = vm.runInThisContext(bundle, { filename: "dist/index.js" });
  assert.ok(plugin && typeof plugin === "object", "bundle must evaluate to the plugin");
  return ReactDOMServer.renderToStaticMarkup(plugin.content);
}

const counts = (pass, fail, skip, unknown) => ({
  pass,
  fail,
  skip,
  unknown,
  total: pass + fail + skip + unknown,
});

const healthOk = {
  state: "ok",
  headline: "All fixes applied and working",
  summary: "0 fail · 0 unknown · 6 skip · 44 pass",
  counts: counts(44, 0, 6, 0),
  issues: [],
  issues_total: 0,
  issue_overflow: 0,
};
const xessXmx = {
  state: "xess-on-xmx",
  label: "XeSS XMX: active (Cyberpunk 2077)",
  game: "Cyberpunk 2077",
  pid: 4242,
  evidence: "libxess_dx12.so in pid 4242; VK_DRIVER_FILES=…/icd-xmx.json",
};

// 1. Healthy machine + XMX game: green lines at the very top.
{
  const html = render({ health: healthOk, xess: xessXmx });
  assert.match(html, /All fixes applied and working/);
  assert.match(html, /XeSS XMX: active \(Cyberpunk 2077\)/);
  assert.match(html, /0 fail · 0 unknown · 6 skip · 44 pass/);
  assert.ok(
    html.indexOf("All fixes applied and working") < html.indexOf("Overall"),
    "the health line must come before the detailed matrix"
  );
  assert.ok(
    html.indexOf("XeSS XMX: active") < html.indexOf("Overall"),
    "the XeSS line must come before the detailed matrix"
  );
  assert.match(html, /Verification/);
  assert.match(html, /Run quick verification/);
}

// 2. Failures: amber/red headline, expandable issue ids, hints and overflow.
{
  const healthFail = {
    state: "fail",
    headline: "Needs attention: vol.unit, xmx.icd +1 more",
    summary: "2 fail · 1 unknown · 8 skip · 41 pass",
    counts: counts(41, 2, 8, 1),
    issues: [
      {
        id: "vol.unit",
        state: "fail",
        message: "volume keys target the wrong sink",
        hint: "sudo tools/oxp3-hw-install.sh install volume",
      },
      {
        id: "xmx.icd",
        state: "fail",
        message: "VK_DRIVER_FILES is not set",
        hint: "tools/oxp3-verify.sh --json",
      },
      {
        id: "aud.sink",
        state: "unknown",
        message: "only auto_null is visible",
        hint: "run inside the game session",
      },
    ],
    issues_total: 3,
    issue_overflow: 0,
  };
  const html = render(
    { health: healthFail, xess: { state: "xess-off", label: "XeSS: current game does not use it" } },
    { open: true }
  );
  assert.match(html, /Needs attention: vol.unit, xmx\.icd \+1 more/);
  assert.match(html, /vol\.unit/);
  assert.match(html, /volume keys target the wrong sink/);
  assert.match(html, /sudo tools\/oxp3-hw-install\.sh install volume/);
  assert.match(html, /aud\.sink/);
  assert.match(html, /run inside the game session/);
  assert.match(html, /XeSS: current game does not use it/);
}

// 3. Issue overflow is reported, not silently dropped.
{
  const healthOverflow = {
    state: "fail",
    headline: "Needs attention: a.one, b.two, c.three +5 more",
    summary: "8 fail · 0 unknown · 0 skip · 0 pass",
    counts: counts(0, 8, 0, 0),
    issues: [
      { id: "a.one", state: "fail", message: "one", hint: "fix one" },
      { id: "b.two", state: "fail", message: "two", hint: "fix two" },
    ],
    issues_total: 8,
    issue_overflow: 6,
  };
  const html = render({ health: healthOverflow, xess: { state: "no-game", label: "No game running" } });
  assert.match(html, /No game running/);
  assert.match(html, /Needs attention: a\.one, b\.two, c\.three \+5 more/);
}

// 4. XeSS without XMX and the no-XeSS/no-game labels.
{
  const standard = render({
    health: healthOk,
    xess: {
      state: "xess-on-standard",
      label: "XeSS: detected, XMX shim NOT active (Cyberpunk 2077)",
      evidence: "libxess.so in pid 7; no XMX ICD/driver/shim",
    },
  });
  assert.match(standard, /XeSS: detected, XMX shim NOT active \(Cyberpunk 2077\)/);

  const noGame = render({
    health: { ...healthOk, state: "warn", headline: "Nothing verified: 6 check(s) were skipped" },
    xess: { state: "no-game", label: "No game running" },
  });
  assert.match(noGame, /No game running/);
  assert.match(noGame, /Nothing verified/);
}

// 5. Missing payload (backend error/shape change) degrades, never throws.
{
  const html = render({});
  assert.match(html, /Fix status unavailable/);
  assert.match(html, /Game status unavailable/);
  assert.match(html, /Verification/);
}

// 6. Before the first fetch the two lines are placeholders above everything.
{
  globalThis.__OXP3_STATUS_OVERVIEW__ = undefined;
  globalThis.__OXP3_STATUS_OVERVIEW_OPEN__ = false;
  const plugin = vm.runInThisContext(bundle, { filename: "dist/index.js" });
  const html = ReactDOMServer.renderToStaticMarkup(plugin.content);
  assert.match(html, /Checking OXP3 fixes\.\.\./);
  assert.match(html, /Checking the running game\.\.\./);
  assert.ok(html.indexOf("Checking OXP3 fixes") < html.indexOf("Overall"));
  assert.ok(html.indexOf("Checking the running game") < html.indexOf("Overall"));
}

assert.ok(connections >= 1, "each evaluated bundle must connect to the loader API");
console.log("overview: all render states passed (%d bundle evaluations)", connections);
