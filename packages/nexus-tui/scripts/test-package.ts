import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { packNativeTui } from "./pack.js";

const packageRoot = fileURLToPath(new URL("../", import.meta.url));
const temporaryParent = realpathSync(tmpdir());
const temporary = mkdtempSync(path.join(temporaryParent, "nexus-tui-package-"));
const bun = process.execPath;
const digest = (file: string) => createHash("sha256").update(readFileSync(file)).digest("hex");
const env = { ...process.env };
for (const name of Object.keys(env)) if (name.startsWith("NEXUS_")) delete env[name];

async function run(args: string[], cwd: string, overrides: Record<string, string> = {}) {
  const child = Bun.spawn(args, { cwd, env: { ...env, ...overrides }, stdout: "pipe", stderr: "pipe" });
  const [stdout, stderr, code] = await Promise.all([
    new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited,
  ]);
  process.stdout.write(stdout);
  process.stderr.write(stderr);
  assert.equal(code, 0, `Command failed: ${args[1]}`);
  return stdout;
}

try {
  const tarball = await packNativeTui(temporary);
  assert(existsSync(tarball));
  const consumer = path.join(temporary, "consumer");
  mkdirSync(consumer);
  writeFileSync(path.join(consumer, "package.json"), JSON.stringify({ private: true,
    dependencies: { "@nexus-ai-fs/tui": `file:${tarball.replaceAll("\\", "/")}` },
    trustedDependencies: ["@nexus-ai-fs/tui"],
  }));
  await run([bun, "install"], consumer);
  const installedPackage = path.join(consumer, "node_modules/@nexus-ai-fs/tui");
  const installedRequire = createRequire(path.join(installedPackage, "package.json"));
  const installedSdk = installedRequire.resolve("#nexus-api-client");
  assert(!path.relative(consumer, installedSdk).startsWith(".."), "SDK must resolve inside the isolated installation");
  const sourceSdk = path.resolve(packageRoot, "../nexus-api-client/dist/index.js");
  assert.equal(digest(installedSdk), digest(sourceSdk), "Packed SDK must match the canonical build");
  const help = await run([bun, "run", "nexus-tui", "--help"], consumer);
  assert(help.includes("Terminal UI for Nexus") && help.includes("--search-url"));
  assert(!help.includes("package.json scripts"));
  if (process.platform !== "win32") {
    await run([bun, "test", "tests/tmux/pre-connection.test.ts", "tests/tmux/panels.test.ts", "tests/tmux/exit-status.test.ts"], packageRoot, {
      NEXUS_TUI_PACKAGED_BIN: path.join(installedPackage, "bin/nexus-tui.cjs"),
      NEXUS_TUI_PACKAGE_CWD: consumer,
    });
  }
  console.log(JSON.stringify({ packed_install: "passed", sdk_sha256: digest(installedSdk),
    tarball_bytes: statSync(tarball).size, actual_terminal: process.platform === "win32" ? "separate ConPTY acceptance" : "four tmux journeys" }));
} finally {
  assert.equal(realpathSync(path.dirname(temporary)), temporaryParent);
  assert(path.basename(temporary).startsWith("nexus-tui-package-") && !lstatSync(temporary).isSymbolicLink());
  rmSync(temporary, { recursive: true, force: true });
}
