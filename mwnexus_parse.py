"""Pure helpers for reading a rendered Nexus mod page and matching option folders.

Used by mwnexus_browser (in-page capture script, result normalisation) and
mock_fomod (choosing the page sentence that explains an optional folder).
Nothing here touches the network, cookies or the browser profile.
"""
from __future__ import annotations

import html as _html
import json
import re

# Runs inside the page. Returns a JSON *string* so the result survives any
# bridge (pywebview / WebView2 ExecuteScript) unchanged; Python json.loads it.
# It never clicks, types or interacts with challenge widgets.
CAPTURE_JS = r"""
(function () {
  function clean(s) { return (s || '').replace(/\u00a0/g, ' ').replace(/[ \t]+\n/g, '\n').trim(); }
  function meta(sel) { var m = document.querySelector(sel); return m ? (m.getAttribute('content') || '') : ''; }
  var out = {url: location.href, title: document.title || '', readyState: document.readyState,
             selector: '', description: '', descriptionHtml: '', about: '', jsonld: [], text: ''};
  try {
    out.about = meta('meta[property="og:description"]') || meta('meta[name="description"]') ||
                meta('meta[name="twitter:description"]');
    var known = ['.mod_description_container', '#tab-description .container', '#tab-description',
                 '[data-e2e-id="mod-description"]', '[data-testid="mod-description"]',
                 'div.tab-description', '#description', '.mod-description', 'article.description'];
    var el = null;
    for (var i = 0; i < known.length && !el; i++) {
      var c = document.querySelector(known[i]);
      if (c && clean(c.innerText).length > 200) { el = c; out.selector = known[i]; }
    }
    if (!el) {
      // Generic fallback: Nexus descriptions are BBCode rendered as text nodes,
      // <br> and inline tags directly inside one container. Pick the element
      // with the most direct (non-nested-block) text, ignoring chrome.
      var best = null, bestScore = 0;
      var skip = /^(NAV|HEADER|FOOTER|SCRIPT|STYLE|NOSCRIPT|ASIDE|FORM|BUTTON|SELECT|TEXTAREA)$/;
      var inline = /^(A|B|I|U|S|EM|STRONG|SPAN|FONT|BR|SUB|SUP|SMALL|BIG|CENTER|IMG)$/;
      var all = document.body ? document.body.getElementsByTagName('*') : [];
      for (var j = 0; j < all.length; j++) {
        var node = all[j];
        if (skip.test(node.tagName) || node.closest('nav,header,footer,.comments,#comment-container,.forum')) continue;
        var score = 0;
        for (var k = 0; k < node.childNodes.length; k++) {
          var ch = node.childNodes[k];
          if (ch.nodeType === 3) score += ch.textContent.trim().length;
          else if (ch.nodeType === 1 && inline.test(ch.tagName)) score += (ch.textContent || '').trim().length * 0.8;
        }
        if (score > bestScore) { bestScore = score; best = node; }
      }
      if (best && bestScore > 300) { el = best; out.selector = 'heuristic:' + best.tagName.toLowerCase(); }
    }
    if (el) {
      out.description = clean(el.innerText).slice(0, 60000);
      out.descriptionHtml = (el.innerHTML || '').slice(0, 200000);
    }
    var ld = document.querySelectorAll('script[type="application/ld+json"]');
    for (var n = 0; n < ld.length && n < 5; n++) out.jsonld.push((ld[n].textContent || '').slice(0, 20000));
    out.text = clean(document.body ? document.body.innerText : '').slice(0, 120000);
  } catch (e) { out.error = String(e && e.message || e); }
  return JSON.stringify(out);
})()
"""

_CHALLENGE = re.compile(
    r"just a moment|checking your browser|verify you are human|verifying you are human|"
    r"security verification|complete the captcha|cloudflare.{0,30}(challenge|verification)|"
    r"enable javascript and cookies|performing security verification|attention required",
    re.I,
)


def is_challenge(title: str, text: str) -> bool:
    """True for Cloudflare/captcha interstitials (short pages with challenge wording)."""
    sample = f"{title}\n{text[:4000]}"
    if not _CHALLENGE.search(sample):
        return False
    # A real mod page can mention "captcha" in a comment; interstitials are short.
    return len(text) < 6000 or bool(re.search(r"just a moment", title or "", re.I))


