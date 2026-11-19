"""Downloaded archives and FOMOD configs are untrusted: staging limits and the guarded XML parser."""
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core_addons  # noqa: E402
import mock_fomod  # noqa: E402
import plugin_install  # noqa: E402

CONFIG = '<?xml version="1.0"?>\n<config><moduleName>Test</moduleName></config>\n'


class StagingArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def zip(self, members, link=None):
        path = self.tmp / "master.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in members.items():
                zf.writestr(name, data)
            if link:
                info = zipfile.ZipInfo(link)
                info.external_attr = 0o120777 << 16
                zf.writestr(info, "../../outside")
        return path

    def test_a_normal_archive_passes(self):
        core_addons._check_staging_archive(self.zip({"Data/A.esm": b"TES3"}), None)

    def test_a_link_member_is_refused(self):
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            core_addons._check_staging_archive(self.zip({"A.esm": b"TES3"}, link="Meshes/x"), None)

    def test_size_and_count_limits_match_the_plugin_installer(self):
        archive = self.zip({"A.esm": b"x" * 100, "B.esm": b"x" * 100})
        with mock.patch.object(plugin_install, "MAX_EXPANDED_MEMBER_BYTES", 99):
            with self.assertRaisesRegex(ValueError, "member expands"):
                core_addons._check_staging_archive(archive, None)
        with mock.patch.object(plugin_install, "MAX_EXPANDED_ARCHIVE_BYTES", 150):
            with self.assertRaisesRegex(ValueError, "expands beyond"):
                core_addons._check_staging_archive(archive, None)
        with mock.patch.object(plugin_install, "MAX_ARCHIVE_MEMBERS", 1):
            with self.assertRaisesRegex(ValueError, "too many members"):
                core_addons._check_staging_archive(archive, None)

    def test_seven_zip_listing_links_are_refused(self):
        records = [{"path": "A.esm", "size": "4"}, {"path": "Meshes/x", "size": "0", "symbolic link": "../out"}]
        with mock.patch.object(core_addons, "_seven_zip_listing", return_value=({}, records, None)):
            with self.assertRaisesRegex(ValueError, "Meshes/x"):
                core_addons._check_staging_archive(self.tmp / "master.7z", "7z.exe")


class FomodConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_a_plain_config_parses(self):
        path = self.tmp / "ModuleConfig.xml"
        path.write_text(CONFIG, encoding="utf-16")
        self.assertEqual(mock_fomod.parse_fomod_config(path).find("moduleName").text, "Test")

    def test_a_dtd_is_refused_in_any_encoding(self):
        bomb = CONFIG.replace("<config>", '<!DOCTYPE config [<!ENTITY a "aaaa">]><config>')
        for encoding in ("utf-8", "utf-16"):
            path = self.tmp / f"{encoding}.xml"
            path.write_text(bomb, encoding=encoding)
            with self.assertRaises(ET.ParseError):
                mock_fomod.parse_fomod_config(path)

    def test_an_oversized_config_is_refused(self):
        path = self.tmp / "ModuleConfig.xml"
        path.write_text(CONFIG)
        with mock.patch.object(mock_fomod, "MAX_FOMOD_CONFIG_BYTES", 10):
            with self.assertRaises(ET.ParseError):
                mock_fomod.parse_fomod_config(path)


if __name__ == "__main__":
    unittest.main()
