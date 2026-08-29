/**
 * W5M - Hand translation. One entry at a time, keyboard-driven.
 *
 * This is not a variant of the review table. Review triages the few percent of
 * an automatic run that broke; this walks every entry deliberately. Committing
 * writes through the same LLM-free entry PATCH the review screen uses, so a
 * session needs no provider, no API key, and no network beyond the loopback
 * engine.
 *
 * Uncommitted text is written to local storage as it is typed (see
 * `stores/manual`), so closing the app mid-entry does not lose it.
 */

import type { ReactNode } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ManualContextPanel } from "@/components/ManualContextPanel";
import { ManualQueue } from "@/components/ManualQueue";
import { McText, TokenText, tokenColorMap, tokensOf } from "@/components/EntryText";
import { useRouter } from "@/stores/router";
import { refId, sameRun, useManual, visibleQueue } from "@/stores/manual";
import { useWizard } from "@/stores/wizard";

/** Milliseconds of idle typing before the draft is written to local storage. */
const DRAFT_IDLE_MS = 400;

export function W5Manual(): ReactNode {
  const { t } = useTranslation();
  const go = useRouter((s) => s.go);
  const translateJobId = useWizard((s) => s.translateJobId);
  const sourceLocale = useWizard((s) => s.sourceLocale);
  const targetLocale = useWizard((s) => s.targetLocale);

  const jobId = useManual((s) => s.jobId);
  const refs = useManual((s) => s.refs);
  const entries = useManual((s) => s.entries);
  const flags = useManual((s) => s.flags);
  const drafts = useManual((s) => s.drafts);
  const flaggedOnly = useManual((s) => s.flaggedOnly);
  const cursor = useManual((s) => s.cursor);
  const error = useManual((s) => s.error);
  const open = useManual((s) => s.open);
  const move = useManual((s) => s.move);
  const commit = useManual((s) => s.commit);
  const setDraft = useManual((s) => s.setDraft);
  const toggleFlag = useManual((s) => s.toggleFlag);
  const advanceToNextPending = useManual((s) => s.advanceToNextPending);

  const editRef = useRef<HTMLTextAreaElement>(null);
  const [colorPreview, setColorPreview] = useState(false);
  /** Local mirror of the editor so typing is not gated on the store write. */
  const [text, setText] = useState("");

  const queue = visibleQueue({ refs, flaggedOnly, flags, jobId });
  const ref = queue[cursor];
  const id = ref === undefined ? null : refId(ref);
  const entry = id === null ? undefined : entries[id];
  const jobDrafts = jobId === null ? {} : (drafts[jobId] ?? {});
  const jobFlags = jobId === null ? [] : (flags[jobId] ?? []);

  useEffect(() => {
    if (translateJobId !== null) void open(translateJobId);
  }, [translateJobId, open]);

  // Load the focused entry's text into the editor: the stored draft if one
  // survived, otherwise whatever the entry already carries.
  useEffect(() => {
    if (id === null) return;
    setText(jobDrafts[id] ?? entry?.translated_text ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, entry?.translated_text]);

  // Debounced persist. Typing stays local; the store (and localStorage) catch
  // up shortly after the user pauses.
  useEffect(() => {
    if (id === null) return;
    const committed = entry?.translated_text ?? "";
    if (text === (jobDrafts[id] ?? committed)) return;
    const timer = setTimeout(() => setDraft(text), DRAFT_IDLE_MS);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, id]);

  const siblings = useMemo(() => {
    if (ref === undefined) return [];
    // The queue groups a numbered run contiguously, so the run is the span of
    // neighbours sharing this file and content kind.
    const same = refs.filter((r) => r.file === ref.file);
    return same
      .map((r) => entries[refId(r)])
      .filter(
        (e): e is NonNullable<typeof e> =>
          e !== undefined && !(e.key === ref.key && e.file === ref.file),
      )
      .filter((e) => sameRun(e.key, ref.key))
      .slice(0, 6);
  }, [ref, refs, entries]);

  const insertAtCursor = (fragment: string): void => {
    const el = editRef.current;
    if (el === null) {
      setText((v) => v + fragment);
      return;
    }
    const start = el.selectionStart;
    const end = el.selectionEnd;
    setText((v) => v.slice(0, start) + fragment + v.slice(end));
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(start + fragment.length, start + fragment.length);
    });
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key === "Enter") {
        e.preventDefault();
        setDraft(text);
        void commit({ advance: !e.shiftKey });
        return;
      }
      if (e.altKey && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
        e.preventDefault();
        move(e.key === "ArrowDown" ? 1 : -1);
        return;
      }
      if (mod && e.key.toLowerCase() === "d" && entry !== undefined) {
        e.preventDefault();
        setText(entry.source_text);
        return;
      }
      if (mod && e.key.toLowerCase() === "b") {
        e.preventDefault();
        toggleFlag();
        advanceToNextPending();
        return;
      }
      if (e.key === "Escape" && entry !== undefined) {
        e.preventDefault();
        setText(entry.translated_text);
        return;
      }
      // Ctrl+1..9 inserts the nth placeholder token from the source. Typing a
      // token by hand is the most common way a manual translation breaks.
      if (mod && /^[1-9]$/.test(e.key) && entry !== undefined) {
        const tokens = sourceTokens(entry.source_text);
        const token = tokens[Number.parseInt(e.key, 10) - 1];
        if (token !== undefined) {
          e.preventDefault();
          insertAtCursor(token);
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const stepHeader = (
    <div className="mb-2 flex items-center gap-[10px] font-mono text-[11px] font-semibold tracking-[0.08em] text-text3 uppercase">
      <span className="text-accent">05</span>
      <span>{t("w5m.stepLabel")}</span>
      <div
        className="h-px flex-1"
        style={{
          backgroundImage: "linear-gradient(90deg, #24322B 50%, transparent 50%)",
          backgroundSize: "6px 1px",
        }}
      />
    </div>
  );

  if (translateJobId === null) {
    return (
      <div className="animate-fade-in-up px-10 py-[28px]">
        {stepHeader}
        <div className="flex flex-col items-center justify-center gap-2 border border-line2 bg-raised py-20">
          <h2 className="m-0 text-[18px] font-bold text-text">{t("w5m.empty.title")}</h2>
          <p className="m-0 max-w-[420px] text-center text-[13px] text-text2">
            {t("w5m.empty.desc")}
          </p>
          <button
            onClick={() => go("w1")}
            className="mt-3 bg-accent px-5 py-[10px] text-[13px] font-bold text-[#0A100D] hover:bg-accent-hi"
          >
            {t("w5m.empty.cta")}
          </button>
        </div>
      </div>
    );
  }

  const isDirty = entry !== undefined && text !== entry.translated_text;
  const colors =
    entry === undefined ? new Map<string, number>() : tokenColorMap(entry.source_text, text);

  return (
    <div className="animate-fade-in-up flex h-full min-h-0 flex-col px-10 py-[28px]">
      {stepHeader}

      <div className="mb-4 flex items-end justify-between gap-6">
        <div>
          <h1 className="m-0 mb-1 text-[24px] font-bold tracking-[-0.02em] text-text">
            {t("w5m.title")}
          </h1>
          <p className="m-0 text-[13px] text-text2">{t("w5m.subtitle")}</p>
        </div>
        <button
          onClick={() => setColorPreview((v) => !v)}
          className={`flex items-center gap-[6px] border px-[10px] py-[6px] text-[11px] font-semibold ${
            colorPreview
              ? "border-accent bg-[rgba(61,220,132,0.08)] text-accent"
              : "border-edge text-text2 hover:border-edge2 hover:text-text"
          }`}
        >
          {t("w5m.colorPreview")}
        </button>
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-[260px_1fr_300px] border border-line2 bg-raised">
        <ManualQueue />

        {/* Focus pane */}
        <div className="flex min-h-0 flex-col overflow-y-auto p-4">
          {entry === undefined ? (
            <div className="flex flex-1 items-center justify-center font-mono text-[11px] text-text3">
              {t("w5m.focus.none")}
            </div>
          ) : (
            <>
              <div className="mb-3 flex items-center gap-2">
                <span className="min-w-0 flex-1 truncate border border-edge bg-card px-[10px] py-[5px] font-mono text-[11px] text-text2">
                  {entry.key}
                </span>
                {jobFlags.includes(id as string) && (
                  <span className="shrink-0 border border-amber px-2 py-[4px] font-mono text-[10px] text-amber">
                    {t("w5m.focus.flagged")}
                  </span>
                )}
              </div>

              <div className="mb-1 font-mono text-[10px] font-bold tracking-[0.06em] text-text3 uppercase">
                {t("w5m.focus.source", {
                  lang: (sourceLocale.split("_")[0] ?? sourceLocale).toUpperCase(),
                })}
              </div>
              <div className="mb-4 border border-edge bg-card p-3 text-[13px] leading-[1.6] break-words text-text [word-break:keep-all]">
                <TokenText text={entry.source_text} colors={colors} />
              </div>

              <div className="mb-1 flex items-center justify-between">
                <span className="font-mono text-[10px] font-bold tracking-[0.06em] text-text3 uppercase">
                  {t("w5m.focus.target", {
                    lang: (targetLocale.split("_")[0] ?? targetLocale).toUpperCase(),
                  })}
                </span>
                {isDirty && (
                  <span className="font-mono text-[10px] text-amber">
                    {t("w5m.focus.unsaved")}
                  </span>
                )}
              </div>
              <textarea
                ref={editRef}
                value={text}
                onChange={(e) => setText(e.target.value)}
                spellCheck={false}
                className="min-h-[120px] w-full resize-y border border-edge bg-card p-3 text-[14px] leading-[1.7] text-text"
              />

              {colorPreview && (
                <div className="mt-3">
                  <div className="mb-1 font-mono text-[10px] font-bold tracking-[0.06em] text-text3 uppercase">
                    {t("w5m.focus.preview")}
                  </div>
                  <div className="border border-edge bg-card p-3 text-[13px] leading-[1.6] break-words text-text">
                    <McText text={text} />
                  </div>
                </div>
              )}

              <div className="mt-4 flex flex-wrap items-center gap-2">
                <button
                  onClick={() => {
                    setDraft(text);
                    void commit({ advance: true });
                  }}
                  disabled={text === ""}
                  className="flex shrink-0 items-center gap-2 bg-accent px-4 py-[10px] text-[13px] font-bold whitespace-nowrap text-[#0A100D] hover:bg-accent-hi disabled:cursor-default disabled:opacity-50"
                >
                  {t("w5m.focus.commitNext")}
                  <span className="bg-[rgba(10,16,13,0.15)] px-1 py-px font-mono text-[10px]">
                    ⌃↵
                  </span>
                </button>
                <button
                  onClick={() => {
                    setDraft(text);
                    void commit({ advance: false });
                  }}
                  disabled={text === ""}
                  className="shrink-0 border border-edge px-3 py-[10px] text-[12px] font-semibold whitespace-nowrap text-text2 hover:border-edge2 hover:text-text disabled:opacity-50"
                >
                  {t("w5m.focus.commitStay")}
                </button>
                <button
                  onClick={() => {
                    toggleFlag();
                    advanceToNextPending();
                  }}
                  className="shrink-0 border border-edge px-3 py-[10px] text-[12px] font-semibold whitespace-nowrap text-text2 hover:border-amber hover:text-amber"
                >
                  {t("w5m.focus.flagNext")}
                </button>
              </div>

              {error !== null && (
                <div className="mt-3 font-mono text-[11px] break-words text-red">{error}</div>
              )}
            </>
          )}
        </div>

        {entry === undefined ? (
          <div className="border-l border-line2" />
        ) : (
          <div className="min-h-0 border-l border-line2">
            <ManualContextPanel
              entry={entry}
              draft={text}
              siblings={siblings}
              onInsert={insertAtCursor}
            />
          </div>
        )}
      </div>

      {/* Footer: shortcuts + route out */}
      <div className="mt-4 flex items-center justify-between border-t border-line pt-3">
        <div className="flex flex-wrap gap-3 font-mono text-[10px] text-text4">
          <span>{t("w5m.hint.commit")}</span>
          <span>{t("w5m.hint.move")}</span>
          <span>{t("w5m.hint.copySource")}</span>
          <span>{t("w5m.hint.flag")}</span>
          <span>{t("w5m.hint.token")}</span>
          <span>{t("w5m.hint.revert")}</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => go("w5")}
            className="px-[14px] py-[8px] text-[12px] font-semibold text-text2 hover:text-text"
          >
            {t("w5m.backToReview")}
          </button>
          <button
            onClick={() => go("w6")}
            className="bg-accent px-5 py-[10px] text-[13px] font-bold text-[#0A100D] hover:bg-accent-hi"
          >
            {t("w5m.next")}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * Distinct placeholder tokens of the source, in order — the `Ctrl+1..9`
 * targets. Deduped because the shortcut is "insert the Nth token", not "insert
 * the Nth occurrence".
 */
function sourceTokens(source: string): string[] {
  const seen: string[] = [];
  for (const token of tokensOf(source)) {
    if (!seen.includes(token)) seen.push(token);
  }
  return seen;
}
