"""Grass plugins in optional FOMOD/BAIN folders still go to Grass\\ (CR-28: Rem-TOTSP.esp)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mock_fomod
import mwmanager_install as ext
import py_compat


def grass_plugin(path):
    import struct

    def sub(tag, data):
        return tag + struct.pack("<I", len(data)) + data

    def record(tag, body):
        return tag + struct.pack("<III", len(body), 0, 0) + body

    # blank TES3 author and description, as in the real Rem-TOTSP.esp
    hedr = sub(b"HEDR", struct.pack("<fI", 1.3, 0) + b"\0" * 288 + struct.pack("<I", 1))
    stat = sub(b"NAME", b"GRS_Tx_BM_grass_01.dds0\0") + sub(b"MODL", b"grass\\rem_sol_grass_01b.nif\0")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(record(b"TES3", hedr) + record(b"STAT", stat))


class OptionalGrassGoesToGrass(unittest.TestCase):
    def setUp(self):
        tmp = py_compat.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_a_top_level_optional_folder_still_sends_its_grass_plugin_to_grass(self):
        mod = self.root / "staging" / "Solstheim Graphical Replacer"
        for folder in ("012 Remiros' Groundcover for TOTSP", "12 Remiros' Groundcover for TOTSP",
                       "Optional - Groundcover"):
            with self.subTest(folder=folder):
                option = mod / folder / "Rem-TOTSP.esp"
                grass_plugin(option)
                plan = ext.plan_mod(str(mod))
                dest = next((Path(d) for s, d, _why in plan if d and Path(s) == option), None)
                self.assertEqual(dest, mod / "Grass" / "Rem-TOTSP.esp")
                option.unlink()

    def test_a_parked_optional_grass_plugin_moves_to_grass(self):
        mod = self.root / "staging" / "Solstheim Graphical Replacer"
        parked = (mod / "Extras" / "Solstheim Graphical Replacer"
                  / "012 Remiros' Groundcover for TOTSP" / "Rem-TOTSP.esp")
        grass_plugin(parked)
        plan = ext.plan_mod(str(mod))
        dest = next((Path(d) for s, d, _why in plan if d and Path(s) == parked), None)
        self.assertIsNotNone(dest)
        self.assertEqual(dest, (mod / "Grass" / "Rem-TOTSP.esp"))

    def test_activating_a_nested_option_path_puts_the_plugin_in_grass(self):
        package = self.root / "TOTSP"
        package.mkdir()
        src = self.root / "src" / "012 Remiros' Groundcover for TOTSP" / "Rem-TOTSP.esp"
        grass_plugin(src)
        rel = str(Path("012 Remiros' Groundcover for TOTSP") / "Rem-TOTSP.esp")
        record = {"key": "grass", "number": 12, "path": None, "payload_files": {rel: src}}
        mock_fomod._activate(package, "TOTSP.esm", [record], {"grass"}, {}, self.root / "bk")
        self.assertTrue((package / "Grass" / "Rem-TOTSP.esp").is_file())
        self.assertFalse((package / "012 Remiros' Groundcover for TOTSP" / "Rem-TOTSP.esp").exists())


    def test_an_unused_grass_plugin_choice_goes_to_grass_not_unused_options(self):
        package = self.root / "TOTSP"
        source = package / "Rem-TOTSP.esp"
        grass_plugin(source)
        record = {"key": "grass", "plugin_choice": True, "payload_files": {"Rem-TOTSP.esp": source}}
        self.assertEqual(mock_fomod._apply_plugin_choices(package, [record], set()), 1)
        self.assertTrue((package / "Grass" / "Rem-TOTSP.esp").is_file())
        self.assertFalse(any((package / "Extras").rglob("*.esp")))
        # asked again with the plugin already in Grass\: it stays, no renamed copy
        record["payload_files"] = {"Rem-TOTSP.esp": package / "Grass" / "Rem-TOTSP.esp"}
        self.assertEqual(mock_fomod._apply_plugin_choices(package, [record], set()), 0)
        self.assertEqual([p.name for p in (package / "Grass").iterdir()], ["Rem-TOTSP.esp"])

    def test_a_grass_plugin_parked_by_an_older_version_moves_to_grass(self):
        package = self.root / "TOTSP"
        parked = package / "Extras" / "TOTSP" / "unused-at-install" / "Rem-TOTSP.esp"
        grass_plugin(parked)
        record = {"key": "grass", "plugin_choice": True, "payload_files": {"Rem-TOTSP.esp": parked}}
        mock_fomod._apply_plugin_choices(package, [record], set())
        self.assertTrue((package / "Grass" / "Rem-TOTSP.esp").is_file())
        self.assertFalse(parked.exists())


class GrassNeverReachesConflicts(unittest.TestCase):
    def test_the_record_catalog_leaves_a_grass_plugin_out(self):
        import sqlite3
        from contextlib import closing
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "database"))
        import catalog
        with py_compat.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            data = Path(tmp) / "Morrowind" / "Data Files"
            grass_plugin(data / "Rem-TOTSP.esp")
            left_out = []
            esms, _bsas, missing = catalog.enumerate_expected(data, data.parent / "Morrowind.ini",
                                                              plugin_files=["Rem-TOTSP.esp"], left_out=left_out)
            self.assertEqual(esms, [])
            self.assertNotIn("Rem-TOTSP.esp", missing)   # left out, not missing (core archives aren't here)
            self.assertEqual([p.name for p in left_out], ["Rem-TOTSP.esp"])
            # an earlier catalog that still holds its records is cleaned by the next --append run
            db = Path(tmp) / "catalog.sqlite3"
            with closing(sqlite3.connect(db)) as con:
                catalog.create_schema(con, catalog.Path(catalog.__file__).with_name("schema.sql"))
                catalog.index_esm(con, data / "Rem-TOTSP.esp", 3)
                con.commit()

            def no_core(data_dir, ini, canonical_core=False, plugin_files=None, left_out=None):
                left_out.append(data / "Rem-TOTSP.esp")
                return [], [], []
            from unittest import mock
            with mock.patch.object(catalog, "enumerate_expected", no_core):
                self.assertEqual(catalog.main(["--game-dir", str(data.parent), "--db", str(db),
                                               "--plugin-files", "Rem-TOTSP.esp", "--append"]), 0)
            with closing(sqlite3.connect(db)) as con:
                left = con.execute("SELECT count(*) FROM esm_record WHERE lower(source_file)='rem-totsp.esp'")
                self.assertEqual(left.fetchone()[0], 0)


class OptionalGrassDescription(unittest.TestCase):
    def test_an_empty_plugin_header_leaves_only_the_folder_name_and_the_grass_note(self):
        """Rem-TOTSP.esp ships with a blank TES3 description, and this archive has no ModuleConfig.xml."""
        with py_compat.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp) / "SGR"
            option = root / "012 Remiros' Groundcover for TOTSP"
            option.mkdir(parents=True)
            grass_plugin(option / "Rem-TOTSP.esp")
            self.assertEqual(mock_fomod._plugin_description(option / "Rem-TOTSP.esp"), "")
            package = Path(tmp) / "TOTSP"
            package.mkdir()
            records = mock_fomod._numbered_folders(root, {"label": "Solstheim Graphical Replacer"}, package)
            mock_fomod._mark_grass_options(records, package)
            grass = next(r for r in records if "Groundcover" in r["label"])
            self.assertEqual(grass["label"], "Remiros' Groundcover for TOTSP")
            self.assertEqual(grass["group_label"], "Grass (MGE XE groundcover)")
            self.assertFalse(grass.get("fomod_description"))
            docs = grass.get("docs") or ""
            self.assertIn("MGE XE grass plugin", docs)
            self.assertIn("Remiros' Groundcover", docs)
            self.assertEqual(mock_fomod._option_description(grass, package), docs.strip())



class GrassStillOptionalAfterTidy(unittest.TestCase):
    def setUp(self):
        tmp = py_compat.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def _records(self, package, installed=None):
        """The same discovery order the installer uses after tidy has already run."""
        installed = installed or set()
        records = mock_fomod._discover(package, installed)
        records.extend(mock_fomod._discover_plugin_choices(
            package, installed, mock_fomod._represented_option_sources(records)))
        records.extend(mock_fomod._discover_grass_plugins(package, records, installed))
        mock_fomod._mark_grass_options(records, package)
        return records

    def test_a_grass_plugin_already_in_grass_is_still_an_optional_choice(self):
        """After tidy moves Rem-TOTSP.esp into the package Grass\\ folder, the mock FOMOD still
        lists it as an optional plugin. Rem-TOTSP.esp has no TES3 description and no
        ModuleConfig.xml; fomod_description is left empty so a description can be hardcoded later.
        Left unchecked: the prerequisite (Remiros' Groundcover) is not staged, and the filename
        is not enough for the existing 'for <mod>' tick rule."""
        package = self.root / "TOTSP"
        plugin = package / "Grass" / "Rem-TOTSP.esp"
        grass_plugin(plugin)
        (package / "TOTSP.esm").write_bytes(b"TES3")     # the package master, not an option
        records = self._records(package)
        grass = [r for r in records if r.get("payload_files") and "Rem-TOTSP.esp" in {
            Path(name).name for name in r["payload_files"]}]
        self.assertEqual(len(grass), 1)
        record = grass[0]
        self.assertFalse(record["required"])
        self.assertTrue(record.get("always_optional") or not record.get("recommended"))
        self.assertFalse(record.get("recommended"))
        self.assertEqual(record["group_label"], "Grass (MGE XE groundcover)")
        self.assertEqual(record["selection_type"], "SelectAny")
        self.assertEqual(record["label"], "Rem-TOTSP.esp")
        self.assertEqual(record.get("fomod_description") or "", "")
        self.assertEqual(mock_fomod._plugin_description(plugin), "")
        # the generic MGE XE note is not a plugin description Dej would hardcode
        self.assertFalse(mock_fomod._option_description(record, package).startswith("Remiros"))

    def test_a_non_grass_plugin_in_the_grass_folder_is_not_listed(self):
        package = self.root / "TOTSP"
        package.mkdir()
        # a TES3 header and a door, not grass statics: not an MGE XE groundcover plugin
        import struct

        def sub(tag, data):
            return tag + struct.pack("<I", len(data)) + data

        def record(tag, body):
            return tag + struct.pack("<III", len(body), 0, 0) + body

        hedr = sub(b"HEDR", struct.pack("<fI", 1.3, 0) + b"\0" * 288 + struct.pack("<I", 1))
        door = sub(b"NAME", b"somedoor\0") + sub(b"MODL", b"d\\door.nif\0")
        path = package / "Grass" / "SomeDoor.esp"
        path.parent.mkdir(parents=True)
        path.write_bytes(record(b"TES3", hedr) + record(b"DOOR", door))
        self.assertFalse(mock_fomod._is_grass_plugin(path))
        self.assertEqual(self._records(package), [])

    def test_a_numbered_folder_option_is_not_listed_again_from_grass(self):
        """If the optional folder is still present (tidy has not run), discovery keeps that
        entry and does not add a second Rem-TOTSP.esp row from Grass\\."""
        package = self.root / "SGR"
        option = package / "012 Remiros' Groundcover for TOTSP"
        grass_plugin(option / "Rem-TOTSP.esp")
        # a leftover or pre-move copy in Grass\\ should not produce a duplicate
        grass_plugin(package / "Grass" / "Rem-TOTSP.esp")
        records = mock_fomod._numbered_folders(package, {"label": "SGR"}, package)
        records.extend(mock_fomod._discover_grass_plugins(package, records))
        mock_fomod._mark_grass_options(records, package)
        totsp = [r for r in records if "TOTSP" in r["label"] or r["label"] == "Rem-TOTSP.esp"]
        self.assertEqual(len(totsp), 1)
        self.assertEqual(totsp[0]["label"], "Remiros' Groundcover for TOTSP")
        self.assertEqual(totsp[0].get("fomod_description") or "", "")


if __name__ == "__main__":
    unittest.main()
