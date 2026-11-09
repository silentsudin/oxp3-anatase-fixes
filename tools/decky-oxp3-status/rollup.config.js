// Build config for Decky Loader. `@decky/rollup` resolves the loader-provided
// modules (`@decky/ui` -> the global DFL/SP_REACT shims, `@decky/api` -> the
// bundled client) and emits dist/index.js, which is what the loader loads.
import deckyPlugin from "@decky/rollup";

export default deckyPlugin({});
