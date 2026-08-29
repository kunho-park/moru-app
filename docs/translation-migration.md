# Previous translation migration

## Goal

Let a user carry a high-quality translation from an older modpack release into a
newer release. Moru compares three user-selected inputs:

- **A**: old original modpack folder or CurseForge modpack ZIP
- **B**: old translation, supplied as a resource-pack ZIP/folder, an overrides
  ZIP/folder, or both
- **C**: the new original modpack folder already selected as the normal Moru
  translation target

Only text that is unchanged from A to C is reused from B. New or changed source
text follows the normal translation pipeline.

## User workflow

1. Select C through the existing modpack picker.
2. Optionally enable **Reuse a previous translation**.
3. Select A, and then select either or both B artifacts.
4. Scan and translate normally. Moru reports reused entries separately and
   translates the remaining diff.
5. Export the existing two installable artifacts: resource pack and overrides.

## Matching semantics

- Do not validate pack name, version, project ID, or declared compatibility
  between A, B, and C. A mismatch simply produces few or no reusable matches.
- Match by output channel, normalized logical file identity, and entry key.
- Reuse B only when the same logical entry exists in A and C and
  `A source text == C source text` exactly.
- Any source-text change, including punctuation or whitespace, is translated
  again. The prior translation is not automatically used as the result.
- A target translation already shipped by C wins over migrated B.
- Migrated entries are run-scoped and are not inserted into the global TM.

## Resource-pack and overrides behavior

- B resource packs match language/structured asset files back to A's original
  entries and feed the normal resource-pack output tree.
- B overrides match files relative to the modpack root and feed the normal
  overrides output tree.
- Font-related resource-pack assets are copied unchanged into the new output:
  font definitions and files under `font/`, bitmap glyphs under
  `textures/font/`, and font binaries referenced from another resource path.
  The same filter applies to Paxi/OpenLoader resource-pack ZIPs supplied as B;
  unrelated textures, sounds, and other assets are not carried forward.
  Translation patch JARs are used for text matching only; their old binaries
  and assets are not copied wholesale.
- Old override files are never copied wholesale. Only matched translated text
  is applied onto C's current file template, preventing old configuration from
  reverting new-version behavior.
- `pack.mcmeta` and `pack.png` are regenerated for C; old pack metadata does not
  replace Moru's output identity.

## Input and storage constraints

- MVP accepts local folders and ZIP files. CurseForge GUI control and automatic
  historical downloads are deferred.
- For a CurseForge export A, unchanged installed mod JAR and resource-pack ZIP
  payloads are proven by matching `(projectID, fileID)` against C and C itself
  is used as the old source for those archives. Modified addons and changed
  file IDs never take this shortcut. Inference never overrides an explicitly
  scanned A payload, and when two unchanged addons share one logical file
  identity, coordinates whose inferred source text disagrees become ambiguous
  instead of one of them silently winning.
- ZIP extraction must reject absolute paths, `..` traversal, links, and entries
  outside a per-run temporary directory.
- Archive extraction scratch is removed after indexing, and each nested
  archive's extraction is reclaimed as soon as that archive is indexed. The
  filtered resource asset cache remains only for the sidecar session so review
  edits can safely regenerate the output, and is removed when the sidecar
  shuts down.
- W2's parsed A/B/C index is reused only when a lightweight path/size/mtime
  fingerprint is unchanged at W4. If any migration input changed, W4
  automatically rebuilds the scan and migration index. Ordinary translation
  keeps v1.0's established behavior of reusing a matching completed W2 scan.
- Existing Moru behavior is unchanged when migration inputs are absent.

## Failure handling

- Missing or malformed A/B inputs fail the translate job with an actionable
  path-specific error.
- Unsupported B files are ignored and logged; one malformed translatable file
  does not cause unrelated matches to be reused incorrectly.
- Ambiguous logical-file matches are not reused automatically.
- A migration with zero matches is valid and falls back to ordinary
  translation, while surfacing a zero-match summary.

## Verification

- Unit tests for exact match, changed source, deleted/new keys, wrong A/B,
  ambiguous files, both output channels, and ZIP traversal rejection.
- Output tests proving font assets survive byte-for-byte while unrelated
  textures/sounds do not, `pack.mcmeta` targets C, and old override-only
  configuration does not leak into the result.
- Pipeline test proving migrated entries skip the LLM while changed entries
  reach it, with separate migration statistics.
- Engine pytest, desktop tests/typecheck/build, and a read-only dry run against
  the supplied Prominence v4.0.1 instance plus v4.0.0hf translation artifact.

### Prominence II real-data result (2026-08-13)

The dry run used the official CurseForge exports for v4.0.0hf (file 8595898)
and v4.0.1 (file 8627902). Both downloaded archives matched the SHA-1 stored in
CurseForge instance metadata. The supplied v4.0.0hf resource-pack and overrides
archives were used as B; no LLM was called.

- 75,812 C entries inspected across 1,481 source files
- 38,344 translations reused: 35,050 resource-pack, 3,294 overrides
- 13,453 entries left for normal translation; 63 translated coordinates had
  changed source text and were deliberately not reused
- 0 ambiguous coordinates after explicit A content took precedence over
  manifest-based addon inference
- 4 font-related assets are preserved by the final default filter (2 font
  definitions plus OTF/TTF files); the original broad-asset dry run retained
  15 files before this review refinement.
- The earlier broad-asset dry run generated 420 resource-pack files and 58
  overrides files. Translation reuse counts and override isolation were
  unchanged by narrowing the final asset filter; the final font-only copy was
  separately verified as the four files above.
- generated `pack.mcmeta` identified C as v4.0.1

## Deferred work

- CurseForge file-ID lookup/download and cached reconstruction of A.
- Fuzzy source matching or using changed B text as LLM context.
- Persisting migration pairs into community/global translation memory.
