#!/usr/bin/env node
// Offline Mermaid -> .svg/.png renderer for the writing image's `diagram` skill.
//
// Hermes Helmet's reusable adapter for gstack's committed diagram-render bundle
// (assets/diagram-render.html). gstack's own driver needs bun and its browse
// daemon; this one needs only Node (global WebSocket) and the Chromium that the
// Hermes base image already resolves. It has no npm dependencies, serves exactly
// one file on 127.0.0.1 and blocks every non-loopback host in the browser, so
// rendering stays offline. The bundle's Excalidraw functions are never called:
// the supported inputs and outputs are Mermaid source, SVG and PNG.
//
//   node render.mjs --check
//   node render.mjs <diagram.mmd> [--out-dir DIR] [--slug NAME] [--width PX]
import { spawn, spawnSync } from "node:child_process";
import { createServer } from "node:http";
import { createHash } from "node:crypto";
import { accessSync, constants, existsSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const SKILL = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const BUNDLE = join(SKILL, "assets/diagram-render.html");
const BUILD_INFO = join(SKILL, "assets/BUILD_INFO.json");
const PROVENANCE = join(SKILL, "PROVENANCE.json");
const HERMES_TOOLS = "/opt/hermes/tools";
const RESOLVED_BROWSER_FILE = "/etc/hermes/agent-browser-executable-path";
const PAGE_FUNCTIONS = ["__renderMermaid", "__rasterize"];
const SOURCE_EXTENSIONS = [".mmd", ".mermaid"];
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function executable(path) {
  try { accessSync(path, constants.X_OK); return true; } catch { return false; }
}

/**
 * The browser the Hermes base image resolved at build time, never a downloaded
 * one. An explicit DIAGRAM_CHROME is authoritative: no silent fallback.
 */
export function findChrome(env = process.env) {
  if (env.DIAGRAM_CHROME) return executable(env.DIAGRAM_CHROME) ? env.DIAGRAM_CHROME : null;
  const candidates = [];
  if (env.AGENT_BROWSER_EXECUTABLE_PATH) candidates.push(env.AGENT_BROWSER_EXECUTABLE_PATH);
  try { candidates.push(readFileSync(RESOLVED_BROWSER_FILE, "utf8").trim()); } catch { /* not an image */ }
  if (existsSync(HERMES_TOOLS)) {
    for (const entry of readdirSync(HERMES_TOOLS).filter((n) => n.startsWith("chromium-")).sort().reverse()) {
      // The layout differs per architecture; the resolved file above is preferred.
      candidates.push(join(HERMES_TOOLS, entry, "chrome-linux64/chrome"), join(HERMES_TOOLS, entry, "chromium-linux-arm64/chromium"));
    }
  }
  return candidates.find((candidate) => candidate && executable(candidate)) ?? null;
}

function browserVersion(chrome) {
  const probe = spawnSync(chrome, ["--version"], { encoding: "utf8", timeout: 20000 });
  return probe.status === 0 ? String(probe.stdout).trim() : null;
}

/** Everything the render path needs; each missing piece is named, nothing is installed. */
export function check(env = process.env) {
  const missing = [];
  const node = Number(process.versions.node.split(".")[0]);
  if (!(node >= 22) || typeof WebSocket !== "function") missing.push("node>=22 with a global WebSocket");
  const chrome = findChrome(env);
  if (!chrome) missing.push("a Chrome/Chromium binary (set DIAGRAM_CHROME or use the image's browser)");
  let provenance = null;
  try { provenance = JSON.parse(readFileSync(PROVENANCE, "utf8")); } catch { missing.push("PROVENANCE.json"); }
  try {
    const info = JSON.parse(readFileSync(BUILD_INFO, "utf8"));
    const actual = createHash("sha256").update(readFileSync(BUNDLE)).digest("hex");
    if (actual !== info.sha256) missing.push("assets/diagram-render.html matching BUILD_INFO.json sha256");
  } catch { missing.push("assets/diagram-render.html with assets/BUILD_INFO.json"); }
  return {
    ok: missing.length === 0,
    missing,
    node: process.versions.node,
    chrome,
    chrome_version: chrome ? browserVersion(chrome) : null,
    gstack_commit: provenance?.gstack?.commit ?? null,
    bundle_sha256: provenance?.bundle?.sha256 ?? null,
  };
}

function serveBundle() {
  const body = readFileSync(BUNDLE);
  const server = createServer((req, res) => {
    if (req.method === "GET" && req.url === "/diagram-render.html") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
      res.end(body);
    } else {
      res.writeHead(404).end();
    }
  });
  return new Promise((ok) => server.listen(0, "127.0.0.1", () => ok(server)));
}

