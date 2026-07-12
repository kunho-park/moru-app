import type { MoruBridge } from "../shared/bridge";

declare global {
  interface Window {
    /** absent in plain-browser dev; lib/bridge.ts provides a shim then */
    moru?: MoruBridge;
  }

  /** injected at build time by electron-vite `define` */
  const __APP_VERSION__: string;
}

export {};
