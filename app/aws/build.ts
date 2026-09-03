import { build } from "esbuild";
import { rm } from "node:fs/promises";

async function main(): Promise<void> {
  await rm("dist", { recursive: true, force: true });
  await build({
    entryPoints: {
      "gateway/index": "gateway/index.ts",
      "lifecycle/index": "lifecycle/index.ts",
    },
    outdir: "dist",
    bundle: true,
    packages: "bundle",
    platform: "node",
    target: "node24",
    format: "cjs",
    legalComments: "none",
    treeShaking: true,
    keepNames: true,
    logLevel: "info",
  });
}

main().catch((error: unknown) => {
  console.error(error);
  process.exitCode = 1;
});