def decode_script_result(value):
    """Normalise whatever a webview bridge returns for CAPTURE_JS into a dict or None."""
    for _ in range(3):  # tolerate double JSON encoding from different bridges
        if isinstance(value, dict):
            return value
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", "replace")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return None
            continue
        return None
    return value if isinstance(value, dict) else None


def _jsonld_descriptions(blocks) -> list[str]:
    found = []
    for block in blocks or []:
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                for key, value in item.items():
                    if key in {"description", "articleBody", "text"} and isinstance(value, str) and len(value) > 40:
                        found.append(_html.unescape(value))
                    elif isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(item, list):
                stack.extend(item)
    return found


def html_to_text(markup: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", markup or "")
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article)>", "\n", text)
    text = re.sub(r"(?i)<li\b[^>]*>", "\n- ", text)
    text = _html.unescape(re.sub(r"<[^>]+>", " ", text))
    return "\n".join(re.sub(r"[ \t\u00a0\ufeff\u200b]+", " ", line).strip() for line in text.splitlines())


def _tidy(text: str) -> str:
    lines = [re.sub(r"[ \t\u00a0\ufeff\u200b]+", " ", line).strip() for line in str(text or "").splitlines()]
    return "\n".join(line for line in lines if line)


def page_text_from_capture(page: dict) -> str:
    """Searchable text, most specific first: description, summary, JSON-LD, whole page."""
    parts = []
    description = _tidy(page.get("description", "")) or _tidy(html_to_text(page.get("descriptionHtml", "")))
    if description:
        parts.append(description)
    about = _tidy(page.get("about", ""))
    if about and about not in description:
        parts.append(about)
    for extra in _jsonld_descriptions(page.get("jsonld")):
        extra = _tidy(extra)
        if extra and extra not in "\n".join(parts):
            parts.append(extra)
    body = _tidy(page.get("text", ""))
    if body:
        parts.append("--- full page ---\n" + body)
    return "\n".join(parts)


# ---- option folder -> sentence matching ---------------------------------

_STOP = {"the", "and", "for", "with", "from", "this", "that", "data", "files", "file", "plugin",
         "option", "options", "optional", "install", "version", "mod", "mods", "folder", "esp", "esm"}


def stem(word: str) -> str:
    w = word.casefold()
    w = re.sub(r"['\u2019]s$", "", w).strip("'")
    for suffix in ("ements", "ement", "ments", "ment", "ings", "ing", "ers", "er", "ed", "es", "s", "e"):
        if len(w) - len(suffix) >= 4 and w.endswith(suffix):
            return w[: -len(suffix)]
    return w


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9']+", text)


def clean_label(label: str) -> str:
    """'01 Replacer' / '02 - Prayer_wheel' -> 'Replacer' / 'Prayer wheel'."""
    value = re.sub(r"[_]+", " ", str(label or "")).strip()
    value = re.sub(r"^\s*\d{1,3}\s*[-.:)]?\s*", "", value)
    return value.strip(" -")



def _plain_quotes(text: str) -> str:
    """Curly quotes to ASCII, so 'Petethegoat's' still matches a word pattern."""
    return (str(text or "").replace("‘", "'").replace("’", "'")
            .replace("“", '"').replace("”", '"'))


def _split_units(text: str) -> list[str]:
    units = []
    for raw in str(text or "").splitlines():
        line = raw.strip(" \t-*•\u2022\ufeff")
        if not line:
            continue
        if len(line) > 400:  # long paragraph: score sentences separately
            units.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"'])", line) if s.strip())
        else:
            units.append(line)
    return units


