/**
 * Startup gate: blocks the app until the sidecar handshake completes.
 * Renders loading / error / ready states.
 */

import { useTranslation } from "react-i18next";

import { MoruLogo } from "@/components/MoruLogo";
import { useEngineStore } from "@/stores/engine";

export function EngineGate({ children }: { children: React.ReactNode }) {
  const { t } = useTranslation();
  const info = useEngineStore((s) => s.info);

  if (info.state === "ready") return <>{children}</>;

  return (
    <div className="flex h-full w-full flex-col items-center justify-center gap-5 bg-bg">
      <div className={info.state === "failed" ? "opacity-40" : "animate-pxpulse"}>
        <MoruLogo width={48} height={40} />
      </div>
      {info.state === "failed" ? (
        <div className="flex max-w-md flex-col items-center gap-3 text-center">
          <div className="text-base font-bold text-text">{t("common.engine.failedTitle")}</div>
          {info.error !== undefined && (
            <div className="border border-line bg-bar px-3 py-2 font-mono text-[11px] text-red">
              {info.error}
            </div>
          )}
          <div className="flex gap-2">
            <button
              onClick={() => window.location.reload()}
              className="bg-accent px-4 py-2 text-[13px] font-bold text-sel-ink hover:bg-accent-hi"
            >
              {t("common.engine.retry")}
            </button>
            <button
              onClick={() => {
                if (info.error !== undefined) void navigator.clipboard.writeText(info.error);
              }}
              className="border border-edge px-4 py-2 text-[13px] font-semibold text-text2 hover:border-edge2 hover:text-text"
            >
              {t("common.engine.copyLog")}
            </button>
          </div>
        </div>
      ) : (
        <div className="font-mono text-xs text-text3">
          {info.state === "restarting"
            ? t("common.engine.restarting", { count: info.restarts })
            : t("common.engine.starting")}
        </div>
      )}
    </div>
  );
}
