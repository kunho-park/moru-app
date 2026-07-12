/** Glossary screen.
 *
 * Wired to the real engine glossary API (api.glossary / api.putGlossary).
 * CSV export copies a CSV string to the clipboard (renderer cannot write
 * files); CSV import reads the picked file through a hidden <input type=file>
 * + FileReader (the only path that actually works from the renderer).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { LocaleFlag } from "@/components/LocaleFlag";
import { api } from "@/lib/api";
import { formatInt, formatRelative } from "@/lib/format";
import { WEB_URL } from "@/lib/web";
import { useSettings } from "@/stores/settings";
import type { GlossaryOrigin, GlossaryTerm } from "../../../shared/engine";

const SOURCE_LANG = "en_us" as const;

const TARGET_LOCALES = [
  { code: "ko_kr", short: "KO" },
  { code: "ja_jp", short: "JA" },
  { code: "zh_cn", short: "ZH-CN" },
  { code: "zh_tw", short: "ZH-TW" },
] as const;

const ORIGIN_STYLE: Record<GlossaryOrigin, string> = {
  vanilla: "text-accent bg-[rgba(61,220,132,0.08)]",
  extracted: "text-purple bg-[rgba(167,139,250,0.08)]",
  manual: "text-blue bg-[rgba(107,179,245,0.08)]",
  community: "text-amber bg-[rgba(245,180,84,0.08)]",
};

const ORIGINS: GlossaryOrigin[] = ["vanilla", "extracted", "manual", "community"];

const GRID = "grid grid-cols-[1.2fr_1.2fr_100px_90px] gap-3";

/* Windowed list rendering: synced glossaries reach tens of thousands of
 * rows, which stalls the DOM if mounted at once. Rows are fixed-height so
 * only the visible slice (plus overscan) is rendered. */
const ROW_HEIGHT = 44;
const LIST_MAX_HEIGHT = 540;
const OVERSCAN = 12;

/** Minimal quoted-CSV parser: handles "" escapes, commas/newlines in quotes, CRLF. */
function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        field += ch;
      }
    } else if (ch === '"') {
      inQuotes = true;
    } else if (ch === ",") {
      row.push(field);
      field = "";
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(field);
      field = "";
      rows.push(row);
      row = [];
    } else {
      field += ch;
    }
  }
  if (field.length > 0 || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  return rows.filter((r) => r.some((c) => c.trim() !== ""));
}

function csvField(value: string): string {
  return /[",\n\r]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
}

function OriginBadge({ origin }: { origin: GlossaryOrigin }) {
  const { t } = useTranslation();
  return (
    <span className={`px-1.5 py-0.5 font-mono text-[10px] ${ORIGIN_STYLE[origin]}`}>
      {t(`glossary.origin.${origin}`)}
    </span>
  );
}

function RowInput({
  value,
  onChange,
  placeholder,
  mono,
  autoFocus,
  onEnter,
  onEscape,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  mono?: boolean;
  autoFocus?: boolean;
  onEnter: () => void;
  onEscape: () => void;
}) {
  return (
    <input
      type="text"
      value={value}
      autoFocus={autoFocus}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter") onEnter();
        if (e.key === "Escape") onEscape();
      }}
      className={`w-full border border-edge bg-card px-2 py-1 text-[13px] text-text outline-none placeholder:text-text4 focus:border-accent ${mono ? "font-mono font-semibold" : ""}`}
    />
  );
}

function RowActionButtons({
  onSave,
  onCancel,
  disabled,
  saveLabel,
  cancelLabel,
}: {
  onSave: () => void;
  onCancel: () => void;
  disabled: boolean;
  saveLabel: string;
  cancelLabel: string;
}) {
  return (
    <div className="flex items-center justify-end gap-1">
      <button
        onClick={onSave}
        disabled={disabled}
        title={saveLabel}
        className="p-1 text-accent hover:text-accent-hi disabled:opacity-40"
      >
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.8">
          <path d="M2 6 L5 9 L10 3" />
        </svg>
      </button>
      <button
        onClick={onCancel}
        disabled={disabled}
        title={cancelLabel}
        className="p-1 text-text3 hover:text-text disabled:opacity-40"
      >
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.8">
          <path d="M3 3 L9 9 M9 3 L3 9" />
        </svg>
      </button>
    </div>
  );
}

