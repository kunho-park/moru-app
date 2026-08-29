/**
 * Translation aids for the focused entry.
 *
 * Everything here is computed locally and needs no provider, no API key, and
 * no network: sibling lines come from the loaded queue using the same
 * numbered-key grouping the engine uses, and the placeholder / §-code checks
 * come from the shared token definitions in `EntryText`.
 *
 * The engine now serves richer aids — glossary rules scoped to this entry's
 * lang key, translation-memory matches, and the authoritative validator
 * verdict — via `GET /translate/{job}/entries/{key}/context` and
 * `POST /translate/{job}/validate`. Wiring those in is deliberately a separate
 * change: glossary scope matching in particular must NOT be reimplemented
 * here, because `key_scope` is resolved server-side per lang key and a second
 * copy of that logic in the renderer would drift and quietly show wrong terms.
 */

import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { Entry } from "../../../shared/engine";
import { STATUS_COLOR, StatusIcon, tokensOf } from "@/components/EntryText";
import { contentKind, fileLabel } from "@/stores/manual";

/** One token's expected vs. actual occurrence count. */
interface TokenBalance {
  token: string;
  source: number;
  draft: number;
}

function tokenBalances(source: string, draft: string): TokenBalance[] {
  const counts: Record<string, TokenBalance> = {};
  for (const token of tokensOf(source)) {
    counts[token] ??= { token, source: 0, draft: 0 };
    counts[token].source += 1;
  }
  for (const token of tokensOf(draft)) {
    counts[token] ??= { token, source: 0, draft: 0 };
    counts[token].draft += 1;
  }
  return Object.values(counts);
}

function Section({ title, children }: { title: string; children: ReactNode }): ReactNode {
  return (
    <div className="border border-edge bg-card">
      <div className="border-b border-edge px-[10px] py-[5px] font-mono text-[10px] font-bold tracking-[0.06em] text-text3 uppercase">
        {title}
      </div>
      <div className="px-[10px] py-2">{children}</div>
    </div>
  );
}

export function ManualContextPanel({
  entry,
  draft,
  siblings,
  onInsert,
}: {
  entry: Entry;
  draft: string;
  siblings: Entry[];
  onInsert: (text: string) => void;
}): ReactNode {
  const { t } = useTranslation();
  const label = fileLabel(entry.file);
  const balances = tokenBalances(entry.source_text, draft);
  const sectionCount = (entry.source_text.match(/§/g) ?? []).length;
  const draftSections = (draft.match(/§/g) ?? []).length;

  return (
    <div className="flex flex-col gap-2 overflow-y-auto p-3">
      {/* Where this string lives */}
      <Section title={t("w5m.context.origin")}>
        <div className="flex flex-col gap-[3px] font-mono text-[11px]">
          <div>
            <span className="text-text4">{t("w5m.context.mod")} </span>
            <span className="text-accent">{label.mod}</span>
          </div>
          <div>
            <span className="text-text4">{t("w5m.context.kind")} </span>
            <span className="text-text2">{contentKind(entry.key)}</span>
          </div>
          <div className="break-all text-text3">{entry.file}</div>
        </div>
      </Section>

      {/* Placeholder balance — the most common hand-translation error */}
      {(balances.length > 0 || sectionCount > 0) && (
        <Section title={t("w5m.context.tokens")}>
          <div className="flex flex-col gap-[4px]">
            {balances.map((b) => {
              const ok = b.source === b.draft;
              return (
                <button
                  key={b.token}
                  onClick={() => onInsert(b.token)}
                  title={t("w5m.context.insertToken")}
                  className="flex items-center gap-2 text-left hover:opacity-80"
                >
                  <span
                    className="border px-[3px] font-mono text-[11px]"
                    style={
                      ok
                        ? {
                            background: "rgba(61,220,132,0.12)",
                            borderColor: "rgba(61,220,132,0.3)",
                            color: "#3DDC84",
                          }
                        : {
                            background: "rgba(242,107,107,0.12)",
                            borderColor: "rgba(242,107,107,0.4)",
                            color: "#F26B6B",
                          }
                    }
                  >
                    {b.token}
                  </span>
                  <span
                    className="font-mono text-[10px]"
                    style={{ color: ok ? "#6A7C74" : "#F26B6B" }}
                  >
                    {b.draft} / {b.source}
                  </span>
                </button>
              );
            })}
            {sectionCount > 0 && (
              <div
                className="mt-1 font-mono text-[10px]"
                style={{ color: draftSections === sectionCount ? "#6A7C74" : "#F5B454" }}
              >
                {t("w5m.context.sectionCodes", {
                  draft: draftSections,
                  source: sectionCount,
                })}
              </div>
            )}
          </div>
        </Section>
      )}

      {/* Sibling lines — one sentence often runs across a numbered run */}
      {siblings.length > 0 && (
        <Section title={t("w5m.context.siblings")}>
          <div className="flex flex-col gap-2">
            <p className="m-0 font-mono text-[10px] leading-relaxed text-text4">
              {t("w5m.context.siblingsHint")}
            </p>
            {siblings.map((s) => (
              <div key={s.key} className="flex flex-col gap-[2px] border-l border-edge2 pl-2">
                <div className="flex items-center gap-[5px]">
                  <StatusIcon status={s.status} size={9} />
                  <span className="truncate font-mono text-[10px] text-text3">{s.key}</span>
                </div>
                <div className="text-[11px] leading-[1.5] text-text2">{s.source_text}</div>
                {s.translated_text !== "" && (
                  <div
                    className="text-[11px] leading-[1.5]"
                    style={{ color: STATUS_COLOR[s.status] }}
                  >
                    {s.translated_text}
                  </div>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Validation carried by the entry from the run that produced it */}
      {entry.errors.length > 0 && (
        <Section title={t("w5m.context.runIssues")}>
          <div className="flex flex-col gap-[4px]">
            {entry.errors.map((err, i) => (
              <div key={i} className="font-mono text-[10px] leading-relaxed text-amber">
                {err}
              </div>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}
