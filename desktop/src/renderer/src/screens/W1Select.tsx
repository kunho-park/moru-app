/**
 * W1 - modpack selection. Drop zone + native folder
 * picker + auto-detected launcher instances + recent folders; a valid probe
 * (exists && isDirectory && hasMods) starts the wizard session.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";

import { moru } from "@/lib/bridge";
import { formatInt, packInitials } from "@/lib/format";
import { useRouter } from "@/stores/router";
import { useSessions } from "@/stores/sessions";
import { useSettings } from "@/stores/settings";
import { useWizard } from "@/stores/wizard";
import type { DetectedInstance, ModpackProbe } from "../../../shared/bridge";

type ProbeErrorKind = "noMods" | "notFound" | "notDirectory" | "zip" | "probeFailed";

const LAUNCHER_CLASS: Record<DetectedInstance["launcher"], string> = {
  CurseForge: "text-purple",
  Modrinth: "text-accent",
  Prism: "text-blue",
  MultiMC: "text-amber",
};

const DASHED_RULE_STYLE: React.CSSProperties = {
  backgroundImage: "linear-gradient(90deg, #24322B 50%, transparent 50%)",
  backgroundSize: "6px 1px",
};

function SectionHeader({ label, count }: { label: string; count?: number }) {
  return (
    <div className="mb-3 flex items-center gap-[10px]">
      <div className="font-mono text-[11px] font-semibold tracking-[0.08em] text-text3 uppercase">
        {label}
      </div>
      {count !== undefined && (
        <div className="bg-[rgba(61,220,132,0.08)] px-[6px] py-[2px] font-mono text-[11px] text-accent">
          {count}
        </div>
      )}
      <div className="h-px flex-1" style={DASHED_RULE_STYLE} />
    </div>
  );
}

interface PackRowProps {
  name: string;
  path: string;
  launcher?: DetectedInstance["launcher"];
  selected: boolean;
  probe: ModpackProbe | null;
  translated: boolean;
  onSelect?: () => void;
}

function PackRow({ name, path, launcher, selected, probe, translated, onSelect }: PackRowProps) {
  const { t } = useTranslation();
  const body = (
    <>
      {selected && <div className="absolute inset-y-0 left-0 w-[3px] bg-accent" />}
      <div className="flex h-10 w-10 shrink-0 items-center justify-center border border-edge bg-card">
        <span className={`font-mono text-xs font-bold ${selected ? "text-accent" : "text-text2"}`}>
          {packInitials(name)}
        </span>
      </div>
      <div className="min-w-0 flex-1">
        <div className="mb-[2px] flex items-center gap-2">
          <span className="text-sm font-bold tracking-[-0.01em] text-text">{name}</span>
          {probe?.hasMods === true && (
            <span className="bg-[rgba(61,220,132,0.08)] px-[5px] py-[2px] font-mono text-[10px] text-accent">
              mods/
            </span>
          )}
          {probe?.hasConfig === true && (
            <span className="bg-bar px-[5px] py-[2px] font-mono text-[10px] text-text3">config</span>
          )}
          {probe?.hasKubejs === true && (
            <span className="bg-bar px-[5px] py-[2px] font-mono text-[10px] text-text3">kubejs</span>
          )}
          {translated && (
            <span className="bg-[rgba(245,180,84,0.08)] px-[5px] py-[2px] font-mono text-[10px] text-amber">
              {t("w1.badge.translated")}
            </span>
          )}
        </div>
        <div className="truncate font-mono text-[11px] text-text3">
          {launcher !== undefined && (
            <>
              <span className={LAUNCHER_CLASS[launcher]}>{launcher}</span>
              {" · "}
            </>
          )}
          {path}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-[10px]">
        {probe !== null && (
          <div className="text-right font-mono text-xs font-bold text-text">
            {t("w1.jarCount", { n: formatInt(probe.modJarCount) })}
          </div>
        )}
        {selected ? (
          <div className="flex h-5 w-5 items-center justify-center bg-accent">
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="#0A100D" strokeWidth="2">
              <path d="M2 6 L5 9 L10 3" />
            </svg>
          </div>
        ) : (
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="#4A5A52" strokeWidth="1.5">
            <path d="M4 2 L8 6 L4 10" />
          </svg>
        )}
      </div>
    </>
  );

  if (selected) {
    return (
      <div className="relative flex items-center gap-[14px] border border-accent-lo bg-tint px-4 py-[14px]">
        {body}
      </div>
    );
  }
  return (
    <button
      type="button"
      onClick={onSelect}
      className="flex w-full items-center gap-[14px] border border-line2 bg-raised px-4 py-[14px] text-left hover:border-edge2 hover:bg-raised-hover"
    >
      {body}
    </button>
  );
}

function SkeletonRow() {
  return (
    <div className="flex items-center gap-[14px] border border-line2 bg-raised px-4 py-[14px]">
      <div className="h-10 w-10 animate-pxpulse border border-edge bg-card" />
      <div className="min-w-0 flex-1">
        <div className="mb-2 h-3 w-44 animate-pxpulse bg-line2" />
        <div className="h-2 w-72 animate-pxpulse bg-line" />
      </div>
    </div>
  );
}

export function W1Select() {
  const { t } = useTranslation();
  const go = useRouter((s) => s.go);
  const modpackPath = useWizard((s) => s.modpackPath);
  const modpackName = useWizard((s) => s.modpackName);
  const probe = useWizard((s) => s.probe);
  const startSession = useWizard((s) => s.startSession);
  const startScan = useWizard((s) => s.startScan);
  const recentFolders = useSettings((s) => s.recentFolders);
  const sessions = useSessions((s) => s.sessions);

  const [dragOver, setDragOver] = useState(false);
  const dragDepth = useRef(0);
  const [probing, setProbing] = useState<string | null>(null);
  const [probeError, setProbeError] = useState<{ kind: ProbeErrorKind; path: string } | null>(null);

  const detected = useQuery({
    queryKey: ["detected-instances"],
    queryFn: () => moru.detectInstances(),
  });

  const translatedPaths = useMemo(
    () => new Set(sessions.filter((s) => s.status === "done").map((s) => s.modpackPath)),
    [sessions],
  );

  const detectedList = detected.data ?? [];
  const detectedPaths = useMemo(
    () => new Set((detected.data ?? []).map((d) => d.path)),
    [detected.data],
  );
  const recentList = recentFolders.filter((p) => p !== modpackPath && !detectedPaths.has(p));
  const selectedInDetected = modpackPath !== null && detectedPaths.has(modpackPath);

  const selectPath = useCallback(
    async (path: string) => {
      setProbeError(null);
      if (/\.zip$/i.test(path)) {
        setProbeError({ kind: "zip", path });
        return;
      }
      setProbing(path);
      try {
        const result = await moru.probeModpack(path);
        if (!result.exists) setProbeError({ kind: "notFound", path });
        else if (!result.isDirectory) setProbeError({ kind: "notDirectory", path });
        else if (!result.hasMods) setProbeError({ kind: "noMods", path });
        else startSession(path, result);
      } catch {
        setProbeError({ kind: "probeFailed", path });
      } finally {
        setProbing(null);
      }
    },
    [startSession],
  );

  const handlePickFolder = useCallback(async () => {
    const path = await moru.pickFolder();
    if (path !== null && path !== "") await selectPath(path);
  }, [selectPath]);

  const handlePickZip = useCallback(async () => {
    const path = await moru.pickFile([{ name: t("w1.drop.zipFilter"), extensions: ["zip"] }]);
    if (path !== null && path !== "") setProbeError({ kind: "zip", path });
  }, [t]);

  const handleNext = useCallback(() => {
    if (modpackPath === null) return;
    void startScan();
    go("w2");
  }, [modpackPath, startScan, go]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Enter" || e.defaultPrevented) return;
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === "BUTTON" || tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      handleNext();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [handleNext]);

  return (
    <div className="max-w-[1100px] animate-fade-in-up px-12 py-10">
      {/* Step label */}
      <div className="mb-2 flex items-center gap-[10px] font-mono text-[11px] font-semibold tracking-[0.08em] text-text3 uppercase">
        <span className="text-accent">01</span>
        <span>{t("common.wizard.step1")}</span>
        <div className="h-px flex-1" style={DASHED_RULE_STYLE} />
      </div>
      <h1 className="mb-[6px] text-[26px] font-bold tracking-[-0.02em] text-text">
        {t("w1.title")}
      </h1>
      <p className="mb-7 text-[13px] text-text2">{t("w1.subtitle")}</p>

      {/* Drop zone */}
      <div
        className={`relative mb-5 overflow-hidden border-2 border-dashed p-10 text-center ${
          dragOver ? "border-accent bg-tint" : "border-[#2A3B33] bg-hover hover:border-accent hover:bg-tint"
        }`}
        onDragEnter={(e) => {
          e.preventDefault();
          dragDepth.current += 1;
          setDragOver(true);
        }}
        onDragOver={(e) => {
          e.preventDefault();
        }}
        onDragLeave={() => {
          dragDepth.current = Math.max(0, dragDepth.current - 1);
          if (dragDepth.current === 0) setDragOver(false);
        }}
        onDrop={(e) => {
          e.preventDefault();
          dragDepth.current = 0;
          setDragOver(false);
          const file = e.dataTransfer.files.item(0);
          if (file !== null) {
            const path = moru.pathForFile(file);
            if (path !== "") void selectPath(path);
          }
        }}
      >
        <div className="absolute top-3 left-3 h-5 w-5 border-accent border-t-[3px] border-l-[3px]" />
        <div className="absolute top-3 right-3 h-5 w-5 border-accent border-t-[3px] border-r-[3px]" />
        <div className="absolute bottom-3 left-3 h-5 w-5 border-accent border-b-[3px] border-l-[3px]" />
        <div className="absolute right-3 bottom-3 h-5 w-5 border-accent border-r-[3px] border-b-[3px]" />
        <div
          className="pointer-events-none absolute inset-0"
          style={{
            backgroundImage:
              "radial-gradient(circle at 2px 2px, rgba(61,220,132,0.08) 1px, transparent 1px)",
            backgroundSize: "12px 12px",
          }}
        />

        <div className="relative flex flex-col items-center gap-4">
          <div className="flex items-end gap-1">
            <div className="h-8 w-5 border border-[#2A3B33] bg-line2" />
            <div className="h-11 w-5 border border-edge2 bg-edge" />
            <div className="h-6 w-5 border border-[#2A3B33] bg-line2" />
            <div className="h-10 w-6 border border-accent-hi bg-accent shadow-[0_0_20px_rgba(61,220,132,0.4)]" />
            <div className="h-7 w-5 border border-edge2 bg-edge" />
            <div className="h-9 w-5 border border-[#2A3B33] bg-line2" />
          </div>
          <div>
            <div className="mb-1 text-lg font-bold tracking-[-0.01em] text-text">
              {t("w1.drop.title")}
            </div>
            <div className="font-mono text-xs text-text3">
              {t("w1.drop.hintPre")}
              <span className="text-accent">mods/</span>
              {t("w1.drop.hintPost")}
            </div>
          </div>
          <div className="mt-1 flex gap-2">
            <button
              type="button"
              onClick={() => void handlePickFolder()}
              className="bg-accent px-[18px] py-[10px] text-[13px] font-bold text-sel-ink hover:bg-accent-hi"
            >
              {t("w1.drop.pickFolder")}
            </button>
            <button
              type="button"
              onClick={() => void handlePickZip()}
              className="border border-edge bg-transparent px-[18px] py-[10px] text-[13px] font-semibold text-text2 hover:border-edge2 hover:text-text"
            >
              {t("w1.drop.fromZip")}
            </button>
          </div>
        </div>
      </div>

      {/* Probe in flight */}
      {probing !== null && (
        <div className="mb-5 flex items-center gap-3 border border-line2 bg-raised px-4 py-3">
          <div className="h-2 w-2 animate-pxblink bg-accent" />
          <span className="shrink-0 font-mono text-xs text-text2">{t("w1.probing")}</span>
          <span className="truncate font-mono text-[11px] text-text3">{probing}</span>
        </div>
      )}

      {/* Invalid folder */}
      {probeError !== null && (
        <div className="mb-5 flex items-center gap-3 border border-[rgba(242,107,107,0.4)] bg-[rgba(242,107,107,0.06)] px-4 py-3">
          <div className="flex h-5 w-5 shrink-0 items-center justify-center bg-red">
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="#0A0F0C" strokeWidth="2">
              <path d="M6 2.5 V7" />
              <path d="M6 9.2 V9.6" />
            </svg>
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-semibold text-red">{t(`w1.error.${probeError.kind}`)}</div>
            <div className="truncate font-mono text-[11px] text-text3">{probeError.path}</div>
          </div>
          <button
            type="button"
            onClick={() => setProbeError(null)}
            className="shrink-0 border border-line2 px-3 py-[6px] text-xs font-semibold text-text2 hover:border-edge2 hover:text-text"
          >
            {t("common.action.close")}
          </button>
        </div>
      )}

      {/* Selection made outside the detected list (drop / picker / recent) */}
      {modpackPath !== null && !selectedInDetected && (
        <div className="mb-5">
          <PackRow
            name={modpackName}
            path={modpackPath}
            selected
            probe={probe}
            translated={translatedPaths.has(modpackPath)}
          />
        </div>
      )}

      {/* Detected launchers */}
      <SectionHeader label={t("w1.detected.label")} count={detected.data?.length} />
      {detected.isPending ? (
        <div className="flex flex-col gap-[6px]">
          <SkeletonRow />
          <SkeletonRow />
        </div>
      ) : detected.isError ? (
        <div className="flex items-center justify-between gap-3 border border-[rgba(242,107,107,0.4)] bg-[rgba(242,107,107,0.06)] px-4 py-3">
          <span className="text-xs text-red">{t("w1.detected.error")}</span>
          <button
            type="button"
            onClick={() => void detected.refetch()}
            className="shrink-0 border border-line2 px-3 py-[6px] text-xs font-semibold text-text2 hover:border-edge2 hover:text-text"
          >
            {t("common.action.retry")}
          </button>
        </div>
      ) : detectedList.length === 0 ? (
        <div className="border border-dashed border-line2 bg-card px-4 py-7 text-center">
          <div className="font-mono text-xs text-text3">{t("w1.detected.empty")}</div>
          <div className="mt-[6px] text-[11px] text-text4">{t("w1.detected.emptyHint")}</div>
        </div>
      ) : (
        <div className="flex flex-col gap-[6px]">
          {detectedList.map((inst) => {
            const isSelected = inst.path === modpackPath;
            return (
              <PackRow
                key={inst.path}
                name={isSelected ? modpackName : inst.name}
                path={inst.path}
                launcher={inst.launcher}
                selected={isSelected}
                probe={isSelected ? probe : null}
                translated={translatedPaths.has(inst.path)}
                onSelect={() => void selectPath(inst.path)}
              />
            );
          })}
        </div>
      )}

      {/* Recent folders */}
      {recentList.length > 0 && (
        <div className="mt-5">
          <SectionHeader label={t("w1.recent.label")} count={recentList.length} />
          <div className="flex flex-col gap-[6px]">
            {recentList.map((path) => (
              <PackRow
                key={path}
                name={path.split(/[\\/]/).filter(Boolean).at(-1) ?? path}
                path={path}
                selected={false}
                probe={null}
                translated={translatedPaths.has(path)}
                onSelect={() => void selectPath(path)}
              />
            ))}
          </div>
        </div>
      )}

      {/* Wizard footer */}
      <div className="mt-8 flex items-center justify-between border-t border-line pt-5">
        <button
          type="button"
          onClick={() => go("home")}
          className="flex items-center gap-[6px] px-[18px] py-[10px] text-[13px] font-semibold text-text2 hover:text-text"
        >
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5">
            <path d="M8 2 L4 6 L8 10" />
          </svg>
          {t("common.action.cancel")}
        </button>
        <div className="flex items-center gap-3">
          {modpackPath !== null && (
            <span className="font-mono text-[11px] text-text3">
              {t("w1.selectedStatus", { name: modpackName })}
            </span>
          )}
          <button
            type="button"
            disabled={modpackPath === null}
            onClick={handleNext}
            className="flex items-center gap-[6px] bg-accent px-5 py-[10px] text-[13px] font-bold text-sel-ink hover:bg-accent-hi disabled:cursor-not-allowed disabled:opacity-40"
          >
            {t("w1.next")}
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M4 2 L8 6 L4 10" />
            </svg>
          </button>
        </div>
      </div>
    </div>
  );
}
