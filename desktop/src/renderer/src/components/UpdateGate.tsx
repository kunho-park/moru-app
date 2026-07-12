/**
 * Forced-update gate: while a newer release is KNOWN to exist
 * (available / downloading / ready) the whole app is blocked until the
 * update is installed. States where no update is known (idle, checking,
 * none, error, dev builds without a feed) pass through, so offline use
 * keeps working.
 */

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

// Relative imports keep this module loadable under `bun test` (the `@/`
// alias lives in the vite config, not in bun's resolver).
import { moru } from "../lib/bridge";
import { MoruLogo } from "./MoruLogo";
import type { UpdateState } from "../../../shared/bridge";

export function isUpdateBlocking(state: UpdateState): boolean {
  return (
    state.status === "available" ||
    state.status === "downloading" ||
    state.status === "ready"
  );
}

export function UpdateGate({ children }: { children: React.ReactNode }) {
  const { t } = useTranslation();
  const [state, setState] = useState<UpdateState>({ status: "idle" });

  useEffect(() => {
    let mounted = true;
    void moru.updates.getState().then((s) => {
      if (mounted) setState(s);
    });
    const off = moru.updates.onState(setState);
    return () => {
      mounted = false;
      off();
    };
  }, []);

  if (!isUpdateBlocking(state)) return <>{children}</>;

  const version =
    state.status === "available" ||
    state.status === "downloading" ||
    state.status === "ready"
      ? state.version
      : "";
  const percent =
    state.status === "downloading" ? Math.floor(state.percent) : null;

  return (
    <div className="flex h-full w-full flex-col items-center justify-center gap-5 bg-bg">
      <div className="animate-pxpulse">
        <MoruLogo width={48} height={40} />
      </div>
      <div className="flex max-w-md flex-col items-center gap-3 text-center">
        <div className="text-base font-bold text-text">
          {t("common.update.forceTitle")}
        </div>
        <div className="text-[12px] leading-relaxed text-text2">
          {t("common.update.forceDesc", { version })}
        </div>
        {state.status === "ready" ? (
          <button
            onClick={() => moru.updates.install()}
            className="bg-accent px-4 py-2 text-[13px] font-bold text-sel-ink hover:bg-accent-hi"
          >
            {t("common.update.installRestart")}
          </button>
        ) : (
          <div className="flex w-64 flex-col items-center gap-2">
            <div className="h-1.5 w-full bg-line2">
              <div
                className="h-full bg-accent transition-[width]"
                style={{ width: `${percent ?? 0}%` }}
              />
            </div>
            <div className="font-mono text-[11px] text-text3">
              {percent !== null
                ? t("common.update.downloading", { percent })
                : t("common.update.preparing")}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