_CREDIT_HEADING = re.compile(
    r"^(?:credits?|thanks(?:\s+and\s+credits)?|credits?\s+and\s+thanks|"
    r"permissions?(?:\s+and\s+credits)?|file credits|author notes|"
    r"donation points?(?:\s+system)?|acknowledg(?:e)?ments?)\s*$",
    re.I,
)
_NAME_FOR = re.compile(
    r"^[A-Z][\w'.\-]+(?:(?:\s*,\s*|\s+and\s+|\s+&\s+|\s+aka\s+)[A-Z][\w'.\-]+)*"
    r"(?:\s+and\s+the\s+[A-Z][\w'.\-]+(?:\s+[A-Z][\w'.\-]+)*)?"
    r"\s+for\s+\S",
)
_INSTALLER_META = re.compile(
    r"\b(?:packed(?:\s+\w+)?\s+in\s+(?:bain|fomod|wrye(?:\s+mash)?|omod)\s+format|"
    r"choose the options that apply|"
    r"drop files(?:\s+from\s+\d{1,3}\s+\S+)?\s+into\s+(?:morrowind[/\\])?data\s+files|"
    r"copy\b.{0,100}\b(?:to|into)\b.{0,80}\bdata\s+files|"
    r"(?:can\s+(?:also\s+)?)?be\s+(?:also\s+)?installed\s+using\s+(?:a\s+)?(?:mod\s+manager|wrye\s+mash|vortex|mo2)|"
    r"install(?:ed)?\s+using\s+(?:a\s+)?(?:mod\s+manager|wrye\s+mash|vortex|mo2))\b",
    re.I,
)
# Stems too common on a mod page to mark a sentence as unique to one option.
_GENERIC_STEMS = frozenset({
    "sound", "music", "texture", "menu", "audio", "track", "image", "mesh", "replac",
})
_FOLDER_NUM_PREFIX = re.compile(r"\b\d{1,3}\s+(?=[A-Za-z])")
_DROP_AND_REPLACE = re.compile(r"\bdrop files(?:\s+and\s+replace)?\s+from\s+", re.I)
_QUALIFIER = re.compile(r"\s*-?\s*\(([^()]*)\)\s*$")
_THANKS_SENTENCE = re.compile(r"(?:^|(?<=[.!?])\s+)(?:(?:huge|many|special|big)\s+)?thanks\s+(?:to\s+)?[A-Z][^.!?]{0,70}[.!]", re.I)


def base_label(label: str) -> str:
    """'Clouds textures - (skies.iv)' -> 'Clouds textures': the name without a trailing variant qualifier."""
    name = clean_label(label)
    return _QUALIFIER.sub("", name).strip(" -") or name


def _lead_pattern(name: str):
    words = _words(name)
    if not words:
        return None
    # an optional folder number ("04 Clouds shader - ...") may precede the name
    return re.compile(r"^\W*(?:\d{1,3}\s*[-.:)]?\s+)?" + r"\W+".join(re.escape(w) for w in words)
                      + r"\s*(?:[-\u2013\u2014:]|\(|is\b|are\b|adds?\b|replaces?\b)", re.I)


def strip_thanks_sentences(text: str) -> str:
    """Drop a short trailing 'Huge thanks to X.' / 'Thanks X!' sentence from a description line."""
    out = _THANKS_SENTENCE.sub("", text or "").strip()
    return out or text


def _stems_of(label: str) -> list[str]:
    name = clean_label(label)
    return [stem(w) for w in _words(name) if w.casefold() not in _STOP and len(w) >= 3]


def is_credit_line(unit: str) -> bool:
    """True for a Credits/Thanks/Permissions heading, or a line that is just names plus 'for <thing>'."""
    line = (unit or "").strip(" \t-*•\u2022")
    if not line:
        return False
    return bool(_CREDIT_HEADING.match(line) or _NAME_FOR.match(line))


def is_installer_meta(unit: str) -> bool:
    """True for installer-format advice (BAIN packing, generic 'drop into Data Files', mod-manager install)."""
    return bool(_INSTALLER_META.search(unit or ""))


def _best_span(unit: str, stems: set[str]) -> tuple[int, int] | None:
    """The shortest character span that starts on a stem word and covers as many of `stems` as possible."""
    if not stems:
        return None
    words = list(re.finditer(r"[A-Za-z][A-Za-z0-9']+", unit))
    best = None  # (coverage, -length, start, end)
    for i, start in enumerate(words):
        if stem(start.group(0)) not in stems:
            continue
        have = set()
        for j in range(i, min(i + 6, len(words))):
            have.add(stem(words[j].group(0)))
            covered = have & stems
            if not covered:
                continue
            cand = (len(covered), -(words[j].end() - start.start()), start.start(), words[j].end())
            if best is None or cand[:2] > best[:2]:
                best = cand
    return (best[2], best[3]) if best else None


