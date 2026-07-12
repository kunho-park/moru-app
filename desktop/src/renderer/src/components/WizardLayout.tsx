/** Wizard chrome: left step indicator + content pane. */

import { useTranslation } from "react-i18next";

import { formatDuration } from "@/lib/format";
import { modelDisplayName } from "@/lib/models";
import { useRouter, type Screen } from "@/stores/router";
import { useSettings } from "@/stores/settings";
import { selectedScanTotals, useWizard } from "@/stores/wizard";

const STEPS: { screen: Screen; num: string; key: string }[] = [
  { screen: "w1", num: "01", key: "step1" },
  { screen: "w2", num: "02", key: "step2" },
  { screen: "w3", num: "03", key: "step3" },
  { screen: "w4", num: "04", key: "step4" },
  { screen: "w5", num: "05", key: "step5" },
  { screen: "w6", num: "06", key: "step6" },
];

function CheckIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="#3DDC84" strokeWidth="1.8">
      <path d="M1 5 L4 8 L9 2" />
    </svg>
  );
}

export function WizardLayout({ children }: { children: React.ReactNode }) {
  const { t, i18n } = useTranslation();
  const screen = useRouter((s) => s.screen);
  const go = useRouter((s) => s.go);
  const wizard = useWizard();
  const settings = useSettings();

  const totals = selectedScanTotals(wizard);
  const presetLabel =
    settings.preset === "custom"
      ? modelDisplayName(settings.model)
      : `${t(`common.preset.${settings.preset}`)} · ${modelDisplayName(settings.model)}`;

  const stepDone: Record<Screen, boolean> = {
    w1: wizard.modpackPath !== null,
    w2: wizard.scanState === "done",
    w3: wizard.runState !== "idle",
    w4: wizard.runState === "done" || wizard.runState === "cancelled",
    w5: wizard.runState === "done" && screen !== "w5" && wizard.exportState !== "idle",
    w6: wizard.exportState === "done",
    home: false,
    onboarding: false,
    history: false,
    glossary: false,
    settings: false,
  };
  const reachable: Record<string, boolean> = {
    w1: true,
    w2: wizard.modpackPath !== null,
    w3: wizard.scanState === "done",
    w4: wizard.runState !== "idle",
    w5: wizard.runState === "done" || wizard.runState === "cancelled",
    w6: wizard.runState === "done" || wizard.runState === "cancelled",
  };
  const doneCount = STEPS.filter((s) => stepDone[s.screen]).length;

  const subLabel: Record<string, string | null> = {
    w1: wizard.modpackName !== "" ? wizard.modpackName : null,
    w2: wizard.scanState === "done" ? t("common.unit.files", { count: totals.files }) : null,
    w3: presetLabel,
    w4: null,
    w5: null,
    w6: null,
  };

  /* ETA: linear extrapolation from live throughput */
  const elapsed = wizard.startedAt !== null ? (Date.now() - wizard.startedAt) / 1000 : 0;
  const remainingEntries = Math.max(totals.entries - wizard.doneEntries, 0);
  const eta =
    wizard.runState === "running" && wizard.doneEntries > 0
      ? (elapsed / wizard.doneEntries) * remainingEntries
      : null;

  return (
    <div className="flex h-full">
      <aside className="w-60 shrink-0 border-r border-line bg-panel px-5 py-6">
        <div className="mb-1 font-mono text-[11px] font-semibold tracking-[0.08em] text-text3 uppercase">
          <span className="text-accent">▍</span> {t("common.wizard.title")}
        </div>
        <div className="mb-5 text-[13px] font-bold tracking-[-0.01em] text-text">
          {wizard.modpackName !== "" ? wizard.modpackName : "—"}
        </div>

        <div className="flex flex-col gap-0.5">
          {STEPS.map((step) => {
            const active = screen === step.screen;
            const done = stepDone[step.screen];
            const enabled = reachable[step.screen];
            return (
              <button
                key={step.screen}
                onClick={() => enabled && go(step.screen)}
                disabled={!enabled}
                className="relative flex items-center gap-2.5 px-3 py-2.5 text-left enabled:hover:bg-hover disabled:cursor-default"
              >
                {active && <div className="absolute top-2 bottom-2 left-0 w-[3px] bg-accent" />}
                <div
                  className={`flex h-[22px] w-[22px] shrink-0 items-center justify-center border border-edge font-mono text-[11px] font-bold ${
                    done || active ? "bg-card text-text" : "bg-raised text-text3"
                  }`}
                >
                  {step.num}
                </div>
                <div className="min-w-0 flex-1">
                  <div
                    className={`text-xs font-semibold ${done || active ? "text-text" : "text-text2"}`}
                  >
                    {t(`common.wizard.${step.key}`)}
                  </div>
                  {subLabel[step.screen] !== null && subLabel[step.screen] !== undefined && (
                    <div className="truncate font-mono text-[10px] text-text3">
                      {subLabel[step.screen]}
                    </div>
                  )}
                </div>
                {done && <CheckIcon />}
              </button>
            );
          })}
        </div>

        <div className="mt-6 border-t border-line pt-5">
          <div className="mb-1 font-mono text-[11px] text-text3">{t("common.wizard.eta")}</div>
          <div className="font-mono text-lg font-bold tracking-[-0.01em] text-text">
            {eta !== null ? formatDuration(eta, i18n.language === "en" ? "en" : "ko") : "—"}
          </div>
          <div className="mt-2.5 flex h-1 gap-px border border-line bg-bar">
            {STEPS.map((step) => (
              <div
                key={step.screen}
                className="flex-1"
                style={{ background: stepDone[step.screen] ? "#3DDC84" : undefined }}
              />
            ))}
          </div>
          <div className="mt-1.5 font-mono text-[10px] text-text3">
            {t("common.wizard.stepsDone", { done: doneCount })}
          </div>
        </div>
      </aside>

      <div className="flex-1 overflow-y-auto">{children}</div>
    </div>
  );
}
