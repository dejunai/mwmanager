"""Round 3 of the CR-25 stress run: after 'Install mods your options need', MWManager reopens the major masters'
options to put in the ones that waited on those mods. The masters were deployed, so assert_package_disposable
refused ("... is referenced by a deployment; remove that deployment first"). The reopen now removes that
deployment first; the masters are offered for deployment again afterwards."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _reopen_options():
    tree = ast.parse((ROOT / "mwmanager_gui.py").read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "reopen_options")


class WaitingOptionsReopen(unittest.TestCase):
    def test_deployment_is_removed_before_the_packages_are_reprocessed(self):
        src = ast.unparse(_reopen_options())
        self.assertIn("remove_deployed(staging, game_data_files, APP_DIR", src)
        self.assertLess(src.index("remove_deployed("), src.index("assert_package_disposable("))
        self.assertIn("tracked_deployment_masters(APP_DIR)", src)

    def test_only_the_waiting_options_follow_up_removes_it(self):
        fn = _reopen_options()
        guard = next(n for n in ast.walk(fn) if isinstance(n, ast.If) and "remove_deployed" in ast.unparse(n)
                     and ast.unparse(n.test) == "reuse_answers")
        self.assertIsNotNone(guard)

    def test_changed_files_stop_the_reopen(self):
        src = ast.unparse(_reopen_options())
        self.assertRegex(src, r"removed\.get\('pending'\) or removed\.get\('edited'\)")


if __name__ == "__main__":
    unittest.main()