def trim_shared_option_clause(unit: str, label: str, sibling_labels=()) -> str:
    """If a sentence names this option and a sibling joined by 'and', keep this option's conjunct."""
    this = set(_stems_of(label))
    siblings = set()
    for sib in sibling_labels or ():
        siblings.update(_stems_of(sib))
    siblings -= this
    unit = _plain_quotes(unit)
    if not this or not siblings or not (siblings & {stem(w) for w in _words(unit)}):
        return unit
    this_span = _best_span(unit, this)
    sib_span = _best_span(unit, siblings)
    if not this_span or not sib_span:
        return unit
    # mid may include a leftover proper name that belongs with this option ("and Petethegoat's")
    mid_and = re.compile(r"\s+and((?:\s+[A-Za-z][\w']*)*)\s*$", re.I)
    def extend(end: int) -> int:
        more = re.match(r"\s+(mod|mods|pack|option|folder|plugin)\b", unit[end:], re.I)
        return end + more.end() if more else end

    a_start, a_end = this_span
    b_start, b_end = sib_span
    a_end, b_end = extend(a_end), extend(b_end)
    if a_end <= b_start:
        mid = unit[a_end:b_start]
        if mid_and.match(mid):
            return (unit[:a_end] + unit[b_end:]).strip()
    if b_end <= a_start:
        mid = unit[b_end:a_start]
        hit = mid_and.match(mid)
        if hit:
            kept = (hit.group(1) or "") + (" " if hit.group(1) else "") + unit[a_start:]
            return (unit[:b_start] + kept.lstrip()).strip()
    return unit