export function GlossaryScreen() {
  const { t, i18n } = useTranslation();
  const lang: "ko" | "en" = i18n.language === "en" ? "en" : "ko";
  const queryClient = useQueryClient();

  const [targetLang, setTargetLang] = useState<string>(() => {
    const saved = useSettings.getState().targetLocale;
    return TARGET_LOCALES.some((l) => l.code === saved) ? saved : "ko_kr";
  });
  const [originFilter, setOriginFilter] = useState<"all" | GlossaryOrigin>("all");
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search);
  const [scrollTop, setScrollTop] = useState(0);
  const [adding, setAdding] = useState(false);
  const [editIndex, setEditIndex] = useState<number | null>(null);
  const [draftSource, setDraftSource] = useState("");
  const [draftTarget, setDraftTarget] = useState("");
  const [toast, setToast] = useState<{ id: number; text: string; tone: "ok" | "err" } | null>(null);

  const searchRef = useRef<HTMLInputElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const targetMeta = TARGET_LOCALES.find((l) => l.code === targetLang) ?? TARGET_LOCALES[0];

  const query = useQuery({
    queryKey: ["glossary", SOURCE_LANG, targetLang],
    queryFn: () => api.glossary(SOURCE_LANG, targetLang),
  });
  const terms: GlossaryTerm[] = useMemo(() => query.data?.terms ?? [], [query.data]);

  const saveMutation = useMutation({
    mutationFn: (next: GlossaryTerm[]) =>
      api.putGlossary({ source_lang: SOURCE_LANG, target_lang: targetLang, terms: next }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["glossary", SOURCE_LANG, targetLang] });
    },
    onError: () => showToast(t("glossary.toast.saveFailed"), "err"),
  });

  const tmStats = useQuery({ queryKey: ["tm-stats"], queryFn: api.tmStats });

  const syncMutation = useMutation({
    mutationFn: () => api.syncCommunity(WEB_URL, targetLang, SOURCE_LANG),
    onSuccess: (sync) => {
      void queryClient.invalidateQueries({ queryKey: ["glossary", SOURCE_LANG, targetLang] });
      void queryClient.invalidateQueries({ queryKey: ["tm-stats"] });
      if (sync.glossary === null && sync.tm === null) {
        showToast(t("glossary.sync.nothing"));
      } else if (sync.glossary?.updated === true || sync.tm?.updated === true) {
        showToast(
          t("glossary.sync.updated", {
            terms: sync.glossary?.terms ?? 0,
            entries: sync.tm?.entries ?? 0,
          }),
        );
      } else {
        showToast(t("glossary.sync.upToDate"));
      }
    },
    onError: (error) => showToast(t("glossary.sync.failed", { error: String(error) }), "err"),
  });

  const showToast = (text: string, tone: "ok" | "err" = "ok"): void => {
    setToast({ id: Date.now(), text, tone });
  };

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 2500);
    return () => window.clearTimeout(timer);
  }, [toast]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Reset transient row state when the language pair changes.
  useEffect(() => {
    setAdding(false);
    setEditIndex(null);
    setOriginFilter("all");
  }, [targetLang]);

  // Filter changes reshuffle row indices; stale scroll offsets would show
  // a blank window, so snap back to the top.
  useEffect(() => {
    listRef.current?.scrollTo({ top: 0 });
    setScrollTop(0);
  }, [targetLang, originFilter, deferredSearch]);

  const counts = useMemo(() => {
    const acc: Record<GlossaryOrigin, number> = { vanilla: 0, extracted: 0, manual: 0, community: 0 };
    for (const term of terms) acc[term.origin] += 1;
    return acc;
  }, [terms]);

  const visible = useMemo(() => {
    const q = deferredSearch.trim().toLowerCase();
    return terms
      .map((term, index) => ({ term, index }))
      .filter(({ term }) => originFilter === "all" || term.origin === originFilter)
      .filter(
        ({ term }) =>
          q === "" || term.source.toLowerCase().includes(q) || term.target.toLowerCase().includes(q),
      );
  }, [terms, originFilter, deferredSearch]);

  const windowStart = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const windowEnd = Math.min(
    visible.length,
    Math.ceil((scrollTop + LIST_MAX_HEIGHT) / ROW_HEIGHT) + OVERSCAN,
  );
  const windowed = visible.slice(windowStart, windowEnd);

  const beginEdit = (index: number): void => {
    setAdding(false);
    setEditIndex(index);
    setDraftSource(terms[index].source);
    setDraftTarget(terms[index].target);
  };

  const beginAdd = (): void => {
    setEditIndex(null);
    setDraftSource("");
    setDraftTarget("");
    // The add row mounts at the top of the list; surface it.
    listRef.current?.scrollTo({ top: 0 });
    setScrollTop(0);
    setAdding(true);
  };

  const cancelRow = (): void => {
    setAdding(false);
    setEditIndex(null);
  };

  const commitAdd = (): void => {
    const source = draftSource.trim();
    const target = draftTarget.trim();
    if (!source || !target) return;
    const next = [...terms];
    const existing = next.findIndex((term) => term.source === source);
    const entry: GlossaryTerm = { source, target, origin: "manual" };
    if (existing >= 0) next[existing] = entry;
    else next.unshift(entry);
    saveMutation.mutate(next, { onSuccess: () => setAdding(false) });
  };

  const commitEdit = (): void => {
    if (editIndex === null) return;
    const source = draftSource.trim();
    const target = draftTarget.trim();
    if (!source || !target) return;
    const next = terms.map((term, i) => (i === editIndex ? { ...term, source, target } : term));
    saveMutation.mutate(next, { onSuccess: () => setEditIndex(null) });
  };

  const removeTerm = (index: number): void => {
    saveMutation.mutate(terms.filter((_, i) => i !== index));
  };

  const exportCsv = (): void => {
    const csv = [
      "source,target",
      ...terms.map((term) => `${csvField(term.source)},${csvField(term.target)}`),
    ].join("\n");
    navigator.clipboard
      .writeText(csv)
      .then(() => showToast(t("glossary.toast.copied")))
      .catch(() => showToast(t("glossary.toast.copyFailed"), "err"));
  };

  const importCsvFile = (file: File): void => {
    const reader = new FileReader();
    reader.onload = () => {
      const rows = parseCsv(String(reader.result ?? ""));
      if (rows.length === 0) {
        showToast(t("glossary.toast.importFailed"), "err");
        return;
      }
      let start = 0;
      let si = 0;
      let ti = 1;
      const header = rows[0].map((c) => c.trim().toLowerCase());
      const hs = header.indexOf("source");
      const ht = header.indexOf("target");
      if (hs !== -1 && ht !== -1) {
        si = hs;
        ti = ht;
        start = 1;
      }
      const imported: GlossaryTerm[] = [];
      for (const row of rows.slice(start)) {
        const source = (row[si] ?? "").trim();
        const target = (row[ti] ?? "").trim();
        if (source && target) imported.push({ source, target, origin: "manual" });
      }
      if (imported.length === 0) {
        showToast(t("glossary.toast.importFailed"), "err");
        return;
      }
      const merged = [...terms];
      for (const entry of imported) {
        const existing = merged.findIndex((term) => term.source === entry.source);
        if (existing >= 0) merged[existing] = entry;
        else merged.push(entry);
      }
      saveMutation.mutate(merged, {
        onSuccess: () => showToast(t("glossary.toast.imported", { count: imported.length })),
      });
    };
    reader.onerror = () => showToast(t("glossary.toast.importFailed"), "err");
    reader.readAsText(file);
  };

  const busy = saveMutation.isPending;
  const isEmpty = !query.isPending && !query.isError && terms.length === 0;

  const filterChip = (active: boolean): string =>
    active
      ? "border border-accent bg-[rgba(61,220,132,0.08)] px-2.5 py-[5px] text-[11px] font-semibold text-accent"
      : "border border-edge bg-transparent px-2.5 py-[5px] text-[11px] font-semibold text-text2 hover:border-edge2 hover:text-text";

  return (
    <div className="animate-fade-in-up px-10 py-8">
      {/* Header */}
      <div className="mb-6 flex items-end justify-between">
        <div>
          <div className="mb-1.5 font-mono text-[12px] font-semibold tracking-[0.08em] text-text3 uppercase">
            <span className="text-accent">▍</span> {t("glossary.eyebrow")}
          </div>
          <h1 className="mb-1 text-[28px] font-bold tracking-[-0.02em] text-text">{t("glossary.title")}</h1>
          <p className="text-[13px] text-text2">{t("glossary.subtitle")}</p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => syncMutation.mutate()}
            disabled={syncMutation.isPending}
            title={t("glossary.sync.tooltip", {
              version: tmStats.data?.last_sync_version ?? "—",
            })}
            className="flex items-center gap-1.5 border border-edge bg-transparent px-3.5 py-2 text-[12px] font-semibold text-purple hover:border-purple disabled:opacity-50"
          >
            <svg
              width="12"
              height="12"
              viewBox="0 0 12 12"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              className={syncMutation.isPending ? "animate-pxspin" : undefined}
            >
              <path d="M10.5 6a4.5 4.5 0 1 1-1.3-3.2M10.5 1v2.5H8" />
            </svg>
            {syncMutation.isPending ? t("glossary.sync.running") : t("glossary.sync.button")}
          </button>
          <button
            onClick={() => fileRef.current?.click()}
            disabled={busy}
            title={t("glossary.importTooltip")}
            className="flex items-center gap-1.5 border border-edge bg-transparent px-3.5 py-2 text-[12px] font-semibold text-text2 hover:border-edge2 hover:text-text disabled:opacity-50"
          >
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M6 8 V1 M3 4 L6 1 L9 4" />
              <path d="M1 10 H11" />
            </svg>
            {t("glossary.importCsv")}
          </button>
          <button
            onClick={exportCsv}
            disabled={query.isPending || query.isError}
            title={t("glossary.exportTooltip")}
            className="flex items-center gap-1.5 border border-edge bg-transparent px-3.5 py-2 text-[12px] font-semibold text-text2 hover:border-edge2 hover:text-text disabled:opacity-50"
          >
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M6 1 V8 M3 5 L6 8 L9 5" />
              <path d="M1 10 H11" />
            </svg>
            {t("glossary.exportCsv")}
          </button>
          <button
            onClick={beginAdd}
            disabled={busy || query.isPending || query.isError}
            className="flex items-center gap-1.5 bg-accent px-3.5 py-2 text-[13px] font-bold text-bar hover:bg-accent-hi disabled:opacity-50"
          >
            <svg width="12" height="12" viewBox="0 0 12 12" shapeRendering="crispEdges">
              <rect x="5" y="1" width="2" height="10" fill="currentColor" />
              <rect x="1" y="5" width="10" height="2" fill="currentColor" />
            </svg>
            {t("glossary.addTerm")}
          </button>
        </div>
      </div>

      {/* Hidden CSV file input - renderer-side FileReader is the only working import path */}
      <input
        ref={fileRef}
        type="file"
        accept=".csv,text/csv"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) importCsvFile(file);
          e.target.value = "";
        }}
      />

      {/* Filter row */}
      <div className="flex items-center gap-2 border border-b-0 border-line2 bg-raised px-3.5 py-2.5">
        <div className="flex items-center gap-1.5 border border-accent bg-[rgba(61,220,132,0.08)] px-3 py-1.5">
          <LocaleFlag locale={SOURCE_LANG} className="h-3 w-[18px] shrink-0" />
          <span className="font-mono text-[11px] text-text">{SOURCE_LANG}</span>
          <svg width="8" height="8" viewBox="0 0 8 8" fill="none" stroke="#3DDC84" strokeWidth="1.8">
            <path d="M2 3 L6 3 M3 4 L5 4 M4 5 L4 5" />
          </svg>
          <LocaleFlag locale={targetMeta.code} className="h-3 w-[18px] shrink-0" />
          <select
            value={targetLang}
            onChange={(e) => setTargetLang(e.target.value)}
            className="cursor-pointer bg-transparent font-mono text-[11px] text-text outline-none"
          >
            {TARGET_LOCALES.map((locale) => (
              <option key={locale.code} value={locale.code} className="bg-panel text-text">
                {locale.code}
              </option>
            ))}
          </select>
        </div>

        <div className="mx-1 h-5 w-px bg-edge" />

        <button onClick={() => setOriginFilter("all")} className={filterChip(originFilter === "all")}>
          {t("glossary.filterAll")} {formatInt(terms.length)}
        </button>
        {ORIGINS.map((origin) => (
          <button key={origin} onClick={() => setOriginFilter(origin)} className={filterChip(originFilter === origin)}>
            {t(`glossary.origin.${origin}`)} {formatInt(counts[origin])}
          </button>
        ))}

        <div className="flex-1" />

        <div className="flex min-w-[240px] items-center gap-1.5 border border-edge bg-card px-2.5 py-1.5">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="#6A7C74" strokeWidth="1.5">
            <circle cx="5" cy="5" r="3.5" />
            <path d="M8 8 L11 11" />
          </svg>
          <input
            ref={searchRef}
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("glossary.searchPlaceholder")}
            className="flex-1 bg-transparent font-mono text-[12px] text-text outline-none placeholder:text-text4"
          />
          <span className="bg-bar px-1 py-px font-mono text-[10px] text-text4">⌘K</span>
        </div>
      </div>

      {/* Table */}
      <div className="border border-t-0 border-line2 bg-raised">
        {/* Columns */}
        {!isEmpty && (
          <div
            className={`${GRID} border-b border-line2 bg-hover px-3.5 py-2.5 font-mono text-[10px] font-bold tracking-[0.06em] text-text3 uppercase`}
          >
            <div>{t("glossary.col.source")}</div>
            <div>{t("glossary.col.target", { lang: targetMeta.short })}</div>
            <div>{t("glossary.col.origin")}</div>
            <div />
          </div>
        )}

        {/* Rows */}
        <div
          ref={listRef}
          onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
          className="max-h-[540px] overflow-y-auto"
        >
          {/* Add row */}
          {adding && (
            <div
              className={`${GRID} items-center border-b border-line border-l-[3px] border-l-accent bg-[rgba(61,220,132,0.03)] px-3.5 py-3`}
            >
              <RowInput
                value={draftSource}
                onChange={setDraftSource}
                placeholder={t("glossary.sourcePlaceholder")}
                mono
                autoFocus
                onEnter={commitAdd}
                onEscape={cancelRow}
              />
              <RowInput
                value={draftTarget}
                onChange={setDraftTarget}
                placeholder={t("glossary.targetPlaceholder")}
                onEnter={commitAdd}
                onEscape={cancelRow}
              />
              <div>
                <OriginBadge origin="manual" />
              </div>
              <RowActionButtons
                onSave={commitAdd}
                onCancel={cancelRow}
                disabled={busy}
                saveLabel={t("glossary.rowSave")}
                cancelLabel={t("glossary.rowCancel")}
              />
            </div>
          )}

          {/* Loading skeleton */}
          {query.isPending &&
            Array.from({ length: 8 }, (_, i) => (
              <div key={i} className={`${GRID} items-center border-b border-line px-3.5 py-3`}>
                <div className="h-3 animate-pxpulse bg-line" style={{ width: `${55 + ((i * 17) % 35)}%` }} />
                <div className="h-3 animate-pxpulse bg-line" style={{ width: `${40 + ((i * 23) % 40)}%` }} />
                <div className="h-3 w-[52px] animate-pxpulse bg-line" />
                <div />
              </div>
            ))}

          {/* Error */}
          {query.isError && (
            <div className="flex flex-col items-center gap-3 px-3.5 py-16 text-center">
              <div className="font-mono text-[12px] font-semibold text-red">{t("glossary.error.title")}</div>
              <div className="max-w-[420px] font-mono text-[11px] text-text3">
                {query.error instanceof Error ? query.error.message : String(query.error)}
              </div>
              <button
                onClick={() => void query.refetch()}
                className="mt-1 border border-edge px-3.5 py-2 text-[12px] font-semibold text-text2 hover:border-edge2 hover:text-text"
              >
                {t("glossary.error.retry")}
              </button>
            </div>
          )}

          {/* Empty state */}
          {isEmpty && !adding && (
            <div className="flex flex-col items-center gap-3 px-3.5 py-16 text-center">
              <div
                className="h-16 w-16"
                style={{
                  backgroundImage: "radial-gradient(#1F2B25 1.5px, transparent 1.5px)",
                  backgroundSize: "8px 8px",
                }}
              />
              <div className="text-[13px] font-semibold text-text">{t("glossary.empty.title")}</div>
              <div className="text-[12px] text-text3">{t("glossary.empty.desc")}</div>
              <button
                onClick={beginAdd}
                className="mt-1 flex items-center gap-1.5 bg-accent px-3.5 py-2 text-[13px] font-bold text-bar hover:bg-accent-hi"
              >
                <svg width="12" height="12" viewBox="0 0 12 12" shapeRendering="crispEdges">
                  <rect x="5" y="1" width="2" height="10" fill="currentColor" />
                  <rect x="1" y="5" width="10" height="2" fill="currentColor" />
                </svg>
                {t("glossary.empty.cta")}
              </button>
            </div>
          )}

          {/* Term rows — only the scrolled-to window is mounted; spacer
              padding keeps the scrollbar sized for the full list. */}
          {!query.isPending && !query.isError && visible.length > 0 && (
            <div
              style={{
                paddingTop: windowStart * ROW_HEIGHT,
                paddingBottom: (visible.length - windowEnd) * ROW_HEIGHT,
              }}
            >
              {windowed.map(({ term, index }) =>
                editIndex === index ? (
                  <div
                    key={`${term.source}-${index}`}
                    className={`${GRID} h-11 items-center border-b border-line border-l-[3px] border-l-accent bg-[rgba(61,220,132,0.03)] px-3.5`}
                  >
                    <RowInput
                      value={draftSource}
                      onChange={setDraftSource}
                      placeholder={t("glossary.sourcePlaceholder")}
                      mono
                      autoFocus
                      onEnter={commitEdit}
                      onEscape={cancelRow}
                    />
                    <RowInput
                      value={draftTarget}
                      onChange={setDraftTarget}
                      placeholder={t("glossary.targetPlaceholder")}
                      onEnter={commitEdit}
                      onEscape={cancelRow}
                    />
                    <div>
                      <OriginBadge origin={term.origin} />
                    </div>
                    <RowActionButtons
                      onSave={commitEdit}
                      onCancel={cancelRow}
                      disabled={busy}
                      saveLabel={t("glossary.rowSave")}
                      cancelLabel={t("glossary.rowCancel")}
                    />
                  </div>
                ) : (
                  <div
                    key={`${term.source}-${index}`}
                    className={`${GRID} group h-11 items-center border-b border-line px-3.5 hover:bg-raised-hover`}
                  >
                    <div className="truncate font-mono text-[13px] font-semibold text-text" title={term.source}>
                      {term.source}
                    </div>
                    <div className="truncate text-[13px] text-text" title={term.target}>
                      {term.target}
                    </div>
                    <div>
                      <OriginBadge origin={term.origin} />
                    </div>
                    {term.origin === "manual" ? (
                      <div className="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100">
                        <button
                          onClick={() => beginEdit(index)}
                          disabled={busy}
                          title={t("glossary.rowEdit")}
                          className="p-1 text-text3 hover:text-text disabled:opacity-40"
                        >
                          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5">
                            <path d="M8.5 1.5 L10.5 3.5 L4 10 L1.5 10.5 L2 8 Z" />
                          </svg>
                        </button>
                        <button
                          onClick={() => removeTerm(index)}
                          disabled={busy}
                          title={t("glossary.rowDelete")}
                          className="p-1 text-text3 hover:text-red disabled:opacity-40"
                        >
                          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5">
                            <path d="M2 3 H10 M4 3 V1.5 H8 V3 M3 3 L3.5 10.5 H8.5 L9 3" />
                          </svg>
                        </button>
                      </div>
                    ) : (
                      <div className="text-right font-mono text-[10px] text-text4">{t("glossary.readonly")}</div>
                    )}
                  </div>
                ),
              )}
            </div>
          )}

          {/* Search / filter no-match */}
          {!query.isPending && !query.isError && terms.length > 0 && visible.length === 0 && (
            <div className="px-3.5 py-3 text-center font-mono text-[11px] text-text3">{t("glossary.noMatch")}</div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between border-t border-line2 bg-hover px-3.5 py-2.5">
          <div className="font-mono text-[11px] text-text3">
            {query.isPending || query.isError
              ? "—"
              : t("glossary.footer.summary", {
                  count: formatInt(terms.length),
                  time: formatRelative(query.dataUpdatedAt, lang),
                })}
          </div>
          <div className="flex items-center gap-2 font-mono text-[11px] text-text3">
            <span className="text-text4">{t("glossary.footer.webPending")}</span>
            <button disabled title={t("glossary.footer.syncTooltip")} className="cursor-not-allowed text-text4">
              {t("glossary.footer.sync")}
            </button>
          </div>
        </div>
      </div>

      {/* Toast */}
      {toast && (
        <div
          key={toast.id}
          className={`animate-fade-in-up fixed right-6 bottom-6 z-50 border px-3.5 py-2.5 font-mono text-[12px] text-text ${toast.tone === "ok" ? "border-accent" : "border-red"} bg-raised`}
        >
          {toast.text}
        </div>
      )}
    </div>
  );
}
