import json

import option_descriptions as od


def test_refine_strips_markup_links_chrome_and_changelog():
    raw = ("[b]Replacer[/b] - a replacer for the [url=https://x.example/a]worst offenders[/url].\n"
           "See [the guide](https://example.com/guide) and https://example.com/raw\n"
           "Endorsements 1,234\n"
           "v1.2.3 fixed a typo\n"
           "**Bold** text stays as words.\n"
           "**Bold** text stays as words.\n")
    out = od.refine(raw)
    assert out.splitlines() == [
        "Replacer - a replacer for the worst offenders.",
        "See the guide and",
        "Bold text stays as words.",
    ]


def test_refine_cuts_at_a_sentence_end():
    raw = " ".join(f"Sentence number {i} is here." for i in range(60))
    out = od.refine(raw, limit=200)
    assert len(out) <= 200 and out.endswith(".")


def test_nexus_id_reads_both_download_name_formats():
    assert od.nexus_id("AURA 6.0.0-48255-6-0-0-1779801776.7z") == "48255"
    assert od.nexus_id("Almalexia'S Chamber Overhaul 52737 1.95 2026-10-11T01-30Z OmEgmfDk.zip") == "52737"
    assert od.nexus_id("loose readme.zip") == ""


def _corpus(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir(exist_ok=True)
    records = [{"archive": "Thing-12345-1-0-1700000000.7z", "doc_dir": "",
                "options": [{"folder": "01 Replacer"}, {"folder": "02 Extra"}]},
               {"archive": "NoOptions-222-1-0-1700000000.7z", "options": []}]
    (corpus / "index.json").write_text(json.dumps(records), encoding="utf-8")
    return corpus


def test_update_uses_page_text_refines_and_keeps_unchanged(tmp_path, monkeypatch):
    import mwnexus_parse
    calls = []

    def fake_page(mod_id, fetch, refresh_days, log=print):
        calls.append((mod_id, fetch))
        return "page", 1.0, ""

    def fake_best(page, name, extra_terms=()):
        return ["[b]Replacer[/b] swaps the [url=https://x]meshes[/url]."] if "Replacer" in name else []

    monkeypatch.setattr(od, "page_text", fake_page)
    monkeypatch.setattr(mwnexus_parse, "best_option_sentences", fake_best)
    out = tmp_path / "option_descriptions.json"
    stats = od.update(fetch="cache", out=out, corpus=_corpus(tmp_path), log=lambda *_: None, delay=0)
    assert calls == [("12345", "cache")]
    assert (stats["packages"], stats["options"], stats["nexus"], stats["empty"]) == (1, 2, 1, 1)
    assert stats["refined"] == 1 and stats["added"] == 2
    data = json.loads(out.read_text(encoding="utf-8"))["options"]
    entry = data["Thing-12345-1-0-1700000000.7z | 01 Replacer"]
    assert entry["text"] == "Replacer swaps the meshes." and entry["source"] == "nexus"
    assert stats["samples"][0]["after"] == "Replacer swaps the meshes."

    again = od.update(fetch="cache", out=out, corpus=_corpus(tmp_path), log=lambda *_: None, delay=0)
    assert (again["kept"], again["added"], again["changed"]) == (2, 0, 0)


def test_page_text_cache_mode_never_fetches(monkeypatch):
    import mwnexus_browser
    monkeypatch.setattr(mwnexus_browser, "cached_page", lambda mod_id: None)
    assert od.page_text("12345", "cache", 14) == ("", None, "")
    assert od.page_text("", "http", 14)[2] == "no Nexus id in the archive name"


def test_option_window_page_text_is_refined(tmp_path):
    import mock_fomod
    record = {"label": "01 Replacer", "path": "01 Replacer", "targets": []}
    page = ("Replacer - a replacer for some of the worst offenders, see "
            "[url=https://example.com]the list[/url] for [b]details[/b].\n")
    text = mock_fomod._option_description(record, tmp_path, page)
    assert text and "[url" not in text and "[b]" not in text and "https://" not in text


def test_refine_drops_changelog_entries_with_their_continuation_lines():
    raw = ("0.9: * First Release\n"
           "   1.0: * Moved Solstheim 1 cell to the north because I decided to\n"
           "          prioritize the map of the big province mods\n"
           "   - and fixed a typo\n"
           "Core files: the island and its NPCs.\n")
    assert od.refine(raw) == "Core files: the island and its NPCs."
