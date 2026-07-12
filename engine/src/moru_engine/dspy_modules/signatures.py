"""DSPy signatures for the translation engine.

The docstrings below are SEED instructions only. Language-specific style
rules live HERE and nowhere else in the codebase; GEPA evolves them into
the compiled artifacts under engine/artifacts/. Do not scatter style
rules into handlers, pipeline, or prompts elsewhere.
"""

from __future__ import annotations

import dspy

from ..models import TermRule


class TranslateEntries(dspy.Signature):
    """Translate Minecraft modpack text entries from source_lang to target_lang.

    Hard rules:
    - Source text contains protected tokens like {{COLOR}}, {{RESET}},
      {{ARG}}, {{BR}}. The translation MUST contain every token exactly as
      many times as the source does — never invent, drop, or alter a token.
      A number appears only when one text mixes several different values of
      the same kind ({{COLOR1}} vs {{COLOR2}} are different colors); copy
      each token exactly as written.
    - Token meanings, so you can position them naturally for the target
      language:
      {{COLOR}} starts a color/format span and the next {{RESET}} ends
      it — after reordering words, each span must still wrap the same
      words it wrapped in the source. A source may open a span without
      closing it; that is intentional — use ONLY the tokens present in
      the source, never add a closing token yourself.
      {{ARG}} is a runtime value slot (a number, item or player name);
      move it wherever the value reads naturally.
      {{VAR}} and {{TAG}} are verbatim markup; keep them attached to
      the text they mark.
      {{BR}} is a line break; keep line structure.
    - The glossary is binding: when a glossary term appears in the source,
      its mapped target term MUST be used verbatim.
    - Return the same keys as the input; translate values only.
    - Never leave a value untranslated unless it is a proper noun that the
      glossary says to keep.
    - Korean (ko_kr): natural gamer-facing tone; never append the English
      original in parentheses or brackets; no romanization of items that
      have established Korean names.
    - Japanese (ja_jp): plain polite register for UI text.
    - Chinese (zh_cn / zh_tw): follow official Minecraft terminology of the
      respective variant.
    """

    source_lang: str = dspy.InputField(desc="source locale code, e.g. en_us")
    target_lang: str = dspy.InputField(desc="target locale code, e.g. ko_kr")
    context: str = dspy.InputField(
        desc="mod name, content type (quest/item/tooltip/guidebook), file path hint"
    )
    glossary: str = dspy.InputField(
        desc="binding term rules in 'source = target' form; MUST be followed"
    )
    entries: dict[str, str] = dspy.InputField(
        desc="key -> source text with protected {{KIND}} tokens"
    )
    translations: dict[str, str] = dspy.OutputField(
        desc="exactly the same keys -> translated text"
    )


class RefineTranslation(dspy.Signature):
    """Fix a translation that failed programmatic validation.

    Address every listed error. Keep the parts of the translation that are
    already correct. Every protected token from the source ({{COLOR}} opens
    a color span, {{RESET}} closes it, {{ARG}} is a value slot) must appear
    in the fix exactly as many times as in the source, copied exactly as
    written. The glossary is binding.
    """

    source: str = dspy.InputField(desc="source text (placeholders protected)")
    bad_translation: str = dspy.InputField(desc="current failing translation")
    validation_errors: str = dspy.InputField(desc="verbatim validator error list")
    glossary: str = dspy.InputField(desc="binding term rules, 'source = target'")
    target_lang: str = dspy.InputField(desc="target locale code")
    fixed_translation: str = dspy.OutputField(desc="corrected translation")


class CurateGlossaryTerms(dspy.Signature):
    """Curate glossary rules from term candidates mined across the whole
    modpack. Each candidate line is "term (xCOUNT) — e.g. context".

    Accept candidates that name game content (items, blocks, entities,
    mechanics, dimensions) and give each ONE consistent target-language
    translation. Reject generic vocabulary, sentence fragments, and player
    names. Translations must not conflict with the existing glossary.
    """

    candidates: str = dspy.InputField(
        desc="one mined candidate per line with corpus occurrence count "
        "and a usage context"
    )
    existing_glossary: str = dspy.InputField(desc="already-fixed term rules")
    target_lang: str = dspy.InputField(desc="target locale code")
    feedback: str = dspy.InputField(
        desc="schema errors from your previous attempt (empty on the first try); "
        "when set, fix exactly these problems - use only the allowed category "
        "literals and fill every required field on every rule"
    )
    term_rules: list[TermRule] = dspy.OutputField(
        desc="rules for accepted candidates; term_ko holds the "
        "target-language term, aliases the source term"
    )


class JudgeTranslationQuality(dspy.Signature):
    """Judge the fluency and adequacy of a game-text translation.

    Score 1.0 = fluent, natural, faithful; 0.0 = unusable. Judge meaning
    and register, not formatting tokens (those are checked elsewhere).
    """

    source_text: str = dspy.InputField()
    translated_text: str = dspy.InputField()
    target_lang: str = dspy.InputField()
    score: float = dspy.OutputField(desc="0.0 to 1.0")
    issues: str = dspy.OutputField(desc="short list of fluency/adequacy issues")
