export type LocaleFlagCode = "en_us" | "ko_kr" | "ja_jp" | "zh_cn" | "zh_tw";

interface LocaleFlagProps {
  locale: LocaleFlagCode;
  className?: string;
}

function FlagArtwork({ locale }: { locale: LocaleFlagCode }) {
  switch (locale) {
    case "en_us":
      return (
        <>
          <rect width="24" height="16" fill="#FFF" />
          <path
            fill="#B22234"
            d="M0 0h24v1.231H0zM0 2.462h24v1.231H0zM0 4.923h24v1.231H0zM0 7.385h24v1.231H0zM0 9.846h24v1.231H0zM0 12.308h24v1.231H0zM0 14.769h24V16H0z"
          />
          <rect width="9.6" height="8.615" fill="#3C3B6E" />
          <g fill="#FFF">
            <circle cx="1.4" cy="1.4" r="0.32" />
            <circle cx="4.8" cy="1.4" r="0.32" />
            <circle cx="8.2" cy="1.4" r="0.32" />
            <circle cx="3.1" cy="3.2" r="0.32" />
            <circle cx="6.5" cy="3.2" r="0.32" />
            <circle cx="1.4" cy="5" r="0.32" />
            <circle cx="4.8" cy="5" r="0.32" />
            <circle cx="8.2" cy="5" r="0.32" />
            <circle cx="3.1" cy="6.8" r="0.32" />
            <circle cx="6.5" cy="6.8" r="0.32" />
          </g>
        </>
      );
    case "ko_kr":
      return (
        <>
          <rect width="24" height="16" fill="#FFF" />
          <g transform="rotate(33 12 8)">
            <path d="M9 8a3 3 0 0 1 6 0H9Z" fill="#CD2E3A" />
            <path d="M9 8a3 3 0 0 0 6 0H9Z" fill="#0047A0" />
            <circle cx="10.5" cy="8" r="1.5" fill="#CD2E3A" />
            <circle cx="13.5" cy="8" r="1.5" fill="#0047A0" />
          </g>
          <g stroke="#111" strokeWidth="0.55" strokeLinecap="square">
            <path d="M2.1 3.2l4.2-1.8M2.5 4.1l4.2-1.8M2.9 5l4.2-1.8" />
            <path d="m17.3 13.7 1.8-.8m.9-.4 1.8-.8m-4.9 1.1 1.8-.8m.9-.4 1.8-.8m-4.9 1.1 1.8-.8m.9-.4 1.8-.8" />
            <path d="m17.2 2.3 1.8.8m.9.4 1.8.8m-4.9-1.1 4.2 1.8m-4.6-.9 1.8.8m.9.4 1.8.8" />
            <path d="m2.3 11.7 4.2 1.8m-3.8-2.7 1.8.8m.9.4 1.8.8m-4.1-3 4.2 1.8" />
          </g>
        </>
      );
    case "ja_jp":
      return (
        <>
          <rect width="24" height="16" fill="#FFF" />
          <circle cx="12" cy="8" r="3.2" fill="#BC002D" />
        </>
      );
    case "zh_cn":
      return (
        <>
          <rect width="24" height="16" fill="#DE2910" />
          <path
            fill="#FFDE00"
            d="m5 1.8.72 2.21h2.33L6.16 5.38l.72 2.22L5 6.23 3.12 7.6l.72-2.22-1.89-1.37h2.33L5 1.8Z"
          />
          <g fill="#FFDE00">
            <circle cx="9.2" cy="2.6" r="0.48" />
            <circle cx="10.7" cy="4.2" r="0.48" />
            <circle cx="10.5" cy="6.3" r="0.48" />
            <circle cx="8.9" cy="7.8" r="0.48" />
          </g>
        </>
      );
    case "zh_tw":
      return (
        <>
          <rect width="24" height="16" fill="#FE0000" />
          <rect width="12" height="8" fill="#000095" />
          <g fill="#FFF" stroke="#FFF" strokeWidth="0.55" strokeLinecap="round">
            <path d="M6 0.7v1.1M6 6.2v1.1M2.7 4H1.6M10.4 4H9.3M3.7 1.7l.8 1M8.3 6.3l-.8-1M8.3 1.7l-.8 1M3.7 6.3l.8-1" />
            <circle cx="6" cy="4" r="1.7" stroke="none" />
          </g>
          <circle cx="6" cy="4" r="1.05" fill="#000095" />
          <circle cx="6" cy="4" r="0.82" fill="#FFF" />
        </>
      );
  }
}

/** Platform-independent flags; Windows does not render regional emoji as flags. */
export function LocaleFlag({ locale, className }: LocaleFlagProps) {
  return (
    <svg
      aria-hidden="true"
      className={className}
      data-locale={locale}
      focusable="false"
      viewBox="0 0 24 16"
      xmlns="http://www.w3.org/2000/svg"
    >
      <FlagArtwork locale={locale} />
      <rect
        x="0.25"
        y="0.25"
        width="23.5"
        height="15.5"
        fill="none"
        stroke="#000"
        strokeOpacity="0.18"
        strokeWidth="0.5"
      />
    </svg>
  );
}