function launchChrome(chrome, profileDir) {
  const child = spawn(chrome, [
    "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--no-first-run",
    "--no-default-browser-check", "--disable-extensions", "--disable-background-networking",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
    `--user-data-dir=${profileDir}`, "--remote-debugging-port=0", "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });
  const endpoint = new Promise((ok, fail) => {
    let seen = "";
    const timer = setTimeout(() => fail(new Error("Chrome did not expose a debugging endpoint: " + seen.slice(-300))), 30000);
    child.stderr.on("data", (chunk) => {
      seen += chunk;
      const match = seen.match(/DevTools listening on (ws:\/\/\S+)/);
      if (match) { clearTimeout(timer); ok(match[1]); }
    });
    child.on("exit", (code) => { clearTimeout(timer); fail(new Error(`Chrome exited early (${code}): ${seen.slice(-300)}`)); });
  });
  return { child, endpoint };
}

class Cdp {
  constructor(url) {
    this.next = 1; this.pending = new Map();
    this.ws = new WebSocket(url);
    this.ready = new Promise((ok, fail) => { this.ws.onopen = ok; this.ws.onerror = () => fail(new Error("CDP connection failed")); });
    this.ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      const waiter = message.id && this.pending.get(message.id);
      if (!waiter) return;
      this.pending.delete(message.id);
      message.error ? waiter.fail(new Error(message.error.message)) : waiter.ok(message.result);
    };
  }
  send(method, params = {}, sessionId) {
    const id = this.next++;
    this.ws.send(JSON.stringify({ id, method, params, sessionId }));
    return new Promise((ok, fail) => this.pending.set(id, { ok, fail }));
  }
}

async function evaluate(cdp, session, expression) {
  const done = await cdp.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, session);
  if (done.exceptionDetails) {
    throw new Error("render script did not finish: " + (done.exceptionDetails.exception?.description || done.exceptionDetails.text));
  }
  return done.result.value;
}

/** Render Mermaid source to `{ svg, png, pageErrors }`; throws on a parse or page error. */
export async function render(source, { width = 1950 } = {}) {
  const state = check();
  if (!state.ok) throw new Error("diagram capability unavailable: " + state.missing.join("; "));
  const profileDir = mkdtempSync(join(tmpdir(), "diagram-render-"));
  const server = await serveBundle();
  const { child, endpoint } = launchChrome(state.chrome, profileDir);
  try {
    const cdp = new Cdp(await endpoint);
    await cdp.ready;
    const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
    await cdp.send("Page.enable", {}, sessionId);
    await cdp.send("Page.navigate", { url: `http://127.0.0.1:${server.address().port}/diagram-render.html` }, sessionId);
    await evaluate(cdp, sessionId, `new Promise((ok, fail) => {
      const stop = Date.now() + 60000;
      const tick = () => document.querySelector('#done') ? ok(true)
        : Date.now() > stop ? fail(new Error('render bundle did not become ready')) : setTimeout(tick, 100);
      tick();
    })`);
    const absent = await evaluate(cdp, sessionId, `${JSON.stringify(PAGE_FUNCTIONS)}.filter(n => typeof window[n] !== 'function')`);
    if (absent.length) throw new Error("render bundle lacks page functions: " + absent.join(", "));
    const text = JSON.stringify(source);
    const svg = await evaluate(cdp, sessionId, `window.__renderMermaid('diagram-1', ${text})`);
    const png = await evaluate(cdp, sessionId, `window.__rasterize(${JSON.stringify(svg)}, ${Number(width)})`);
    const errors = await evaluate(cdp, sessionId, "JSON.stringify(window.__errors || [])");
    const data = Buffer.from(String(png).split(",", 2)[1] || "", "base64");
    if (!String(svg).includes("<svg")) throw new Error("render produced no SVG");
    if (data.length < PNG_SIGNATURE.length || !data.subarray(0, 8).equals(PNG_SIGNATURE)) throw new Error("render produced no PNG");
    return { svg, png: data, pageErrors: JSON.parse(errors) };
  } finally {
    child.kill("SIGKILL");
    server.close();
    rmSync(profileDir, { recursive: true, force: true });
  }
}

const USAGE = "usage: render.mjs --check | <diagram.mmd> [--out-dir DIR] [--slug NAME] [--width PX]";

function parse(argv) {
  const args = { out: null, slug: null, width: 1950, check: false, input: null };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--check") args.check = true;
    else if (a === "--out-dir") args.out = argv[++i];
    else if (a === "--slug") args.slug = argv[++i];
    else if (a === "--width") args.width = Number(argv[++i]);
    else if (a.startsWith("-") || args.input) throw new Error(USAGE);
    else args.input = a;
  }
  return args;
}

async function main() {
  const args = parse(process.argv.slice(2));
  if (args.check) {
    const state = check();
    console.log(JSON.stringify(state));
    process.exit(state.ok ? 0 : 1);
  }
  if (!args.input || !Number.isFinite(args.width) || args.width < 100) throw new Error(USAGE);
  const input = resolve(args.input);
  if (!SOURCE_EXTENSIONS.includes(extname(input).toLowerCase())) {
    throw new Error("input must be Mermaid source (.mmd); only Mermaid source, SVG and PNG are supported");
  }
  const slug = args.slug || basename(input, extname(input));
  if (!/^[A-Za-z0-9._-]+$/.test(slug)) throw new Error("slug must be letters, digits, dot, dash or underscore");
  const out = resolve(args.out || dirname(input));
  const source = readFileSync(input, "utf8");
  const result = await render(source, { width: args.width });
  mkdirSync(out, { recursive: true });
  writeFileSync(join(out, slug + ".svg"), result.svg);
  writeFileSync(join(out, slug + ".png"), result.png);
  // The Mermaid source stays beside the renders; it is the editable original.
  if (resolve(out, slug + ".mmd") !== input) writeFileSync(join(out, slug + ".mmd"), source);
  for (const ext of ["mmd", "svg", "png"]) console.log(`OK ${join(out, slug + "." + ext)}`);
  if (result.pageErrors.length) console.log("PAGE_ERRORS=" + JSON.stringify(result.pageErrors));
}

// The profile reaches this file through a skill directory that may be linked, so compare real paths.
if (process.argv[1] && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))) {
  main().catch((error) => { console.error("ERROR: " + error.message); process.exit(1); });
}
