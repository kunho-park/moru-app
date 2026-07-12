/**
 * moru.gg account session. The desktop API token lives in
 * the OS keychain via `moru.secrets`; this store only mirrors login state
 * for the UI. Never log the token.
 */

import { create } from "zustand";

import { moru } from "@/lib/bridge";
import { WEB_URL } from "@/lib/web";

const TOKEN_KEY = "web:token";
const NAME_KEY = "web:name";

interface AccountStore {
  status: "loading" | "guest" | "connected";
  /** true while the browser OAuth round-trip is in flight */
  pending: boolean;
  name: string | null;
  token: string | null;

  hydrate(): Promise<void>;
  /** Opens the system browser; resolves true when connected. */
  login(): Promise<boolean>;
  logout(): Promise<void>;
}

export const useAccount = create<AccountStore>()((set, get) => ({
  status: "loading",
  pending: false,
  name: null,
  token: null,

  hydrate: async () => {
    const [token, name] = await Promise.all([
      moru.secrets.get(TOKEN_KEY),
      moru.secrets.get(NAME_KEY),
    ]);
    if (token !== null && token.length > 0) {
      set({ status: "connected", token, name: name ?? "" });
    } else {
      set({ status: "guest", token: null, name: null });
    }
  },

  login: async () => {
    if (get().pending) return false;
    set({ pending: true });
    try {
      const account = await moru.account.login(WEB_URL);
      if (account === null) return false;
      await Promise.all([
        moru.secrets.set(TOKEN_KEY, account.token),
        moru.secrets.set(NAME_KEY, account.name),
      ]);
      set({ status: "connected", token: account.token, name: account.name });
      return true;
    } finally {
      set({ pending: false });
    }
  },

  logout: async () => {
    moru.account.cancelLogin();
    await Promise.all([
      moru.secrets.delete(TOKEN_KEY),
      moru.secrets.delete(NAME_KEY),
    ]);
    set({ status: "guest", token: null, name: null });
  },
}));

void useAccount.getState().hydrate();
