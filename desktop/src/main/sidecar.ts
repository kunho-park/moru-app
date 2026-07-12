/**
 * Python engine sidecar lifecycle.
 *
 * Startup handshake: pick a free loopback port, generate a
 * session token, spawn the sidecar with both, poll /health until ready.
 * On crash: restart up to MAX_RESTARTS, then surface a failed state.
 *
 * Dev attach: when MORU_ENGINE_PORT + MORU_ENGINE_TOKEN are set, no process
 * is spawned - we attach to a manually started
 * `uv run python -m moru_engine.server`.
 */

import { type ChildProcess, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { EventEmitter } from "node:events";
import { existsSync } from "node:fs";
import net from "node:net";
import path from "node:path";

import { app } from "electron";

import type { EngineInfo } from "../shared/bridge";

const MAX_RESTARTS = 3;
const HEALTH_TIMEOUT_MS = 30_000;
const HEALTH_INTERVAL_MS = 250;
const SHUTDOWN_GRACE_MS = 5_000;

function delay(ms: number): Promise<void> {
  const { promise, resolve } = Promise.withResolvers<void>();
  setTimeout(resolve, ms);
  return promise;
}

async function pickFreePort(): Promise<number> {
  const { promise, resolve, reject } = Promise.withResolvers<number>();
  const server = net.createServer();
  server.unref();
  server.on("error", reject);
  server.listen(0, "127.0.0.1", () => {
    const address = server.address();
    if (address === null || typeof address === "string") {
      server.close();
      reject(new Error("could not determine free port"));
      return;
    }
    const { port } = address;
    server.close(() => resolve(port));
  });
  return promise;
}

interface SpawnPlan {
  command: string;
  args: string[];
  cwd?: string;
}

/**
 * Resolve how to launch the engine.
 * - packaged: bundled PyInstaller onedir under resources/engine/
 * - dev: `uv run python -m moru_engine.server` from the sibling engine repo dir
 */
function resolveSpawnPlan(port: number, token: string): SpawnPlan {
  const serverArgs = ["--port", String(port), "--token", token];
  if (app.isPackaged) {
    const bin = process.platform === "win32" ? "moru-engine-server.exe" : "moru-engine-server";
    return {
      command: path.join(process.resourcesPath, "engine", bin),
      args: serverArgs,
    };
  }
  const engineCwd =
    process.env.MORU_ENGINE_DIR ?? path.resolve(app.getAppPath(), "..", "engine");
  return {
    command: "uv",
    args: ["run", "python", "-m", "moru_engine.server", ...serverArgs],
    cwd: engineCwd,
  };
}

export class EngineSidecar extends EventEmitter {
  #child: ChildProcess | null = null;
  #info: EngineInfo = { state: "starting", port: null, token: null, restarts: 0 };
  #stopping = false;
  #attached = false;

  get info(): EngineInfo {
    return this.#info;
  }

  #setInfo(patch: Partial<EngineInfo>): void {
    this.#info = { ...this.#info, ...patch };
    this.emit("state", this.#info);
  }

  async start(): Promise<void> {
    const envPort = process.env.MORU_ENGINE_PORT;
    const envToken = process.env.MORU_ENGINE_TOKEN;
    if (envPort !== undefined && envToken !== undefined) {
      this.#attached = true;
      const port = Number(envPort);
      this.#setInfo({ state: "starting", port, token: envToken });
      await this.#waitHealthy(port);
      this.#setInfo({ state: "ready" });
      return;
    }
    await this.#spawnOnce();
  }

  async #spawnOnce(): Promise<void> {
    const port = await pickFreePort();
    const token = randomBytes(32).toString("hex");
    const plan = resolveSpawnPlan(port, token);
    if (app.isPackaged && !existsSync(plan.command)) {
      this.#setInfo({ state: "failed", error: `engine binary missing: ${plan.command}` });
      return;
    }
    this.#setInfo({ state: this.#info.restarts > 0 ? "restarting" : "starting", port, token });

    const env: NodeJS.ProcessEnv = { ...process.env, PYTHONUNBUFFERED: "1" };
    if (app.isPackaged) {
      // Frozen builds resolve optimized-prompt artifacts via env override
      // (engine artifacts_dir()); the repo-layout fallback is meaningless
      // inside an onedir bundle.
      env.MORU_ARTIFACTS_DIR = path.join(process.resourcesPath, "engine", "artifacts");
    }
    const child = spawn(plan.command, plan.args, {
      cwd: plan.cwd,
      stdio: ["ignore", "pipe", "pipe"],
      env,
    });
    this.#child = child;
    child.stdout?.on("data", (chunk: Buffer) => {
      // NOTE: session token never appears in engine logs (engine convention).
      console.log(`[engine] ${String(chunk).trimEnd()}`);
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      console.error(`[engine] ${String(chunk).trimEnd()}`);
    });
    child.on("exit", (code) => {
      this.#child = null;
      if (this.#stopping) return;
      console.error(`[engine] exited unexpectedly with code ${code}`);
      void this.#restart();
    });

    try {
      await this.#waitHealthy(port);
    } catch (error) {
      // Health never came up: kill the orphan and go through restart budget.
      child.kill();
      if (!this.#stopping) {
        console.error(`[engine] health check failed: ${String(error)}`);
        await this.#restart();
      }
      return;
    }
    this.#setInfo({ state: "ready" });
  }

  async #restart(): Promise<void> {
    if (this.#info.restarts >= MAX_RESTARTS) {
      this.#setInfo({
        state: "failed",
        error: `engine crashed ${this.#info.restarts + 1} times; giving up`,
      });
      return;
    }
    this.#setInfo({ restarts: this.#info.restarts + 1, state: "restarting" });
    await delay(500);
    if (!this.#stopping) await this.#spawnOnce();
  }

  async #waitHealthy(port: number): Promise<void> {
    const deadline = Date.now() + HEALTH_TIMEOUT_MS;
    while (Date.now() < deadline) {
      if (this.#stopping) throw new Error("sidecar stopped during startup");
      try {
        const res = await fetch(`http://127.0.0.1:${port}/health`, {
          signal: AbortSignal.timeout(1_000),
        });
        if (res.ok) return;
      } catch {
        // not up yet
      }
      await delay(HEALTH_INTERVAL_MS);
    }
    throw new Error(`engine /health not ready within ${HEALTH_TIMEOUT_MS}ms`);
  }

  /** Graceful shutdown: POST /shutdown, then SIGKILL after a grace period. */
  async stop(): Promise<void> {
    this.#stopping = true;
    const { port, token } = this.#info;
    const child = this.#child;
    if (this.#attached || port === null || token === null) return;
    try {
      await fetch(`http://127.0.0.1:${port}/shutdown`, {
        method: "POST",
        headers: { authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(2_000),
      });
    } catch {
      // engine already gone or unresponsive; fall through to kill
    }
    if (child === null || child.exitCode !== null) return;
    const { promise, resolve } = Promise.withResolvers<void>();
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      resolve();
    }, SHUTDOWN_GRACE_MS);
    child.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
    await promise;
  }
}
