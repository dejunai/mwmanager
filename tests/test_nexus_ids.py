"""nexus_ids: an archive with no Nexus id in its name or .meta file is looked up in Vortex's records (read-only),
then by a Nexus search (a fake here, from tests/fixtures/nexus_graphql_search.json; no network), else marked as not
a Nexus mod, remembered, and changeable by hand. Also the small read-only LevelDB/Snappy reader for Vortex's state."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nexus_ids as ni  # noqa: E402
import vortex_state as vs  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nexus_graphql_search.json"


# ---- a tiny LevelDB writer for fixtures (uncompressed tables, one log) ---------------------------------------------

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _block(entries) -> bytes:
    body = b"".join(_varint(0) + _varint(len(k)) + _varint(len(v)) + k + v for k, v in entries)
    return body + (0).to_bytes(4, "little") + (1).to_bytes(4, "little")


def write_table(path: Path, rows, start_seq: int = 1) -> None:
    """rows: [(key, value or None)] in key order; None writes a deletion."""
    entries = []
    for i, (key, value) in enumerate(rows):
        kind = 0 if value is None else 1
        ikey = key.encode() + (((start_seq + i) << 8) | kind).to_bytes(8, "little")
        entries.append((ikey, b"" if value is None else json.dumps(value).encode()))
    data = _block(entries)
    blob = data + b"\x00" + b"\x00" * 4
    handle = _varint(0) + _varint(len(data))
    index = _block([(entries[-1][0], handle)])
    index_off = len(blob)
    blob += index + b"\x00" + b"\x00" * 4
    footer = _varint(0) + _varint(0) + _varint(index_off) + _varint(len(index))
    blob += footer + b"\x00" * (40 - len(footer)) + vs.TABLE_MAGIC
    path.write_bytes(blob)


def write_log(path: Path, rows, seq: int) -> None:
    batch = seq.to_bytes(8, "little") + len(rows).to_bytes(4, "little")
    for key, value in rows:
        if value is None:
            batch += b"\x00" + _varint(len(key)) + key.encode()
        else:
            raw = json.dumps(value).encode()
            batch += b"\x01" + _varint(len(key)) + key.encode() + _varint(len(raw)) + raw
    path.write_bytes(b"\x00\x00\x00\x00" + len(batch).to_bytes(2, "little") + b"\x01" + batch)


def _download_rows(dl_id: str, name: str, mod_id: int | None, size: int, md5: str = ""):
    base = f"persistent###downloads###files###{dl_id}###"
    rows = [(base + "fileMD5", md5), (base + "game", ["morrowind"]), (base + "localPath", name),
            (base + "size", size)]
    if mod_id:
        rows += [(base + "modInfo###nexus###ids###fileId", mod_id * 10), (base + "modInfo###nexus###ids###modId", mod_id)]
    return rows


@pytest.fixture
def state(tmp_path):
    folder = tmp_path / "state.v2"
    folder.mkdir()
    payload = b"renamed archive bytes"
    rows = (_download_rows("a1", "Old Name.7z", 41001, 100)
            + _download_rows("b2", "Copied Away.7z", 42002, len(payload), hashlib.md5(payload).hexdigest())
            + _download_rows("c3", "Gone.7z", 43003, 5)
            + [("persistent###mods###morrowind###Staged Mod###attributes###fileName", "Staged Mod.7z"),
               ("persistent###mods###morrowind###Staged Mod###attributes###modId", 44004),
               ("persistent###mods###morrowind###Staged Mod###installationPath", "Staged Mod"),
               ("settings###mods###installPath###morrowind", str(tmp_path / "staging" / "{game}"))])
    write_table(folder / "000005.ldb", sorted(rows))
    # the log is newer: it renames the first download's mod and deletes the third download
    write_log(folder / "000006.log", [("persistent###downloads###files###a1###modInfo###nexus###ids###modId", 41009),
                                      ("persistent###downloads###files###c3###localPath", None)], seq=1000)
    return folder, payload


# ---- names, Snappy and the Vortex state reader --------------------------------------------------------------------

def test_ids_from_name_strict_and_loose():
    assert ni.ids_from_name("AURA 6.0.0-48255-6-0-0-1779801776.7z") == ("48255", "")
    assert ni.ids_from_name("Mod-49042-2-7-5-1752788019.7z") == ("49042", "")      # upload time, not a file id
    assert ni.ids_from_name("Almalexia'S Chamber Overhaul 52737 1.95 2026-10-11T01-30Z OmEgmfDk.zip") == ("52737", "")
    assert ni.ids_from_name("Corprus Natives-45674-1.zip") == ("", "")
    assert ni.ids_from_name("Corprus Natives-45674-1.zip", loose=True) == ("45674", "")
    assert ni.ids_from_name("Gestalt Revoiced -43642-.7z", loose=True) == ("43642", "")
    assert ni.ids_from_name("Better Bodies 2.2.rar", loose=True) == ("", "")


def test_snappy_literal_and_overlapping_copy():
    # length 12; literal "abcd"; copy (tag kind 1) of 8 bytes from offset 4, which overlaps its own output
    stream = _varint(12) + bytes([(4 - 1) << 2]) + b"abcd" + bytes([0b001 | ((8 - 4) << 2), 4])
    assert vs.snappy_decompress(stream) == b"abcdabcdabcd"
    with pytest.raises(ValueError):
        vs.snappy_decompress(_varint(5) + bytes([(2 - 1) << 2]) + b"ab")


def test_state_reader_newest_wins_and_deletions_hide(state):
    folder, _payload = state
    rows = {Path(r["file"]).name: r for r in vs.downloads(state_dir=folder)}
    assert rows["Old Name.7z"]["mod_id"] == "41009"                      # the log's newer value
    assert "Gone.7z" not in rows                                         # deleted localPath
    (mod,) = vs.staged_mods(state_dir=folder)
    assert (mod["archive"], mod["mod_id"], mod["folder"]) == ("Staged Mod.7z", "44004", "Staged Mod")
    assert vs.read_prefix("x", tmp := folder / "missing") == {} and not tmp.exists()


def test_state_reader_never_writes(state):
    folder, _payload = state
    before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in folder.iterdir()}
    vs.downloads(state_dir=folder)
    ni.VortexIndex.load(state_dir=folder)
    assert {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in folder.iterdir()} == before


# ---- resolution order ---------------------------------------------------------------------------------------------

def _downloads(tmp_path) -> Path:
    folder = tmp_path / "Vortex" / "morrowind"
    folder.mkdir(parents=True)
    (tmp_path / "Vortex" / ni.DOWNLOADS_MARKER).write_text("{}", encoding="utf-8")
    return folder


class FakeSearch:
    def __init__(self, answer=None, error=False):
        self.answer, self.error, self.calls = answer, error, []

    def __call__(self, archive, plugins=()):
        self.calls.append(Path(archive).name)
        if self.error:
            raise ni.SearchUnavailable("offline")
        return self.answer


def test_vortex_records_answer_by_name_then_by_size_and_md5(tmp_path, state):
    folder, payload = state
    downloads = _downloads(tmp_path)
    index = ni.VortexIndex.load(state_dir=folder)
    (downloads / "Old Name.7z").write_bytes(b"x" * 100)
    (downloads / "Renamed by hand.7z").write_bytes(payload)
    manager = tmp_path / "manager"
    manager.mkdir()
    by_name = ni.resolve(downloads / "Old Name.7z", manager, vortex=index)
    assert (by_name["mod_id"], by_name["source"]) == ("41009", "vortex_download")
    by_hash = ni.resolve(downloads / "Renamed by hand.7z", manager, vortex=index)
    assert (by_hash["mod_id"], by_hash["source"]) == ("42002", "vortex_download")
    assert "MD5" in by_hash["detail"]
    cached = ni.read_cache(manager)
    assert cached["old name.7z"]["source"] == "vortex_download" and cached["renamed by hand.7z"]["mod_id"] == "42002"


def test_vortex_staging_meta_ini(tmp_path, state):
    folder, _payload = state
    downloads = _downloads(tmp_path)
    staged = tmp_path / "staging" / "morrowind" / "Plain Mod"
    staged.mkdir(parents=True)
    (staged / "meta.ini").write_text("[General]\nmodid=45555\n", encoding="utf-8")
    (downloads / "Plain Mod.7z").write_bytes(b"zz")
    result = ni.resolve(downloads / "Plain Mod.7z", None, vortex=ni.VortexIndex.load(state_dir=folder))
    assert (result["mod_id"], result["source"]) == ("45555", "vortex_staging")


def test_name_and_meta_come_before_vortex(tmp_path):
    downloads = _downloads(tmp_path)
    index = ni.VortexIndex(downloads=[{"file": "Mod-12345-1-0-1700000000.7z", "mod_id": "99999", "size": 1}])
    archive = downloads / "Mod-12345-1-0-1700000000.7z"
    archive.write_bytes(b"a")
    assert ni.resolve(archive, None, vortex=index)["source"] == "filename"
    plain = downloads / "Plain.7z"
    plain.write_bytes(b"b")
    (downloads / "Plain.7z.meta").write_text(json.dumps({"modInfo": {"modId": 777, "fileId": 7}}), encoding="utf-8")
    assert ni.resolve(plain, None, vortex=index) == {"mod_id": "777", "file_id": "7", "source": "archive_meta",
                                                     "detail": "the archive's .meta file"}


def test_search_then_not_nexus_is_remembered_and_overridable(tmp_path):
    downloads = _downloads(tmp_path)
    manager = tmp_path / "manager"
    manager.mkdir()
    archive = downloads / "Homebrew Tweaks.7z"
    archive.write_bytes(b"c")
    nothing = FakeSearch(None)
    first = ni.resolve(archive, manager, vortex=False, search=nothing)
    assert (first["mod_id"], first["source"]) == ("", ni.NOT_NEXUS)
    again = ni.resolve(archive, manager, vortex=False, search=nothing)
    assert again["source"] == ni.NOT_NEXUS and nothing.calls == ["Homebrew Tweaks.7z"]     # not searched again
    ni.set_override(manager, archive.name, "56789")
    assert ni.resolve(archive, manager, vortex=False, search=nothing)["mod_id"] == "56789"
    ni.set_override(manager, archive.name, None)
    assert ni.rescan(manager, [archive.name]) == 1
    found = ni.resolve(archive, manager, vortex=False, search=FakeSearch({"mod_id": "60001", "file_id": "6",
                                                                          "detail": "fake"}))
    assert (found["mod_id"], found["source"]) == ("60001", "nexus_search")
    ni.set_override(manager, archive.name, "none")
    assert ni.resolve(archive, manager, vortex=False)["source"] == ni.NOT_NEXUS
    with pytest.raises(ValueError):
        ni.set_override(manager, archive.name, "abc")


def test_search_that_cannot_run_leaves_it_unresolved_and_uncached(tmp_path):
    downloads = _downloads(tmp_path)
    manager = tmp_path / "manager"
    manager.mkdir()
    archive = downloads / "Homebrew Tweaks.7z"
    archive.write_bytes(b"c")
    assert ni.resolve(archive, manager, vortex=False, search=FakeSearch(error=True))["source"] == ni.UNRESOLVED
    assert ni.resolve(archive, manager, vortex=False, search=None)["source"] == ni.UNRESOLVED
    assert ni.read_cache(manager) == {}


def test_a_changed_file_is_looked_up_again(tmp_path):
    downloads = _downloads(tmp_path)
    manager = tmp_path / "manager"
    manager.mkdir()
    archive = downloads / "Homebrew Tweaks.7z"
    archive.write_bytes(b"c")
    ni.resolve(archive, manager, vortex=False, search=FakeSearch(None))
    archive.write_bytes(b"a new download of the same name")
    search = FakeSearch({"mod_id": "60002", "file_id": "", "detail": "fake"})
    assert ni.resolve(archive, manager, vortex=False, search=search)["mod_id"] == "60002"


def test_vortex_is_only_read_for_archives_in_a_vortex_downloads_folder(tmp_path, monkeypatch):
    loads = []
    monkeypatch.setattr(ni, "_VORTEX", {})
    monkeypatch.setattr(ni.VortexIndex, "load", classmethod(lambda cls, *a, **k: loads.append(1) or cls()))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "Loose.7z").write_bytes(b"d")
    assert ni.resolve(elsewhere / "Loose.7z", None)["source"] == ni.UNRESOLVED and loads == []
    downloads = _downloads(tmp_path)
    (downloads / "Loose.7z").write_bytes(b"d")
    ni.resolve(downloads / "Loose.7z", None)
    assert loads == [1]


# ---- the Nexus search, faked from the fixture ---------------------------------------------------------------------

def fake_query():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    calls = []

    def query(document, variables):
        calls.append(variables)
        if "game(" in document:
            return {"game": data["game"]}
        if "mods(" in document:
            text = variables["f"]["name"][0]["value"].casefold()
            return {"mods": {"nodes": next((v for k, v in data["mods"].items() if k.casefold() == text), [])}}
        return {"modFiles": data["files"].get(variables["m"], [])}
    return query, calls


def test_search_accepts_the_same_file_name(tmp_path):
    archive = tmp_path / "Glowing_Bitter_Coast_v1.2.7z"
    archive.write_bytes(b"e" * 10)
    query, _calls = fake_query()
    hit = ni.NexusSearch(query=query)(archive)
    assert (hit["mod_id"], hit["file_id"]) == ("51001", "1000070001") and "Glowing_Bitter_Coast_v1.2.7z" in hit["detail"]


def test_search_accepts_the_same_size_and_a_similar_name(tmp_path):
    archive = tmp_path / "quiet_guars.zip"
    archive.write_bytes(b"f" * 2048)
    query, _calls = fake_query()
    assert ni.NexusSearch(query=query)(archive)["mod_id"] == "52001"
    other = tmp_path / "quiet_guars_other.zip"
    other.write_bytes(b"f" * 2047)                                 # a different size and no version: no match
    assert ni.NexusSearch(query=query)(other) is None


def test_search_rejects_two_equally_good_mods_and_a_name_only_match(tmp_path):
    query, _calls = fake_query()
    twins = tmp_path / "Twin Lamps 1.0.7z"
    twins.write_bytes(b"g")
    assert ni.NexusSearch(query=query)(twins) is None                     # both mods have a 1.0 "Twin Lamps"
    shack = tmp_path / "Lonely Shack.7z"
    shack.write_bytes(b"h")
    assert ni.NexusSearch(query=query)(shack) is None                     # similar name, nothing else agrees


def test_search_terms_drop_versions_dates_and_nexus_suffixes():
    assert ni.search_terms(Path("KS_Julan_Ashlander_Companion_3.0_beta_200814 (1).7z")) == ["KS Julan Ashlander Companion"]
    assert ni.search_terms(Path("AnimatedSoulgemsV0_4-25278.rar")) == ["Animated Soulgems"]
    assert ni.search_terms(Path("x.7z"), ["Quiet Guars.esp"]) == ["Quiet Guars"]


def test_default_search_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("MWMANAGER_NEXUS_SEARCH", "0")
    assert ni.default_search() is None
    monkeypatch.setenv("MWMANAGER_NEXUS_SEARCH", "1")
    assert isinstance(ni.default_search(), ni.NexusSearch)


# ---- where MWManager uses it --------------------------------------------------------------------------------------

def test_scanned_item_gets_the_resolved_id_and_its_source(tmp_path, monkeypatch):
    import plugin_install
    monkeypatch.setattr(ni, "_VORTEX", {"default": ni.VortexIndex()})     # no real Vortex state in tests
    downloads = _downloads(tmp_path)
    manager = tmp_path / "manager"
    manager.mkdir()
    archive = downloads / "Homebrew Tweaks.7z"
    archive.write_bytes(b"c")
    item = {"archive": str(archive), "plugins": ["Homebrew.esp"], "vortex_metadata": {}}
    plugin_install._apply_nexus_id_fallback(item, archive, manager,
                                            search=FakeSearch({"mod_id": "61000", "file_id": "", "detail": "x"}))
    assert (item["nexus_mod_id"], item["nexus_id_source"]) == ("61000", "nexus_search")
    ni.set_override(manager, archive.name, "none")                        # a cached scan item follows the override
    plugin_install._apply_nexus_id_fallback(item, archive, manager)
    assert item["nexus_mod_id"] is None and item["nexus_id_source"] == ni.NOT_NEXUS
    named = {"archive": "Mod-12345-1-0-1700000000.7z"}
    plugin_install._apply_nexus_id_fallback(named, Path(named["archive"]), manager)
    assert named["nexus_mod_id"] == "12345" and "nexus_id_source" not in named


def test_option_descriptions_skip_pages_for_mods_not_on_nexus(tmp_path, monkeypatch):
    import option_descriptions as od
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    records = [{"archive": "Homebrew Tweaks.7z", "doc_dir": "", "options": [{"folder": "01 Core"}]},
               {"archive": "Old Style-45674-1.zip", "doc_dir": "", "options": [{"folder": "01 Core"}]}]
    (corpus / "index.json").write_text(json.dumps(records), encoding="utf-8")
    pages = []
    monkeypatch.setattr(od, "page_text", lambda mod_id, *a, **k: pages.append(mod_id) or ("", None, ""))
    answers = {"Homebrew Tweaks.7z": ("", ni.NOT_NEXUS), "Old Style-45674-1.zip": ("45674", "vortex_download")}
    stats = od.update(fetch="cache", out=tmp_path / "out.json", corpus=corpus, log=lambda *_: None, delay=0,
                      resolver=lambda name: answers[name])
    assert pages == ["45674"] and stats["not_nexus"] == 1
    assert stats["page_problems"]["Homebrew Tweaks.7z"] == "not a Nexus mod"
    data = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))["options"]
    assert data["Old Style-45674-1.zip | 01 Core"]["nexus_id"] == "45674"
