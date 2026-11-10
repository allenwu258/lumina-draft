import { createInterface } from "node:readline";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { resolve, relative, isAbsolute } from "node:path";
import { chromium, type Browser } from "playwright-core";
import { CONVERTER_VERSION } from "@lumina/scene-core";
import { canonical, convert } from "./capture.js";

const root = resolve(process.argv[2] || ".");
const send = (v: unknown) => process.stdout.write(JSON.stringify(v) + "\n");
function within(path: string) {
  const result = resolve(root, path),
    rel = relative(root, result);
  if (rel.startsWith("..") || isAbsolute(rel))
    throw new Error("INVALID_TASK_PATH");
  return result;
}
let browser: Browser;
try {
  browser = await chromium.launch({ headless: true });
} catch (e) {
  console.error(String(e));
  process.exit(1);
}
send({
  type: "ready",
  protocol: 1,
  converterVersion: CONVERTER_VERSION,
  chromium: browser.version(),
});
let busy = false;
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
lines.on("line", async (line) => {
  let request: {
    type: string;
    request_id: string;
    attempt_token: string;
    input: string;
    output: string;
  };
  try {
    request = JSON.parse(line);
  } catch {
    send({ type: "error", code: "INVALID_PROTOCOL" });
    return;
  }
  if (request.type === "shutdown") {
    await browser.close();
    lines.close();
    process.exit(0);
  }
  if (busy) {
    send({
      type: "error",
      request_id: request.request_id,
      attempt_token: request.attempt_token,
      code: "WORKER_BUSY",
    });
    return;
  }
  busy = true;
  const controller = new AbortController(),
    timer = setTimeout(() => controller.abort(), 25000);
  try {
    if (request.type !== "parse") throw new Error("INVALID_PROTOCOL");
    const input = JSON.parse(await readFile(within(request.input), "utf8")),
      output = within(request.output);
    await mkdir(output, { recursive: true });
    const scene = await convert(
      input,
      { browser, outputDir: output },
      controller.signal,
    );
    await writeFile(resolve(output, "scene.json"), canonical(scene));
    send({
      type: "result",
      request_id: request.request_id,
      attempt_token: request.attempt_token,
      scene: "scene.json",
    });
  } catch (e) {
    const message = String(e).slice(0, 1600);
    console.error(message);
    send({
      type: "error",
      request_id: request.request_id,
      attempt_token: request.attempt_token,
      code: controller.signal.aborted
        ? "CONVERSION_TIMEOUT"
        : !browser.isConnected()
          ? "BROWSER_CRASHED"
          : "CONVERSION_CONTENT_ERROR",
      message,
    });
  } finally {
    clearTimeout(timer);
    busy = false;
  }
});
lines.on("close", () => void browser.close().finally(() => process.exit(0)));
