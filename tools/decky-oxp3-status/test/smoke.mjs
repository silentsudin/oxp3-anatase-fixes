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
import { createRequire } from "node:module";

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

const mod = await import("../dist/index.js");
const plugin = mod.default;

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

console.log(
  "smoke: dist/index.js loaded and rendered (%d bytes of markup, api handshake ok)",
  html.length,
);
