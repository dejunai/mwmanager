"""Tidy, luafix and the converter write under staging, not Data Files or the game folder.

A source guard fails if MWManager launches those tools with Data Files or game_dir as
the write/working root. Read-only lookups via --game-data-files are allowed.
Deploy-time stand-ins and patches (--fix/--full/--multipatch after deploy) are not
covered by this guard. Every path here is a temporary folder.
"""
import ast
import tempfile
import unittest
from pathlib import Path

import workflow_safety


# MWManager-owned modules that launch tidy / luafix / the converter (not vendored copies).
_OWN = (
    "mwmanager_gui.py",
    "mwmanager_tidy_ext.py",
    "mwmanager_install.py",
    "mwmanager_overrides.py",
    "mwmanager_init.py",
    "plugin_install.py",
    "major_master_deploy.py",
    "preinstall.py",
)


def _repo() -> Path:
    return Path(__file__).resolve().parents[1]


class StagingWriteRoot(unittest.TestCase):
    def test_data_files_is_rejected_as_a_write_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp) / "Game"
            data = game / "Data Files"
            staging = Path(tmp) / "staging" / "plugins"
            data.mkdir(parents=True)
            staging.mkdir(parents=True)
            with self.assertRaises(ValueError):
                workflow_safety.require_staging_write_root(data, data_dir=data, game_dir=game)
            with self.assertRaises(ValueError):
                workflow_safety.require_staging_write_root(game, data_dir=data, game_dir=game)
            with self.assertRaises(ValueError):
                workflow_safety.require_staging_write_root(data / "Meshes", data_dir=data, game_dir=game)
            self.assertEqual(
                workflow_safety.require_staging_write_root(staging, data_dir=data, game_dir=game),
                staging.resolve(),
            )
            self.assertTrue(workflow_safety.is_data_files_tree(data))
            self.assertFalse(workflow_safety.is_data_files_tree(staging))

    def test_run_tidy_steps_refuses_data_files_as_staging(self):
        import mwmanager_gui as g
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "Game" / "Data Files"
            data.mkdir(parents=True)
            with self.assertRaises(ValueError):
                g.run_tidy_steps("py", data, data, lambda _m: None, "Tidy")

    def test_source_does_not_hand_data_files_to_tidy_as_the_write_root(self):
        """Fail if a MWManager-owned caller launches Tidy/luafix/converter against Data Files."""
        findings = []
        for name in _OWN:
            path = _repo() / name
            if not path.is_file():
                continue
            source = path.read_text(encoding="utf-8")
            # The old Initial Tidy command: --init with only --game-data-files (no --staging).
            if "TIDY_SCRIPT" in source or "mwmanager_install.py" in source:
                if '--init", "--game-data-files"' in source or "--init', '--game-data-files'" in source:
                    findings.append(f"{name}: launches Tidy --init with Data Files as the write root")
            # run_initial_tidy must not call the scanner on data_dir.
            if name == "mwmanager_tidy_ext.py":
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if not isinstance(node, ast.FunctionDef) or node.name != "run_initial_tidy":
                        continue
                    text = ast.get_source_segment(source, node) or ""
                    if "_scanner.main" in text or "run_data_files_tidy" in text:
                        findings.append("mwmanager_tidy_ext.py: run_initial_tidy still writes to Data Files")
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
