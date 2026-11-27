"""Every upstream name a MWManager layer replaces still exists upstream (a sync that renames or removes one would
leave the override silently unused). The baseline is tests/layer_overrides.json; after a deliberate change, rerun
with MWMANAGER_UPDATE_LAYER_BASELINE=1 to rewrite it."""
import json
import os
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mwmanager_upstream  # noqa: E402

BASELINE = ROOT / "tests" / "layer_overrides.json"


class LayerReportTests(unittest.TestCase):
    def test_every_override_still_replaces_an_upstream_name(self):
        mwmanager_upstream.compose()
        mwmanager_upstream.compose_tidy()
        now = {k: v["replaced"] for k, v in sorted(mwmanager_upstream.LAYER_REPORT.items())}
        if os.environ.get("MWMANAGER_UPDATE_LAYER_BASELINE") == "1":
            BASELINE.write_text(json.dumps(now, indent=1), encoding="utf-8")
        want = json.loads(BASELINE.read_text(encoding="utf-8"))
        problems = []
        for layer, names in want.items():
            report = mwmanager_upstream.LAYER_REPORT.get(layer, {"replaced": [], "added": []})
            for name in sorted(set(names) - set(report["replaced"])):
                where = "now added as new" if name in report["added"] else "gone"
                problems.append(f"{layer}: {name} no longer replaces an upstream name ({where}): "
                                f"renamed or removed upstream?")
            for name in sorted(set(report["replaced"]) - set(names)):
                problems.append(f"{layer}: {name} now replaces an upstream name (new override or upstream "
                                f"name clash)")
        self.assertEqual(problems, [], "\n".join(problems))




class StagedDataRebase(unittest.TestCase):
    """texture_index / assets_check / lua_check must take --game-data-files through StagedData."""

    def test_rebase_sends_texture_index_through_staged_data(self):
        import tempfile
        tidy = mwmanager_upstream.compose_tidy()
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp) / "Game"
            data = game / "Data Files"
            staging = Path(tmp) / "staging"
            data.mkdir(parents=True)
            staging.mkdir()
            previous = tidy.GAME_DATA_FILES
            index = getattr(tidy, "_TEX_INDEX", None)
            try:
                tidy.GAME_DATA_FILES = str(data)
                if index is not None:
                    index.clear()
                seen = []
                real = tidy.mwmanager_staged_data

                def hook(*args, **kwargs):
                    staged = real(*args, **kwargs)
                    seen.append(staged)
                    return staged

                with mock.patch.object(tidy, "mwmanager_staged_data", side_effect=hook):
                    tidy.texture_index(str(staging))
                self.assertTrue(seen, "texture_index did not call mwmanager_staged_data")
                self.assertEqual(Path(seen[0].game_dir).resolve(), game.resolve())
                self.assertEqual(Path(seen[0].data_dir).resolve(), data.resolve())
            finally:
                tidy.GAME_DATA_FILES = previous
                if index is not None:
                    index.clear()


class NonAsciiRename(unittest.TestCase):
    def test_a_renamed_files_original_goes_to_off_plugin_ascii(self):
        import tempfile
        mwmanager_upstream.compose()
        import mwdialog_assets
        with tempfile.TemporaryDirectory() as tmp:
            mod = Path(tmp, "Voices-123-1")
            (mod / "Sound" / "Vo").mkdir(parents=True)
            (mod / "Voices.esp").write_bytes(b"TES3")
            (mod / "Sound" / "Vo" / "café.mp3").write_bytes(b"original")
            (mod / "Sound" / "Vo" / "cafe.mp3").write_bytes(b"renamed earlier")
            said = []
            mwdialog_assets.rename_ascii(str(mod), str(mod / "Sound" / "Vo" / "café.mp3"), "Sound\\Vo\\café.mp3",
                                         "Sound\\Vo\\cafe.mp3", said.append)
            kept = mod / "Off" / "Voices" / "ascii" / "Sound" / "Vo" / "café.mp3"
            self.assertEqual(kept.read_bytes(), b"original")
            self.assertFalse((mod / "Off" / "Voices" / "names").exists())
            self.assertIn(os.path.join("Off", "Voices", "ascii", "Sound", "Vo", "café.mp3"), said[0])

            # an original an earlier version kept in Off\<plugin>\names\ still counts: no second copy
            (mod / "Sound" / "Vo" / "thé.mp3").write_bytes(b"again")
            (mod / "Sound" / "Vo" / "the.mp3").write_bytes(b"renamed earlier")
            old = mod / "Off" / "Voices" / "names" / "Sound" / "Vo" / "thé.mp3"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"first original")
            mwdialog_assets.rename_ascii(str(mod), str(mod / "Sound" / "Vo" / "thé.mp3"), "Sound\\Vo\\thé.mp3",
                                         "Sound\\Vo\\the.mp3", said.append)
            self.assertFalse((mod / "Sound" / "Vo" / "thé.mp3").exists())
            self.assertEqual(old.read_bytes(), b"first original")
            self.assertFalse((mod / "Off" / "Voices" / "ascii" / "Sound" / "Vo" / "thé.mp3").exists())


class AssetRepairHeartbeat(unittest.TestCase):
    def test_asset_repairs_say_how_far_they_are(self):
        import contextlib
        import io
        import itertools
        import tempfile
        mwmanager_upstream.compose()
        import mwdialog_assets
        with tempfile.TemporaryDirectory() as tmp:
            fixes = []
            for i in range(3):
                path = Path(tmp, f"m{i}.nif")
                path.write_bytes(b"NIF without absolute paths")
                fixes.append(("abs", str(path), f"Meshes\\m{i}.nif", {}))
            clock = itertools.count(0, 25)                 # 25 s between calls: every file is a slow one
            err = io.StringIO()
            with mock.patch.object(mwdialog_assets.time, "monotonic", lambda: next(clock)), \
                    contextlib.redirect_stderr(err):
                mwdialog_assets.apply(fixes, tmp, tmp, mock.Mock(), [])
            lines = err.getvalue().splitlines()
            self.assertEqual(lines[0], "Asset repairs: 3 file(s)...")
            self.assertIn("Asset repairs: 1 of 3 files...", lines)

class TidyApplyProgress(unittest.TestCase):
    """A 54,000-move package sat silent for about a minute before its first progress line."""

    def test_progress_starts_before_any_path_lookup_and_names_each_package(self):
        import contextlib
        import io
        import os
        import tempfile
        tidy = mwmanager_upstream.compose_tidy()
        with tempfile.TemporaryDirectory() as staging:
            moves = []
            for i in range(3):
                src = Path(staging, "Tamriel Data", f"f{i}.dds")
                src.parent.mkdir(exist_ok=True)
                src.write_bytes(b"x")
                moves.append((str(src), str(Path(staging, "Tamriel Data", "Textures", f"f{i}.dds")), "texture"))
            err = io.StringIO()
            journal = Path(staging, "journal.jsonl")
            with mock.patch.dict(os.environ, {"MWMANAGER_GUI": "1"}), \
                    mock.patch.dict(tidy.__dict__, {"JOURNAL": str(journal)}), \
                    mock.patch.object(tidy, "_path_plugin", side_effect=AssertionError("disk lookup per move")), \
                    contextlib.redirect_stderr(err):
                tidy.apply(moves, staging)
            self.assertTrue(err.getvalue().startswith("MWMANAGER-PROGRESS: Applying Tidy moves 0 of 3"))
            self.assertIn('"plugins": ["Tamriel Data"]', journal.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
