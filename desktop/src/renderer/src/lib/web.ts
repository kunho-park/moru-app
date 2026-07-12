/**
 * moru.gg web API client (contracts/web-api.yaml). Calls carry the desktop
 * API token issued by /auth/desktop-login. Pack uploads go through the
 * engine (upload job), not this client - the engine owns the archive.
 *
 * Requests route through the main-process proxy (moru.webRequest): the
 * renderer runs on file:// where the browser enforces CORS that the web
 * API does not answer.
 */

import { moru } from "./bridge";

export const WEB_URL: string =
  window.localStorage.getItem("moru:web-url") ?? "https://moru.gg";

export class WebApiError extends Error {
  constructor(
    readonly status: number,
    detail: string,
  ) {
    super(detail);
    this.name = "WebApiError";
  }
}

async function request<T>(
  path: string,
  token?: string,
  init?: { method?: "GET" | "POST" | "PATCH"; body?: unknown },
): Promise<T> {
  const res = await moru.webRequest({
    url: `${WEB_URL}${path}`,
    method: init?.method ?? "GET",
    token,
    body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
  });
  let data: unknown = null;
  try {
    data = JSON.parse(res.body) as unknown;
  } catch {
    // non-JSON error body
  }
  if (res.status < 200 || res.status >= 300) {
    const detail = (data as { error?: string } | null)?.error ?? `HTTP ${res.status}`;
    throw new WebApiError(res.status, detail);
  }
  return data as T;
}

export interface WebProfile {
  id: string;
  name: string;
  bio: string | null;
  avatar: string | null;
  isAdmin: boolean;
  contributionScore: number;
}

/** Mirror of the web's Notification row; message text is composed app-side. */
export interface WebNotification {
  id: string;
  type: string;
  payload: {
    packId?: string;
    authorName?: string;
    claimerName?: string;
    modpackName?: string;
  };
  readAt: string | null;
  createdAt: string;
}

/** CurseForge modpack candidate from GET /api/modpacks/search. */
export interface ModpackSearchResult {
  id: number;
  name: string;
  slug: string;
  summary: string;
  logoUrl: string | null;
  author: string | null;
  downloads: number;
  gameVersions: string | null;
  url: string | null;
}

export const web = {
  me: (token: string) => request<WebProfile>("/api/me", token),

  notifications: (token: string) =>
    request<{ notifications: WebNotification[]; unread: number }>(
      "/api/notifications",
      token,
    ),

  /** Mark read; omit ids to mark everything. */
  markRead: (token: string, ids?: string[]) =>
    request<{ ok: boolean }>("/api/notifications", token, {
      method: "POST",
      body: ids !== undefined ? { ids } : {},
    }),

  /** CurseForge modpack search (upload identity confirmation). */
  searchModpacks: (query: string) =>
    request<{ results: ModpackSearchResult[] }>(
      `/api/modpacks/search?q=${encodeURIComponent(query)}`,
    ),
};
