import { expect, test } from "bun:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { LocaleFlag } from "./LocaleFlag.tsx";

const REQUIRED_COLORS = {
  en_us: ["#B22234", "#3C3B6E"],
  ko_kr: ["#CD2E3A", "#0047A0"],
  ja_jp: ["#BC002D"],
  zh_cn: ["#DE2910", "#FFDE00"],
  zh_tw: ["#FE0000", "#000095"],
};

test("renders every glossary locale as a platform-independent SVG flag", () => {
  for (const [locale, colors] of Object.entries(REQUIRED_COLORS)) {
    const html = renderToStaticMarkup(
      createElement(LocaleFlag, { locale, className: "flag" }),
    );

    expect(html).toContain(`<svg`);
    expect(html).toContain(`data-locale="${locale}"`);
    expect(html).toContain(`viewBox="0 0 24 16"`);
    expect(html).not.toContain("linear-gradient");
    for (const color of colors) expect(html).toContain(color);
  }
});
