import { execFileSync, spawnSync } from "node:child_process";
import { copyFileSync, cpSync, existsSync, mkdirSync, mkdtempSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, "..");
const frontendDir = path.join(rootDir, "frontend");
const backendDir = path.join(rootDir, "backend");
const packDir = path.join(rootDir, "fnnas.imageduplicatescanner");
const serverDir = path.join(packDir, "app", "server");
const staticDir = path.join(serverDir, "static");
const outputFpk = path.join(rootDir, "fnnas.imageduplicatescanner.fpk");

function run(command, args, cwd) {
  execFileSync(command, args, {
    cwd,
    stdio: "inherit",
    shell: process.platform === "win32",
  });
}

function copyIfExists(source, target) {
  if (existsSync(source)) {
    copyFileSync(source, target);
  }
}

function resolvePythonRunner() {
  const explicit = process.env.FPK_PYTHON;
  const bundledCodexPython = path.join(
    os.homedir(),
    ".cache",
    "codex-runtimes",
    "codex-primary-runtime",
    "dependencies",
    "python",
    process.platform === "win32" ? "python.exe" : "python"
  );
  const candidates = explicit
    ? [{ command: explicit, args: [] }]
    : [
        { command: bundledCodexPython, args: [] },
        { command: "python", args: [] },
        { command: "py", args: ["-3"] },
        { command: "python3", args: [] },
      ];

  for (const candidate of candidates) {
    if (path.isAbsolute(candidate.command) && !existsSync(candidate.command)) continue;
    const probe = spawnSync(candidate.command, [...candidate.args, "-c", "import sys; print(sys.version)"], {
      cwd: rootDir,
      shell: false,
      stdio: "ignore",
    });
    if (probe.status === 0) return candidate;
  }

  throw new Error("No usable Python interpreter found for packaging. Set FPK_PYTHON if needed.");
}

const stageRoot = mkdtempSync(path.join(os.tmpdir(), "imgdupscan-frontend-"));
const stageFrontendDir = path.join(stageRoot, "frontend");

try {
  cpSync(frontendDir, stageFrontendDir, {
    recursive: true,
    filter: (source) => {
      const normalized = source.replaceAll("\\", "/");
      return !normalized.includes("/node_modules/") && !normalized.endsWith("/dist");
    },
  });

  run("npm", ["ci"], stageFrontendDir);
  run("npm", ["run", "build"], stageFrontendDir);

  rmSync(serverDir, { recursive: true, force: true });
  mkdirSync(staticDir, { recursive: true });

  copyFileSync(path.join(backendDir, "main.py"), path.join(serverDir, "main.py"));
  copyFileSync(path.join(backendDir, "requirements.txt"), path.join(serverDir, "requirements.txt"));
  cpSync(path.join(stageFrontendDir, "dist"), staticDir, { recursive: true });
  copyIfExists(path.join(rootDir, "README.md"), path.join(serverDir, "README.upstream.md"));

  const python = resolvePythonRunner();
  run(
    python.command,
    [...python.args, path.join(__dirname, "pack-native-fpk.py"), packDir, outputFpk],
    rootDir
  );

  console.log(`Native fnOS package directory is ready: ${packDir}`);
  console.log(`Native fnOS package archive is ready: ${outputFpk}`);
} finally {
  rmSync(stageRoot, { recursive: true, force: true });
}
