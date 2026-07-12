/** Sidecar handshake state, mirrored from the main process. */

import { create } from "zustand";

import type { EngineInfo } from "../../../shared/bridge";
import { moru } from "../lib/bridge";

interface EngineStore {
  info: EngineInfo;
}

export const useEngineStore = create<EngineStore>(() => ({
  info: { state: "starting", port: null, token: null, restarts: 0 },
}));

export function bootEngineStore(): void {
  void moru.engine.getInfo().then((info) => useEngineStore.setState({ info }));
  moru.engine.onState((info) => useEngineStore.setState({ info }));
}
