import assert from "node:assert/strict";
import { cpSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = fileURLToPath(new URL("../", import.meta.url));

export async function packNativeTui(destination: string): Promise<string> {
  const sdkRoot = path.resolve(packageRoot, "../nexus-api-client");
  assert(existsSync(path.join(sdkRoot, "dist/index.js")), "Build the canonical API client with npm ci first");
  const manifest = JSON.parse(readFileSync(path.join(packageRoot, "package.json"), "utf8"));
  const sdk = JSON.parse(readFileSync(path.join(sdkRoot, "package.json"), "utf8"));
  const temporaryParent = realpathSync(tmpdir());
  const stage = mkdtempSync(path.join(temporaryParent, "nexus-tui-pack-"));
  try {
    for (const name of manifest.files) {
      const target = path.join(stage, name);
      mkdirSync(path.dirname(target), { recursive: true });
      cpSync(path.join(packageRoot, name), target, { recursive: true });
    }
    delete manifest.dependencies["@nexus-ai-fs/api-client"];
    manifest.imports["#nexus-api-client"] = "./vendor/api-client/dist/index.js";
    manifest.files.push("vendor/api-client");
    manifest.scripts = { start: manifest.scripts.start };
    delete manifest.devDependencies;
    writeFileSync(path.join(stage, "package.json"), JSON.stringify(manifest, null, 2) + "\n");
    const bundledSdk = path.join(stage, "vendor/api-client");
    mkdirSync(bundledSdk, { recursive: true });
    for (const name of sdk.files) cpSync(path.join(sdkRoot, name), path.join(bundledSdk, name), { recursive: true });
    delete sdk.scripts;
    delete sdk.devDependencies;
    writeFileSync(path.join(bundledSdk, "package.json"), JSON.stringify(sdk, null, 2) + "\n");
    mkdirSync(destination, { recursive: true });
    const child = Bun.spawn([process.execPath, "pm", "pack", "--ignore-scripts", "--destination", destination, "--quiet"], {
      cwd: stage, stdout: "inherit", stderr: "inherit",
    });
    assert.equal(await child.exited, 0, "Packing the terminal failed");
    return path.join(destination, `nexus-ai-fs-tui-${manifest.version}.tgz`);
  } finally {
    assert.equal(realpathSync(path.dirname(stage)), temporaryParent);
    assert(path.basename(stage).startsWith("nexus-tui-pack-") && !lstatSync(stage).isSymbolicLink());
    rmSync(stage, { recursive: true, force: true });
  }
}

if (import.meta.main) console.log(await packNativeTui(path.resolve(process.argv[2] ?? path.join(packageRoot, "dist"))));
