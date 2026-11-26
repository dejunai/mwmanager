r"""The suite's Lua lives only under Data Files\MWSE\mods\mwsuite, as in the Steam install, and shows as the one
"MW Conflict Tools" Mod Config Menu entry. These tests state that as rules over what --init installs, so a new
suite mod, a new metadata file or a new deploy step cannot put suite Lua under another heading. Every path is a
temporary folder or relative to the repository."""
import contextlib
import io
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import mwmanager_upstream  # noqa: E402

GUARDS = ROOT / "reference" / "mwse-lua-guards"
MENU = ROOT / "reference" / "mwmanager-menu"
UPSTREAM_SUITE = ROOT / "tools" / "steam-upstream" / "lua tools" / "MWSE" / "mods" / "mwsuite"


def suite_names():
    return sorted(p.name for p in (GUARDS / "mods").iterdir() if (p / "main.lua").is_file()) + ["mwmanagermenu"]


class SuiteLayoutRule(unittest.TestCase):
    def setUp(self):
        import mwmanager as m
        self.m = m
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.manager = base / "MWManager"
        self.manager.mkdir()
        self.data = base / "Game" / "Data Files"
        self.mwse = self.data / "MWSE"
        (self.mwse / "mods" / "somemod").mkdir(parents=True)
        (self.mwse / "mods" / "somemod" / "main.lua").write_text("-- third party\n", encoding="utf-8")
        self.patch = mwmanager_upstream.use_manager_dir(self.manager)
        self.patch.__enter__()

    def tearDown(self):
        self.patch.__exit__(None, None, None)
        self.tmp.cleanup()

    def install(self, dry_run=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.m.install_mwse_lua_guards(
                types.SimpleNamespace(data_dir=str(self.data), dry_run=dry_run)), 0)
        return out.getvalue()

    def installed_files(self):
        return sorted(p.relative_to(self.data).as_posix() for p in self.data.rglob("*") if p.is_file())

    def test_every_suite_file_is_under_mwsuite_and_metadata_sits_beside_the_esms(self):
        self.install()
        suite_files = [f for f in self.installed_files() if not f.startswith("MWSE/mods/somemod/")]
        for rel in suite_files:
            parts = rel.split("/")
            if parts[0] == "MWSE":
                self.assertEqual(parts[:3], ["MWSE", "mods", "mwsuite"], rel)
            else:
                self.assertEqual(len(parts), 1, rel)
                self.assertTrue(rel.endswith("-metadata.toml"), rel)
        # no suite module folder anywhere else under MWSE\mods, and MWManager adds no heading of its own
        self.assertEqual(sorted(p.name for p in (self.mwse / "mods").iterdir()), ["mwsuite", "somemod"])
        mods = {p.name for p in (self.mwse / "mods" / "mwsuite").iterdir() if p.is_dir()}
        self.assertEqual(mods, set(suite_names()))

    def test_layout_is_the_steam_layout_bootstrap_ownmods_and_module_folders(self):
        self.install()
        suite = self.mwse / "mods" / "mwsuite"
        self.assertEqual(sorted(p.name for p in suite.iterdir() if p.is_file()), ["main.lua", "ownmods.json"])
        self.assertEqual((suite / "ownmods.json").read_bytes(), (UPSTREAM_SUITE / "ownmods.json").read_bytes())
        self.assertEqual((suite / "main.lua").read_bytes(),
                         (MENU / "bootstrap" / "main.lua").read_bytes())
        listed = json.loads((suite / "ownmods.json").read_text("utf-8"))["ownMods"]
        self.assertTrue(set(suite_names()) <= set(listed), sorted(set(suite_names()) - set(listed)))

    def test_every_metadata_file_points_at_the_mwsuite_folder(self):
        self.install()
        found = sorted(self.data.glob("*-metadata.toml"))
        self.assertTrue(found)
        for toml in found:
            text = toml.read_text("utf-8")
            lua_mod = re.search(r'^lua-mod\s*=\s*"([^"]+)"', text, re.M).group(1)
            stem = toml.name[:-len("-metadata.toml")]
            self.assertEqual(lua_mod, "mwsuite" if stem == "mwsuite" else "mwsuite." + stem, toml.name)

    def test_one_menu_entry_named_mw_conflict_tools(self):
        self.install()
        installed = (self.mwse / "mods" / "mwsuite" / "mwmanagermenu" / "main.lua").read_text("utf-8")
        name = re.search(r'^local NAME = "([^"]+)"', installed, re.M).group(1)
        self.assertEqual(name, "MW Conflict Tools")
        self.assertEqual(name, self.m.SUITE_NAME)
        self.assertEqual(self.m.SUITE_FOLDER, "mwsuite")
        for toml in (MENU / "metadata").glob("*-metadata.toml"):
            if "description" in toml.read_text("utf-8") and "Conflict" in toml.read_text("utf-8"):
                self.assertIn("MW Conflict Tools", toml.read_text("utf-8"))
        # no suite mod registers a menu entry of its own through MWManager's code: only the menu names the suite
        for source in (ROOT / "mwmanager_init.py", ROOT / "preinstall.py"):
            self.assertNotIn("MWManager's MWSE Lua", source.read_text("utf-8"))

    def test_an_old_layout_is_migrated_through_the_ledger_with_a_backup_and_user_files_stay(self):
        import mwmanager_journal
        flat = self.mwse / "mods" / "mwguard"
        flat.mkdir(parents=True)
        (flat / "main.lua").write_text("-- flat copy\n", encoding="utf-8")
        old_heading = self.mwse / "mods" / "MWManager" / "modules" / "mwcollide"
        old_heading.mkdir(parents=True)
        (old_heading / "main.lua").write_text("-- under the old heading\n", encoding="utf-8")
        (self.mwse / "mods" / "mwsuite").mkdir(parents=True)
        (self.mwse / "mods" / "mwsuite" / "ownmods.json").write_text("{\"version\": 1, \"ownMods\": [\"old\"]}\n",
                                                                    encoding="utf-8")
        journal = mwmanager_journal.Journal(self.manager / "journal")
        journal.capture_baseline(self.data.parent)
        self.install()
        self.assertEqual(sorted(p.name for p in (self.mwse / "mods").iterdir()), ["mwsuite", "somemod"])
        backups = self.mwse / "backups" / "mods"
        self.assertEqual((backups / "mwguard" / "main.lua").read_text("utf-8"), "-- flat copy\n")
        self.assertEqual((backups / "MWManager" / "modules" / "mwcollide" / "main.lua").read_text("utf-8"),
                         "-- under the old heading\n")
        self.assertEqual((self.mwse / "mods" / "somemod" / "main.lua").read_text("utf-8"), "-- third party\n")
        self.assertNotIn("old", (self.mwse / "mods" / "mwsuite" / "ownmods.json").read_text("utf-8").split("ownMods")[1][:12])
        before = self.installed_files()
        self.install()
        self.assertEqual(self.installed_files(), before)            # nothing more to migrate: a second run is idle
        entries, problems = journal.entries()
        self.assertEqual(problems, [])
        self.assertEqual({e.get("tag") for e in entries}, {"mwse-lua-suite"})
        plan = journal.plan_rollback(self.data.parent)
        self.assertEqual((plan["refuse"], plan["unexplained"]), ([], []))
        journal.execute_rollback(self.data.parent, plan)
        self.assertEqual((flat / "main.lua").read_text("utf-8"), "-- flat copy\n")
        self.assertTrue((self.mwse / "mods" / "MWManager" / "modules" / "mwcollide" / "main.lua").is_file())
        self.assertIn("old", (self.mwse / "mods" / "mwsuite" / "ownmods.json").read_text("utf-8"))

    def test_dry_run_names_ownmods_and_writes_nothing(self):
        before = self.installed_files()
        out = self.install(dry_run=True)
        self.assertIn("ownmods.json", out)
        self.assertEqual(self.installed_files(), before)


class NoCodeDeploysSuiteLuaElsewhere(unittest.TestCase):
    r"""MWManager's own source (not the unchanged upstream copies) names no folder under MWSE\mods but mwsuite for
    suite Lua; the only other names are the retired headings it moves out of the way."""

    OWN = ("mwmanager_init.py", "preinstall.py", "mwmanager_overrides.py", "mwmanager_gui.py", "mwmanager_journal.py")

    def test_only_retired_headings_are_named_besides_mwsuite(self):
        import mwmanager_init
        for name in self.OWN:
            text = (ROOT / name).read_text("utf-8")
            for m in re.finditer(r'MWSE[\\/]+mods[\\/]+([A-Za-z0-9_-]+)', text):
                self.assertIn(m.group(1).casefold(),
                              {"mwsuite", "mwmanager", "mwquickfixes", "mwplainpaperfix", "mge", "readme"}
                              | {n.casefold() for n in mwmanager_init.RETIRED_MWSE_MODS},
                              f"{name}: {m.group(0)}")
        self.assertEqual({n.casefold() for n in mwmanager_init.RETIRED_MWSE_MODS},
                         {"mwmanager", "mwquickfixes", "mwplainpaperfix"})


if __name__ == "__main__":
    unittest.main()
