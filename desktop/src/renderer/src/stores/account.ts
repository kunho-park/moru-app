/**
 * moru.gg account session. The desktop API token lives in
 * the OS keychain via `moru.secrets`; this store only mirrors login state
 * for the UI. Never log the token.
 */

import { create } from "zustand";

import { moru } from "@/lib/bridge";
import { WEB_URL, WebApiError, web } from "@/lib/web";

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
    if (token === null || token.length === 0) {
      set({ status: "guest", token: null, name: null });
      return;
    }
    set({ status: "connected", token, name: name ?? "" });
    // The keychain can miss the name (older logins) and it can change on
    // the web, so refresh it from the profile API. 401 means the token was
    // revoked -> back to guest; network failures keep the cached name.
    try {
      const profile = await web.me(token);
      if (get().token !== token) return; // logged out mid-flight
      await moru.secrets.set(NAME_KEY, profile.name);
      set({ name: profile.name });
    } catch (error) {
      if (error instanceof WebApiError && error.status === 401) {
        await get().logout();
      }
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
