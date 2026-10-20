#!/usr/bin/env bun
const { spawn } = require("node:child_process");
const path = require("node:path");

const child = spawn(process.execPath, [
  "run", "--cwd", path.resolve(__dirname, ".."), "start", ...process.argv.slice(2),
], { stdio: "inherit" });
child.on("error", (error) => { console.error(error.message); process.exitCode = 1; });
child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  else process.exitCode = code ?? 1;
});
