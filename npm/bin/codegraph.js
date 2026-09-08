#!/usr/bin/env node
"use strict";
const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const BIN_DIR = __dirname;
const IS_WIN = process.platform === "win32";
const args = process.argv.slice(2);

function read(f) {
  try { return fs.readFileSync(path.join(BIN_DIR, f), "utf8").trim(); }
  catch { return null; }
}

const mode = read(".mode");
let cmd, cmdArgs;

if (mode === "binary") {
  cmd = path.join(BIN_DIR, "codegraph-bin" + (IS_WIN ? ".exe" : ""));
  cmdArgs = args;
} else if (mode === "pipx" || mode === "python") {
  // pipx / pip --user put a `codegraph` script on PATH
  const py = read(".python");
  if (py && mode === "python") { cmd = py; cmdArgs = ["-m", "codegraph", ...args]; }
  else { cmd = "codegraph"; cmdArgs = args; }
} else {
  process.stderr.write(
    "code-graph is not installed. Try:\n" +
    "  pipx install code-graph      (recommended)\n" +
    "  pip install code-graph\n");
  process.exit(1);
}

const r = spawnSync(cmd, cmdArgs, { stdio: "inherit" });
if (r.error && r.error.code === "ENOENT") {
  process.stderr.write(
    `code-graph: '${cmd}' not found on PATH.\n` +
    "Reinstall:  npm rebuild code-graph   or   pipx install code-graph\n");
  process.exit(1);
}
process.exit(r.status === null ? 1 : r.status);
