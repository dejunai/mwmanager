"""Staged Tidy makes its stand-ins from the game's Morrowind.bsa (CR-25 round 2: the missing-asset pass stopped with
FileNotFoundError because a staged Tidy runs outside the game folder)."""
import ast
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    tree = ast.parse((ROOT / "mwmanager_tidy_ext.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_ensure_dummy_assets")
    ns = {"os": os}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "mwmanager_tidy_ext.py", "exec"), ns)
    return ns["_ensure_dummy_assets"], tree


class StagedDummyAssets(unittest.TestCase):
    def test_stand_ins_come_from_the_game_folder_of_data_files(self):
        ensure, _ = _load()
        calls = []
        fake = types.SimpleNamespace(DUMMY_SOURCES={"mwmanager_dummy.nif": "meshes\\\\m\\\\misc_dwrv_coin00.nif",
                                                    "mwmanager_dummy.kf": "meshes\\\\i\\\\xin_dagoth_bridge00.kf"},
                                     dummy_path=lambda name, game_dir=None: calls.append((name, game_dir)) or name)
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "Game", "Data Files")
            old = sys.modules.get("mwdialog_media")
            sys.modules["mwdialog_media"] = fake
            try:
                made = ensure(data)
            finally:
                if old is None:
                    sys.modules.pop("mwdialog_media", None)
                else:
                    sys.modules["mwdialog_media"] = old
        self.assertEqual(len(made), 2)
        self.assertEqual({g for _, g in calls}, {os.path.join(tmp, "Game")})

    def test_a_missing_bsa_is_reported_not_raised(self):
        ensure, _ = _load()
        lines = []

        def boom(name, game_dir=None):
            raise FileNotFoundError("no Morrowind.bsa")
        fake = types.SimpleNamespace(DUMMY_SOURCES={"mwmanager_dummy.nif": "x"}, dummy_path=boom)
        old = sys.modules.get("mwdialog_media")
        sys.modules["mwdialog_media"] = fake
        try:
            self.assertEqual(ensure("Data Files", log=lines.append), [])
        finally:
            if old is None:
                sys.modules.pop("mwdialog_media", None)
            else:
                sys.modules["mwdialog_media"] = old
        self.assertEqual(len(lines), 1)
        self.assertEqual(ensure(None), [])

    def test_assets_apply_makes_stand_ins_before_resolving(self):
        src = (ROOT / "mwmanager_tidy_ext.py").read_text(encoding="utf-8")
        body = src[src.index("def assets_apply("):]
        self.assertLess(body.index("_ensure_dummy_assets("), body.index("resolve_missing_assets("))


if __name__ == "__main__":
    unittest.main()