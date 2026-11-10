import { build } from "esbuild";
await build({
  entryPoints: ["../../packages/scene-renderer/src/renderer.ts"],
  bundle: true,
  platform: "browser",
  format: "iife",
  globalName: "LuminaScene",
  outfile: "dist/export/renderer.browser.js",
});
