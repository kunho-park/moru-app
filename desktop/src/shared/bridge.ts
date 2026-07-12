/**
 * The `window.moru` preload bridge surface shared by main, preload, and
 * renderer. Only structured-clonable data crosses this boundary.
 */

export interface EngineInfo {
  state: "starting" | "ready" | "restarting" | "failed";
  port: number | null;
  token: string | null;
  /** populated when state === "failed" */
  error?: string;
  restarts: number;
}

export interface ModpackProbe {
  exists: boolean;
  isDirectory: boolean;
  name: string;
  hasMods: boolean;
  hasConfig: boolean;
  hasKubejs: boolean;
  hasResourcepacks: boolean;
  /** number of .jar files directly under mods/ */
  modJarCount: number;
}

export interface DetectedInstance {
  launcher: "CurseForge" | "Modrinth" | "Prism" | "MultiMC";
  name: string;
  path: string;
}

export type UpdateState =
  | { status: "idle" }
  | { status: "checking" }
  | { status: "available"; version: string }
  | { status: "downloading"; percent: number; version: string }
  | { status: "ready"; version: string }
  | { status: "none" }
  | { status: "error"; message: string };

/** Result of a completed desktop login. */
export interface WebAccount {
  /** moru.gg desktop API token - store via secrets, never log. */
  token: string;
  name: string;
}

/**
 * Cross-origin HTTP via the main process. The renderer runs on file://
 * where browsers enforce CORS the web APIs don't answer; main-process
 * fetch is exempt. Body/response are JSON-serialized strings.
 */
export interface WebRequestInit {
  url: string;
  method?: "GET" | "POST" | "PATCH";
  /** Bearer token added as Authorization header */
  token?: string;
  /** JSON body (already stringified) */
  body?: string;
}

export interface WebResponse {
  status: number;
  /** raw response text (JSON for our APIs) */
  body: string;
}

export type DesktopPlatform = "win32" | "darwin" | "linux";

export interface MoruBridge {
  platform: DesktopPlatform;
  versions: { app: string; electron: string };

  engine: {
    getInfo(): Promise<EngineInfo>;
    onState(cb: (info: EngineInfo) => void): () => void;
  };

  pickFolder(): Promise<string | null>;
  pickFile(filters?: { name: string; extensions: string[] }[]): Promise<string | null>;
  saveFile(defaultPath?: string): Promise<string | null>;
  probeModpack(path: string): Promise<ModpackProbe>;
  detectInstances(): Promise<DetectedInstance[]>;
  pathForFile(file: File): string;

  openPath(path: string): Promise<void>;
  showItemInFolder(path: string): Promise<void>;
  openExternal(url: string): Promise<void>;

  win: {
    minimize(): void;
    toggleMaximize(): void;
    close(): void;
    isMaximized(): Promise<boolean>;
    onMaximizeChange(cb: (maximized: boolean) => void): () => void;
  };

  secrets: {
    get(key: string): Promise<string | null>;
    set(key: string, value: string): Promise<void>;
    delete(key: string): Promise<void>;
  };

  account: {
    /**
     * Opens `{webUrl}/auth/desktop-login?port={loopback}` in the system
     * browser and resolves when the OAuth callback hits the loopback
     * server. `null` = cancelled or timed out.
     */
    login(webUrl: string): Promise<WebAccount | null>;
    cancelLogin(): void;
  };

  /** main-process fetch for web APIs (CORS-exempt). https only. */
  webRequest(init: WebRequestInit): Promise<WebResponse>;

  /** renderer -> main: a translation job is running (quit confirmation). */
  setBusy(busy: boolean): void;

  updates: {
    check(): Promise<void>;
    install(): void;
    getState(): Promise<UpdateState>;
    onState(cb: (state: UpdateState) => void): () => void;
  };
}