def clean_option_folder_wording(text: str, label: str) -> str:
    """Strip a leading BAIN folder number before this option's name, and a generic 'drop files ... from' lead-in."""
    name = clean_label(label)
    if not text:
        return text
    for candidate in dict.fromkeys(n for n in (name, base_label(label)) if n):
        numbered = re.compile(r"\b\d{1,3}\s+(?=" + re.escape(candidate) + r"\b)", re.I)
        text = numbered.sub("", text)
    text = _DROP_AND_REPLACE.sub("use ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    # Capitalize a leading "use " left after dropping "drop files ... from"
    text = re.sub(r"(?:(?<=\.\s)|(?<=^))use ", "Use ", text)
    return text


def best_option_sentences(page_text: str, label: str, extra_terms=(), limit: int = 3,
                          sibling_labels=()) -> list[str]:
    """Rank page lines/sentences that describe the optional folder ``label``.

    Uses stemmed matching (replacer ~ replace ~ replacement), rewards definition-style
    lines that start with the folder's own name ("Replacer - a replacer for ..."),
    and demotes changelog, permission and navigation lines. Credit/thanks lines and
    generic installer-format advice are dropped unless that advice is the only text
    that names the option. A sentence that also names a sibling option is trimmed to
    this option's conjunct when the two names are joined by 'and'. A label passed in sibling_labels is ignored when it is this option. Sentences that only share a generic stem (sound, music, replacer) are not treated as unique to the option.
    """
    name = clean_label(label)
    label_stems = _stems_of(label)
    lead_patterns = [pat for pat in dict.fromkeys(
        filter(None, (_lead_pattern(name), _lead_pattern(base_label(label))))) if pat]
    qualifier = _QUALIFIER.search(name)
    qualifier_words = [w for w in _words(qualifier.group(1))] if qualifier else []
    qualifier_stems = {stem(w) for w in qualifier_words if len(w) >= 3}
    qualifier_re = (re.compile(r"\b" + r"\W*".join(map(re.escape, qualifier_words)) + r"\b", re.I)
                    if qualifier_words else None)
    extra_stems = [stem(w) for t in extra_terms for w in _words(str(t)) if w.casefold() not in _STOP and len(w) >= 3]
    self_key = name.casefold()
    sibling_labels = tuple(
        s for s in (sibling_labels or ())
        if clean_label(s).casefold() != self_key
    )
    sibling_stems = set()
    for sib in sibling_labels:
        sibling_stems.update(_stems_of(sib))
    sibling_stems -= set(label_stems)
    if not label_stems and not extra_stems:
        return []
    text = _plain_quotes(page_text)
    full_marker = text.find("--- full page ---")
    ranked = []
    seen = set()
    in_changelog = False
    in_credits = False
    for index, unit in enumerate(_split_units(text)):
        if unit == "--- full page ---":
            in_credits = False
            continue
        folded = unit.casefold()
        if _CREDIT_HEADING.match(unit.strip()):
            in_credits = True
            continue
        if re.match(r"^(changelogs?|version history|mods using this mod|posts|comments)\b", folded):
            in_changelog = folded.startswith(("changelog", "version history"))
            in_credits = False
            continue
        if in_credits and (is_credit_line(unit) or len(unit) < 80):
            continue
        if in_credits:
            in_credits = False
        if is_credit_line(unit):
            continue
        if folded in seen or len(unit) < 18:
            continue
        unit_stems = {stem(w) for w in _words(unit)}
        hits = [s for s in label_stems if s in unit_stems]
        extra = [s for s in extra_stems if s in unit_stems]
        if not hits and not extra:
            continue
        score = 10.0 * len(set(hits)) / max(1, len(set(label_stems))) + 2.0 * len(set(extra))
        if label_stems and len(set(hits)) == len(set(label_stems)):
            score += 4
        lead = any(pat.match(unit) for pat in lead_patterns)
        if lead and qualifier_stems and re.search(
                r"\b(?:incompatible|not\s+compatible)\s+with\b[^.]*\b(?:" + "|".join(map(re.escape, qualifier_words)) + r")",
                unit, re.I):
            lead = False  # "Clouds textures - ... Incompatible with skies.iv" does not describe the skies.iv variant
            score -= 12
        if qualifier_re and qualifier_re.search(unit) and not lead and score > 0:
            score += 6  # names this variant ('skies.iv pack')
        if lead:
            score += 12  # "Replacer - a replacer for ..." definition line
        if re.match(r"^version\s+\d", folded) or in_changelog:
            score -= 9
        if re.search(r"\b(permission|endorse|download|log in|tracked|report abuse)\b", folded):
            score -= 4
        distinctive = set(label_stems) - sibling_stems
        strong = distinctive - _GENERIC_STEMS
        sib_penalty = 0
        if sibling_stems and (sibling_stems & unit_stems) and (set(label_stems) & unit_stems) and not lead:
            sib_penalty += 3  # a unique sentence beats one that names several options
            if distinctive and not (set(hits) & distinctive):
                sib_penalty += 10  # names a sibling, not this option's own words
        for sib in sibling_labels:
            sib_name = clean_label(sib)
            if sib_name and base_label(sib).casefold() != base_label(label).casefold() and re.match(
                    r"^\W*(?:\d{1,3}\s*[-.:)]?\s+)?" + re.escape(sib_name) + r"\s*(?:[-\u2013\u2014:]|\(|is\b|are\b)", unit, re.I):
                sib_penalty += 12  # this is the sibling's own definition line
        unique_flag = bool(lead) or bool(set(hits) & strong)
        score -= sib_penalty
        if is_installer_meta(unit):
            score -= 6
        if 40 <= len(unit) <= 320:
            score += 1.5
        position = text.find(unit)
        if full_marker >= 0 and position > full_marker:
            score -= 0.5  # same line from the generic body dump is slightly less trusted
        seen.add(folded)
        ranked.append((score, index, unit, unique_flag, score + sib_penalty))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if not ranked:
        return []
    unique_ranked = [item for item in ranked if item[3]]
    if unique_ranked:
        pool = unique_ranked
        floor = max(2.0, pool[0][0] * 0.75)
        chosen = [unit for score, _i, unit, _u, _base in pool if score >= floor][:limit]
    else:
        # No sentence names this option distinctly: keep the best shared or general lines,
        # without letting a sibling-name penalty bury a pack summary.
        pool = sorted(ranked, key=lambda item: (-item[4], item[1]))
        floor = max(2.0, pool[0][4] * 0.75)
        chosen = [unit for _s, _i, unit, _u, base in pool if base >= floor][:limit]
    descriptive = [unit for unit in chosen if not is_installer_meta(unit)]
    if descriptive:
        chosen = descriptive
    cleaned = [strip_thanks_sentences(clean_option_folder_wording(trim_shared_option_clause(unit, label, sibling_labels), label))
               for unit in chosen]
    unique = []
    for unit in cleaned:
        key = unit.casefold()
        if any(key == other.casefold() or key.startswith(other.casefold()) or other.casefold().startswith(key)
               for other in unique):
            # keep the longer wording if a shorter duplicate already landed
            unique = [unit if (u.casefold() == key or key.startswith(u.casefold()) or u.casefold().startswith(key))
                      and len(unit) > len(u) else u for u in unique]
            if not any(unit.casefold() == u.casefold() or unit.casefold().startswith(u.casefold())
                       or u.casefold().startswith(unit.casefold()) for u in unique):
                unique.append(unit)
            continue
        unique.append(unit)
    return unique


