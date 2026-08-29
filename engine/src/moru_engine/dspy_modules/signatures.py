"""DSPy signatures for the translation engine.

The docstrings below are SEED instructions only. Language-specific style
rules live HERE and nowhere else in the codebase; GEPA evolves them into
the compiled artifacts under engine/artifacts/. Do not scatter style
rules into handlers, pipeline, or prompts elsewhere.
"""

from __future__ import annotations

from typing import Literal

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
    - Consecutive numbered sibling keys (``...tooltip1``/``...tooltip2``,
      ``...desc.1``/``...desc.2``) are consecutive DISPLAY LINES of one
      text block, and they always arrive together, in key order, in the
      same batch. Read the whole run before translating any of its lines.
      When a line ends mid-sentence and the next line finishes it,
      translate the run as ONE sentence, then split that sentence back
      across exactly the same keys at a natural phrase boundary, giving
      each key roughly the share of the text its source line carried.
      Never leave a line as a dangling fragment, never repeat the same
      verb or clause on two lines, never move a clause onto a different
      key than the position it occupies in the sentence, and never merge
      the run into one key or omit a key — every key in the run must come
      back. Lines that are already complete sentences stay independent.
    - style_directives is BINDING and overrides the style guidance below
      whenever the two conflict. It carries the run's speech level
      (말투/register) and term-rendering preference. When it is empty,
      follow the guidance below unchanged.
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
    style_directives: str = dspy.InputField(
        desc="binding per-run style requirements (speech level, "
        "term-rendering preference); overrides general style guidance"
    )
    entries: dict[str, str] = dspy.InputField(
        desc="key -> source text with protected {{KIND}} tokens"
    )
    translations: dict[str, str] = dspy.OutputField(
        desc="exactly the same keys -> translated text"
    )


#: Selectable target speech level (말투). "auto" keeps whatever
#: per-surface register the compiled instructions already prescribe; the
#: others force one register across the whole pack.
SpeechLevel = Literal["auto", "polite", "banmal", "hage"]

#: Selectable preference for terms that have no established
#: target-language name. "auto" keeps today's split — official names
#: translated, invented portmanteaus transliterated.
TermStyle = Literal["auto", "translate", "transliterate"]

#: Unconditional: one speaker's lines drifting between 존댓말 and 반말 is
#: the defect this rule exists to stop, and it survives frontier models.
_REGISTER_CONSISTENCY = (
    "Speech level consistency: pick ONE register per speaker and per "
    "surface, then hold it for every line of that speaker/surface. Lines "
    "belonging to one character, quest giver, or dialogue sequence — "
    "including a single sentence split across numbered sibling keys — MUST "
    "NOT mix registers; never switch politeness level mid-sentence, "
    "mid-line, or between one speaker's consecutive lines. System/UI text "
    "and NPC speech may each take their own register, but each must stay "
    "internally consistent across the entire pack."
)

#: Concrete endings per forced register. Korean-specific by nature.
_KOREAN_SPEECH_LEVELS: dict[str, str] = {
    "polite": (
        "Korean speech level: 존댓말 everywhere — statements end -습니다/"
        "-합니다, instructions end -하세요/-세요. Never drop into 반말, not "
        "even for NPC chatter or flavor text."
    ),
    "banmal": (
        "Korean speech level: 반말 (해체) everywhere — statements end -아/"
        "-어/-야/-지/-네, questions end -아?/-어?/-니?, commands end -아/-어/"
        "-라. Never use -습니다 or -요 forms, not even for UI text."
    ),
    "hage": (
        "Korean speech level: 하게체 throughout — the archaic familiar "
        "register that suits medieval and high-fantasy packs. Statements "
        "end -네/-(으)ㄹ세/-데, questions end -(느)ㄴ가?/-나?, commands end "
        "-게/-시게, suggestions end -세, and the listener is addressed as "
        "자네. Never mix in -습니다/-요 forms or plain 반말 endings."
    ),
}

_TERM_STYLES: dict[str, str] = {
    "translate": (
        "Term rendering: when a term has no established target-language "
        "name and both a meaning translation and a transliteration (음차) "
        "would read acceptably, prefer translating its meaning into the "
        "target language. Glossary rules and established official names "
        "still win, and genuine proper nouns are still not translated."
    ),
    "transliterate": (
        "Term rendering: when a term has no established target-language "
        "name and both a meaning translation and a transliteration (음차) "
        "would read acceptably, prefer transliterating the source term "
        "into the target script. Glossary rules and established official "
        "names still win, and this never licenses leaving source-script "
        "text in the output."
    ),
}


def render_style_directives(
    target_lang: str,
    speech_level: SpeechLevel = "auto",
    term_style: TermStyle = "auto",
) -> str:
    """Render the ``style_directives`` value for one run.

    The register-consistency rule always renders. The speech-level block
    is Korean-specific and renders only for a Korean target; "auto" on
    either dimension renders nothing extra, so a default run keeps the
    compiled instructions' own style guidance verbatim.
    """
    blocks = [_REGISTER_CONSISTENCY]
    if speech_level != "auto" and target_lang.startswith("ko"):
        blocks.append(_KOREAN_SPEECH_LEVELS[speech_level])
    if term_style != "auto":
        blocks.append(_TERM_STYLES[term_style])
    return "\n".join(blocks)


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
    """Score one candidate translation of a game-UI string against the
    official reference translation.

    - 1.0: same meaning and terminology as the reference; equally natural
      (or better) phrasing for the target language.
    - 0.7-0.9: correct meaning; minor terminology or register deviations.
    - 0.4-0.6: understandable but wrong terminology, awkward phrasing, or
      partially untranslated.
    - 0.1-0.3: substantial meaning errors or mostly untranslated.
    - 0.0: empty, unrelated, or corrupted output.
    The candidate need not match the reference word-for-word: a different
    but equally correct and natural phrasing scores high. Ignore {{...}}
    placeholder tokens; they are validated elsewhere.
    """

    source_text: str = dspy.InputField()
    reference_translation: str = dspy.InputField(
        desc="official vanilla translation (gold standard)"
    )
    candidate_translation: str = dspy.InputField()
    target_lang: str = dspy.InputField()
    score: float = dspy.OutputField(desc="0.0 to 1.0")
    issues: str = dspy.OutputField(
        desc="short list of quality issues; empty when none"
    )


class JudgeTranslationPair(dspy.Signature):
    """Compare two anonymized candidate translations (A and B) of one
    game-UI string against the official reference translation, then score
    each candidate independently on a 0-10 integer scale.

    - 10: matches the reference's meaning and terminology; equally natural
      (or better) phrasing for the target language.
    - 7-9: correct meaning; minor terminology or register deviations.
    - 4-6: understandable but wrong terminology, awkward phrasing, or
      partially untranslated.
    - 1-3: substantial meaning errors or mostly untranslated.
    - 0: empty, unrelated, or corrupted output.
    Candidate order carries no information — judge each on its own merits;
    identical candidates must receive identical scores. Ignore {{...}}
    placeholder tokens; they are validated elsewhere.
    """

    source_text: str = dspy.InputField()
    reference_translation: str = dspy.InputField(
        desc="official vanilla translation (gold standard)"
    )
    target_lang: str = dspy.InputField()
    translation_a: str = dspy.InputField()
    translation_b: str = dspy.InputField()
    verdict: str = dspy.OutputField(
        desc="one short sentence comparing A and B against the reference"
    )
    score_a: int = dspy.OutputField(desc="integer 0-10")
    score_b: int = dspy.OutputField(desc="integer 0-10")
