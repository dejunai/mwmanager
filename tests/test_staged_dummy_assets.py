"""Stand-ins (the game's own small coin model and animation) are copied out of Morrowind.bsa the first time one is
needed, into MWManager's generated outputs\\dummy folder: never shipped, never listed in a manifest, never put in a
game install. CR-25 round 2 found the staged missing-asset pass could not find Morrowind.bsa by itself (a staged
Tidy runs outside the game folder), so Tidy gives it the game folder of Data Files."""
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mwmanager_upstream  # noqa: E402


def bsa(files):
    """A TES3 Morrowind.bsa holding `files` [(name, bytes)]."""
    c = len(files)
    names = b"".join(n.encode() + b"\0" for n, _d in files)
    noffs, o = [], 0
    for n, _d in files:
        noffs.append(o)
        o += len(n) + 1
    so, doff = b"", 0
    for _n, d in files:
        so += struct.pack("<II", len(d), doff)
        doff += len(d)
    dir_ = so + struct.pack(f"<{c}I", *noffs) + names
    return struct.pack("<III", 0x100, len(dir_), c) + dir_ + b"\0" * 8 * c + b"".join(d for _n, d in files)


class StandInsAreGeneratedInOutputs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.manager = root / "manager"
        self.manager.mkdir()
        self.game = root / "game"
        self.data = self.game / "Data Files"
        self.data.mkdir(parents=True)
        self.tidy = mwmanager_upstream.compose_tidy()
        scope = mwmanager_upstream.use_manager_dir(str(self.manager))
        self.scanner = scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        import mwdialog_media
        self.media = mwdialog_media

    def test_the_stand_in_folder_is_outputs_dummy_for_the_scanner_and_upstream(self):
        want = os.path.join(str(self.manager), "outputs", "dummy")
        self.assertEqual(self.media.DUMMY_DIR, want)
        self.assertEqual(self.scanner.DUMMY_DIR, want)
        self.assertFalse(os.path.exists(want))            # nothing made until a stand-in is needed

    def test_a_stand_in_is_copied_from_the_games_bsa_only_when_first_needed(self):
        media = self.media
        source = media.DUMMY_SOURCES[media.DUMMY_NIF]
        (self.data / "Morrowind.bsa").write_bytes(bsa([(source, b"COIN")]))
        dummy = self.manager / "outputs" / "dummy"
        upstream = media.dummy_path
        with self.tidy._stand_ins_from_game(str(self.data)):
            self.assertFalse(dummy.exists())               # entering the pass makes nothing
            self.assertEqual(media.stand_in_bytes("meshes\\gone\\thing.nif"), b"COIN")
        self.assertIs(media.dummy_path, upstream)          # put back after the pass
        self.assertEqual([p.name for p in dummy.iterdir()], [media.DUMMY_NIF])   # only the one needed
        self.assertEqual(sorted(p.name for p in self.game.iterdir()), ["Data Files"])
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ["Morrowind.bsa"])

    def test_no_data_files_changes_nothing(self):
        upstream = self.media.dummy_path
        with self.tidy._stand_ins_from_game(None):
            self.assertIs(self.media.dummy_path, upstream)

    def test_assets_apply_resolves_missing_assets_inside_the_stand_in_scope(self):
        src = (ROOT / "mwmanager_tidy_ext.py").read_text(encoding="utf-8")
        body = src[src.index("def assets_apply("):]
        self.assertLess(body.index("_stand_ins_from_game("), body.index("resolve_missing_assets("))
        self.assertNotIn("_ensure_dummy_assets", src)     # no eager copy of every stand-in


class StandInNamesAgreeWhateverWasImportedFirst(unittest.TestCase):
    def test_vortex_tidy_imported_before_compose_keeps_the_stand_in_table_whole(self):
        """mock_fomod's grass check imports vortex_tidy before anything is composed. vortex_tidy has its own
        DUMMY_SOURCES (a cache file name); that clash used to stop MWManager's stand-in table reaching
        mwdialog_media while DUMMY_NIF did, so no stand-in could be copied out of Morrowind.bsa."""
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import vortex_tidy; import mwmanager_upstream as U; "
                "U.compose_tidy(); import mwdialog_media as m; "
                "print(m.DUMMY_NIF in m.DUMMY_SOURCES, m.DUMMY_KF in m.DUMMY_SOURCES, "
                "isinstance(vortex_tidy.DUMMY_SOURCES, str))")
        run = subprocess.run([sys.executable, "-B", "-c", code, str(ROOT)], capture_output=True, text=True,
                             cwd=str(ROOT), timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr[-2000:])
        self.assertEqual(run.stdout.split()[-3:], ["True", "True", "True"])


class NoDummyAssetsShipped(unittest.TestCase):
    def test_no_manifest_and_no_tracked_file_carries_a_dummy_folder(self):
        for manifest in (ROOT / "utility_source_manifest.json", ROOT / "tools" / "steam-upstream" / "COPY_MANIFEST.json"):
            text = json.dumps(json.loads(manifest.read_text(encoding="utf-8"))).casefold()
            self.assertNotIn("dummy assets", text, manifest.name)
            self.assertNotIn("outputs\\\\dummy", text, manifest.name)
        try:
            tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True,
                                     check=True).stdout.splitlines()
        except (OSError, subprocess.CalledProcessError):
            tracked = [str(p.relative_to(ROOT)).replace("\\", "/") for p in ROOT.rglob("*") if p.is_file()
                       and ".git" not in p.parts]
        bad = [t for t in tracked if "dummy assets/" in t.casefold() or t.casefold().startswith("outputs/dummy/")]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
