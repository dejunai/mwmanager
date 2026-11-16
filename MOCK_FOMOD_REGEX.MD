# Mock FOMOD: finding option instructions in readmes

Plan only; no code has changed. The aim is that every optional BAIN or folder choice in the option window shows the
author's own instruction for it, plus badges for "choose one", "requires X", "install after", "OpenMW only", and so on.

## What the archive folder contains

Survey of a Vortex Morrowind downloads folder (1,533 archives, listed without extracting; only the doc files of option
packages were extracted). Survey data, the extracted docs and the scripts are in the session scratchpad
(`...\scratchpad\mf\survey.json`, `docs\`, `samples.txt`) and are rebuilt by the corpus script in step 7.

| Layout | Archives |
|---|---|
| Simple (one payload) | 1,252 |
| BAIN: two or more numbered top folders (`00 Core`, `01 Patch`) | 173 |
| Multi: two or more unnumbered payload folders (`256px`, `512px`; `Vanilla version`, `Camonna Tong version`) | 35 |
| One `Optional`/`Extras`/`Patches` folder with choices inside | 8 |
| FOMOD (out of scope) | 64 |

- Of the 216 option packages, **122 have readable docs and 94 have none** (payload only).
- **Doc formats:** 307 .txt, 4 .rtf, 3 .md, 4 .html, 1 .doc, no PDFs. `.doc` (Word 97) is not read today.
- **Long docs:** 41 docs exceed the 12,000 characters `_read_docs` keeps, so their later sections (often the options) are cut.
- **Wrappers:** 12 packages wrap options in `Data Files`; 19 sit one or more wrapper folders deep.
- **Noise folders:** `__MACOSX` showed up as an "option"; names like `- Acid Formula` start with a dash.

### How the 529 options in documented packages are referred to

| Reference found in the readme | Options |
|---|---|
| Exact folder name (often also label and number) | 239 (45%) |
| Label only (text after the number, not the full name) | 46 |
| Number line only (`07 -`, `[07]`, `Option 7:`) | 66 |
| Plugin file name only | 20 |
| Nothing literal | 158 (30%) |

- **Fuzzy matching:** token overlap of 60% or more rescues 107 of the non-literal cases. At that threshold it also
  produced false matches; for example `07 Patch - Windows Glow Complete` matched a line about "Windows Glow - Anthology
  Solstheim". So fuzzy matching needs the safeguards in step 4.
- **Instruction cues** (in 117 readable readme sets):
  - choose-one wording: 43
  - requires / if you use: 68
  - install after / on top of / overwrite: 44
  - OpenMW: 38
  - MWSE: 31
  - quoted folder names: 18
  - German text: 5
  - always/must install core: 9
- **Phrasing seen:**
  - `00 Core --> Always install`
  - `Install 00 Core, then 01 OpenMW if you use OpenMW`
  - `- "08 Creature Buffs - No Disease Labels" folder if you use BTBGI Creature Buffs`
  - `02 Pherim Reflection Mapped (requires Pherims reflection maps) - install this on top of folder 01`
  - `02-04 - A few texture varieties` (a range)
  - `2. (Optional) Overwrite the .esp from 00 Core with the .esp from 01 Patch if you play with Solstheim`
  - `Einfach den Inhalt des "00 Default Red"-Ordners ... entpacken` (German)

## Weak points in the current `mock_fomod.py`

1. `_read_docs` truncates each doc to 12,000 characters before any matching.
2. `HEADING_RE` treats any numbered line as an option heading, so a numbered install step (`2. (Optional) Overwrite…`)
   is taken as option 2's section.
3. Token overlap counts tokens shared by every option in the package (usually the mod's name), so the first option's
   line wins for all of them.
4. A line can be claimed by several options; there is no "best owner" assignment.
5. Docs inside the option folder itself are mixed with the package docs instead of being preferred.
6. Nothing is used when there are no docs, although every plugin carries a description in its TES3 header (HEDR).

## Plan

### 1. Gather text

- **No early truncation:** read every doc whole (cap at about 200 KB) and cut only the final excerpt.
- **Order sources by precision:**
  1. docs inside the option's own folder
  2. package docs (`Docs/<plugin>/` after Tidy)
  3. the plugin's HEDR description
  4. the cached Nexus page text (`_nexus_page_text`)
- **`.doc`:** pull printable runs from the binary (a `[\x20-\x7E]{4,}` regex is enough for a fallback).
- **Ignore as options:** `__MACOSX`, `screenshots`, `images`, `docs`, `source`, `xcf`/`psd` folders.

### 2. Normalize lines before matching

Applied to a copy used for matching; the original text is what gets shown.

- Casefold; map `– — ‐` to `-` and curly quotes to `"`.
- Strip markdown (`#`, `**`, `__`), BBCode (`[b]`, `[size=…]`) and HTML; collapse whitespace.
- Strip a leading bullet: `^\s*(?:[-*•>]|\d+\.)\s+` (kept separately as "was a numbered step").

### 3. Keys for each option

- Folder name; option number; label (text after the number via `OPTION_RE`).
- **Label variants:** drop a leading `Patch -`, `Addon -`, `Optional -`; drop a trailing `- Merged`, `- Modular`,
  `(choose ONE)`, version numbers `\bv?\d+(\.\d+)+\b`, and resolutions `\b\d{3,4}(px|x\d{3,4})?\b|\b[1248]k\b`.
- Plugin file names in the option, with and without extension.
- **Distinctive tokens:** label tokens minus stopwords minus any token that appears in every option of the package.
  This one rule fixes most false matches.

### 4. Tiered matchers

Each matcher yields `(option, line index, score, tier)`. Afterwards each line goes to the option with the highest
score (ties to the earlier tier), so no line describes two options.

| Tier | Pattern (on normalized lines) | Score |
|---|---|---|
| T1 heading | the line is only the folder name, label, or `0*N` + separator + label: `^(?:option\|module\|step)?\s*[\[(]?0*N[\])]?\s*[-:.)]\s*<label variant>\s*:?\s*$` | 100 |
| T2 quoted or exact name | `"<folder name>"`, `'<folder name>'` or the bare folder name anywhere in the line | 80 |
| T3 plugin | `\b<plugin>\b` (with extension), not in a path-like line (`[\\/]` or `--\w+` within 10 characters) | 70 |
| T4 number line | `^[\[(]?0*N[\])]?\s*[-:.)]` where N is written zero-padded, as in the folder (`01`), **or** the line also has a distinctive token. Bare `1.` steps need a token. Ranges `0*A\s*-\s*0*B\s*[-:]` apply to every N in A..B | 60 |
| T5 fuzzy | 75% or more of distinctive tokens and at least 2 of them (or 1 if it appears in no other option) | 40 × coverage |

**Block extent:**
- T1: from the heading to the next heading of any option, a divider (`SECTION_DIVIDER_RE`, `^[-=*_]{3,}$`), or two
  blank lines.
- T2–T5: the line plus lines indented deeper or continuing the sentence, up to the first blank line or bullet.
- Cap at 1,800 characters.

### 5. Instruction badges from the block (and from folder names)

| Badge | Regex on the option's block or folder name |
|---|---|
| Required / preselect | `\b(always\|must\|required\|mandatory)\b.{0,30}\binstall` or folder `^0*0\s*[-_ ]?\s*(core\|main\|base\|required\|data files)\b` |
| Choose one | `\b(choose\|pick\|select\|use\|install)\s+(only\s+)?one\b\|mutually exclusive\|instead of\|\bor\b.{0,40}\bbut not both` or folder `\(choose one\)` |
| Requires X | `\b(requires?\|needs?)\b\s+(?P<x>[^.,;()]{3,60})\|\bif you (use\|have\|play with\|are using)\s+(?P<x2>[^.,;()]{3,60})\|\bpatch for\s+(?P<x3>[^.,;()]{3,60})` or folder `^(?:patch for\|\S+ patch -)\s*(?P<x>.+)` |
| Order | `\b(install\|load)\s+(this\s+)?(after\|on top of\|over\|before)\s+(?P<x>[^.,;]{3,60})\|\boverwrite\b` |
| Engine | `\bopenmw\b` / `\bmwse\b` (feeds the existing `_technology_choice`) |
| Language | folder or block names `\b(german\|deutsch\|french\|fran[cç]ais\|russian\|english)\b` |

**Matching "Requires X":** check X against installed and staged plugins and package names (casefolded, with
punctuation and `.esp`/`.esm` stripped). Then:
- if X is installed, the badge shows as met and the option is suggested;
- if X is missing, the option is shown unchecked with "needs X";
- if X cannot be resolved, show the text as is.

### 6. Doc-free signals (also cover the 94 packages without docs)

- **Shared number:** options with the same leading number (`10 - Option 1`, `10 -Option 2`, `10- Option 3`) are one
  "choose one" group, however the separator is spaced. This is the BAIN convention and the strongest doc-free signal:
  43 of the 211 option packages have it (62 groups, 155 options), for example `02 No MWSE - Merged` /
  `02 No MWSE - Modular` and the four `01 There Can Be Only One … Patch` folders. Compare the number as an integer, so
  `1`, `01` and `001` are the same group. A readme that says two of them go together overrides the grouping.
- **Same output path:** two options that ship the same file path are alternatives or order-dependent; group them as
  "choose one" unless a block says "on top of". This is reliable because it comes from the files, not the prose.
- **Variant names:** options whose labels differ only by `merged/modular`, a resolution, `vanilla/…version`, or a
  body/race variant are one "choose one" group.
- **No text found:** show the plugin's HEDR description; failing that, a content summary such as
  "3 plugins; replaces 120 meshes and 40 textures under `meshes/x`", and say in the window that no readme text was
  found.

### 7. Corpus and measurement

- **`tools/mock_fomod_corpus.py`** rebuilds the corpus from the user's archive folder: listings plus extracted docs
  only, into `outputs/` (git-ignored). The docs are third-party text, so they are not committed.
- **`tests/mock_fomod_golden.json`** holds about 40 hand-checked packages: the expected tier and the first line for
  each option. Include at least these cases:
  - Tamriel Rebuilt
  - `02 Pherim`
  - Creature Buffs merged/modular
  - Swamp Rocks resolutions
  - the German readmes
  - the `.doc` package
  - an install-step collision (`2. (Optional) Overwrite…`)
  - the `LUSH_UNDERWATER` command-line false hit
- **Unit tests** use small synthetic readmes that reproduce each pattern, not the real text.
- **Report per run:**
  - coverage: options with a T1–T4 match (today about 70% literal)
  - fuzzy share
  - golden accuracy
  - false-match count
- **Targets:** at least 85% coverage on documented packages with no golden regressions; every option gets either
  text or a content summary.

## Order of work

1. Corpus script and golden file (measure the baseline first).
2. Normalization and the full-length doc read.
3. Keys with distinctive tokens, and T1–T4 with single-owner assignment.
4. Badges and the "Requires X" resolution.
5. Doc-free grouping (shared output paths) and the HEDR fallback.
6. T5 fuzzy, tuned last against the golden file.
