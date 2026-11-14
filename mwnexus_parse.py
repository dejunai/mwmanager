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


def best_option_sentences(page_text: str, label: str, extra_terms=(), limit: int = 3) -> list[str]:
    """Rank page lines/sentences that describe the optional folder ``label``.

    Uses stemmed matching (replacer ~ replace ~ replacement), rewards definition-style
    lines that start with the folder's own name ("Replacer - a replacer for ..."),
    and demotes changelog, permission and navigation lines.
    """
    name = clean_label(label)
    label_stems = [stem(w) for w in _words(name) if w.casefold() not in _STOP and len(w) >= 3]
    extra_stems = [stem(w) for t in extra_terms for w in _words(str(t)) if w.casefold() not in _STOP and len(w) >= 3]
    if not label_stems and not extra_stems:
        return []
    text = str(page_text or "")
    full_marker = text.find("--- full page ---")
    ranked = []
    seen = set()
    in_changelog = False
    for index, unit in enumerate(_split_units(text)):
        if unit == "--- full page ---":
            continue
        folded = unit.casefold()
        if re.match(r"^(changelogs?|version history|permissions and credits|mods using this mod|posts|comments)\b", folded):
            in_changelog = folded.startswith(("changelog", "version history"))
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
        lead = re.match(r"^\W*" + r"\W+".join(re.escape(w) for w in _words(name)) + r"\s*(?:[-–—:]|\(|is\b|are\b|adds?\b|replaces?\b)",
                        unit, re.I) if _words(name) else None
        if lead:
            score += 12  # "Replacer - a replacer for ..." definition line
        if re.match(r"^version\s+\d", folded) or in_changelog:
            score -= 9
        if re.search(r"\b(permission|endorse|download|log in|tracked|report abuse)\b", folded):
            score -= 4
        if 40 <= len(unit) <= 320:
            score += 1.5
        position = text.find(unit)
        if full_marker >= 0 and position > full_marker:
            score -= 0.5  # same line from the generic body dump is slightly less trusted
        seen.add(folded)
        ranked.append((score, index, unit))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if not ranked:
        return []
    floor = max(2.0, ranked[0][0] * 0.75)  # keep only near-best matches
    return [unit for score, _i, unit in ranked if score >= floor][:limit]
