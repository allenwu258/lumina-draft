import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// This helper is deliberately a tiny data-in/data-out boundary. It never
// evaluates author HTML and never accepts a compiler path, config or plugin
// from the request. The backend supplies only already-tokenized class names.
const root = resolve(process.argv[2] || process.cwd());
const require = createRequire(resolve(root, "frontend/package.json"));
const { compile } = require("@tailwindcss/node");
const base = readFileSync(resolve(root, "frontend/tailwind-ppt.css"), "utf8");

const input = JSON.parse(readFileSync(0, "utf8"));
if (!Array.isArray(input.classes)) throw new Error("TAILWIND_CLASSES_INPUT");
const classes = [...new Set(input.classes.map(String))];
if (classes.length > 4096) throw new Error("TAILWIND_CANDIDATE_LIMIT");
if (classes.some((value) => value.length > 256 || /[\u0000\r\n]/.test(value)))
  throw new Error("TAILWIND_CANDIDATE_INVALID");

const compiler = await compile(base, {
  base: resolve(root, "frontend"),
  onDependency() {},
});
process.stdout.write(compiler.build(classes));
