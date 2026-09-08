#!/usr/bin/env node
/**
 * postinstall for the `code-graph` npm wrapper.
 *
 * Strategy, best first:
 *   1. Download the standalone binary for this OS/arch from the matching
 *      GitHub Release (fully self-contained — no Python needed).
 *   2. Fall back to bootstrapping the Python package: `pipx install code-graph`,
 *      then `pip install --user code-graph`.
 *
 * Whichever wins is recorded in bin/.mode so bin/codegraph.js knows what to run.
 */
"use strict";
const fs = require("fs");
const path = require("path");
const https = require("https");
const { execFileSync, spawnSync } = require("child_process");
const zlib = require("zlib");

const PKG = require("../package.json");
const VERSION = PKG.version;
const REPO = (PKG.repository && PKG.repository.url || "")
  .replace(/^git\+/, "").replace(/\.git$/, "").replace(/^.*github\.com\//, "");
const BIN_DIR = path.join(__dirname, "..", "bin");
const IS_WIN = process.platform === "win32";
const BIN_NAME = "codegraph-bin" + (IS_WIN ? ".exe" : "");
const BIN_PATH = path.join(BIN_DIR, BIN_NAME);
const MODE_PATH = path.join(BIN_DIR, ".mode");

fs.mkdirSync(BIN_DIR, { recursive: true });

function log(m) { process.stdout.write("[code-graph] " + m + "\n"); }
function setMode(m) { fs.writeFileSync(MODE_PATH, m + "\n"); }

// ---- asset name for this platform ---------------------------------------
function assetName() {
  const os = { win32: "windows", darwin: "macos", linux: "linux" }[process.platform];
  const arch = { x64: "x64", arm64: "arm64" }[process.arch];
  if (!os || !arch) return null;
  return `codegraph-${os}-${arch}` + (IS_WIN ? ".exe" : "");
}

function download(url, dest, redirects = 0) {
  return new Promise((resolve, reject) => {
    if (redirects > 8) return reject(new Error("too many redirects"));
    https.get(url, { headers: { "User-Agent": "code-graph-npm" } }, (res) => {
      if ([301, 302, 303, 307, 308].includes(res.statusCode)) {
        res.resume();
        return resolve(download(res.headers.location, dest, redirects + 1));
      }
      if (res.statusCode !== 200) {
        res.resume();
        return reject(new Error("HTTP " + res.statusCode + " for " + url));
      }
      const out = fs.createWriteStream(dest);
      const stream = /gzip/.test(res.headers["content-encoding"] || "")
        ? res.pipe(zlib.createGunzip()) : res;
      stream.pipe(out);
      out.on("finish", () => out.close(() => resolve(dest)));
      out.on("error", reject);
    }).on("error", reject);
  });
}

async function tryBinary() {
  const asset = assetName();
  if (!asset || !REPO || REPO.includes("YOU/")) return false;
  const url =
    `https://github.com/${REPO}/releases/download/v${VERSION}/${asset}`;
  try {
    log(`downloading ${asset} …`);
    await download(url, BIN_PATH);
    if (!IS_WIN) fs.chmodSync(BIN_PATH, 0o755);
    execFileSync(BIN_PATH, ["--version"], { stdio: "ignore" });
    setMode("binary");
    log("installed the standalone binary — no Python needed");
    return true;
  } catch (e) {
    try { fs.unlinkSync(BIN_PATH); } catch {}
    log("no prebuilt binary (" + e.message + ") — falling back to Python");
    return false;
  }
}

// ---- python fallback ---------------------------------------------------
function which(cmd) {
  const r = spawnSync(IS_WIN ? "where" : "which", [cmd], { encoding: "utf8" });
  return r.status === 0 ? r.stdout.split(/\r?\n/)[0].trim() : null;
}

function tryPython() {
  const py = which("python3") || which("python");
  if (!py) {
    log("Python 3.11+ not found. Install it, then run:  pip install code-graph");
    setMode("missing");
    return;
  }
  const ver = spawnSync(py, ["-c", "import sys;print(sys.version_info[:2])"],
    { encoding: "utf8" }).stdout.trim();
  if (which("pipx")) {
    log("pipx install code-graph …");
    if (spawnSync("pipx", ["install", "--force", "code-graph"],
        { stdio: "inherit" }).status === 0) { setMode("pipx"); return; }
  }
  log(`${py} -m pip install --user code-graph …  ${ver}`);
  const r = spawnSync(py, ["-m", "pip", "install", "--user", "--upgrade",
    "code-graph"], { stdio: "inherit" });
  setMode(r.status === 0 ? "python" : "missing");
  if (r.status === 0) {
    fs.writeFileSync(path.join(BIN_DIR, ".python"), py + "\n");
  }
}

(async () => {
  if (await tryBinary()) return;
  tryPython();
})();
