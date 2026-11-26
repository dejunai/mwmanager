"""Tamriel_Data HD/SD swap: install HD, swap to SD, swap back, with no leftover files.

Archives and the game folder are tiny temporary fixtures. Nothing here touches a real
install, Vortex, or the network.
"""
from __future__ import annotations

import io
import json
import os
import re
import struct
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import core_addons as ca  # noqa: E402
import major_master_deploy as deploy  # noqa: E402
from mwmanager_journal import Journal  # noqa: E402

MASTERS = ["Tamriel_Data.esm", "TR_Mainland.esm", "Sky_Main.esm", "PC_Main.esm"]
HD_ARCHIVE = "Tamriel Data (HD) 59927 26.08 2026-08-23T04-15Z a.zip"
SD_ARCHIVE = "Tamriel_Data 44537 26.09 2026-09-23T04-15Z b.zip"
TR_ARCHIVE = "Tamriel Rebuilt 42145 26.11.19 c.zip"
SKY_ARCHIVE = "Sky Home of the Nords 54000 1.0 d.zip"
PC_ARCHIVE = "Province Cyrodiil 54100 1.0 e.zip"

HD_MARK = b"HD-MARKER-TEXTURE"
SD_MARK = b"SD-MARKER-TEXTURE"
HD_ONLY = b"HD-ONLY-TEXTURE"
HD_NMAP = b"HD-NORMAL-MAP"
TR_KEEP = b"TR-SHOULD-STAY"
SKY_KEEP = b"SKY-SHOULD-STAY"
PC_KEEP = b"PC-SHOULD-STAY"


def put(root, rel, data):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def tes3_esm(*masters):
    body = b"".join(
        b"MAST" + struct.pack("<I", len(name) + 1) + name.encode() + b"\0" +
        b"DATA" + struct.pack("<I", 8) + bytes(8) for name in masters)
    return struct.pack("<4sIII", b"TES3", len(body), 0, 0) + body


def tes3_bsa(members: dict[str, bytes]) -> bytes:
    names = b"".join(name.encode("latin-1") + b"\0" for name in members)
    count = len(members)
    name_offsets, offset = [], 0
    for name in members:
        name_offsets.append(offset)
        offset += len(name.encode("latin-1")) + 1
    recs = b""
    data = b""
    relative = 0
    for blob in members.values():
        recs += struct.pack("<II", len(blob), relative)
        data += blob
        relative += len(blob)
    name_table = b"".join(struct.pack("<I", item) for item in name_offsets)
    directory = recs + name_table + names
    header = struct.pack("<III", 0x100, len(directory), count)
    return header + directory + (b"\0" * (8 * count)) + data


def write_zip(path: Path, members: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as stream:
        for name, data in members.items():
            stream.writestr(name, data)
    return path


def copy_catalog(manager: Path) -> None:
    source = ROOT / "database"
    target = manager / "database"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("catalog.py", "schema.sql"):
        (target / name).write_bytes((source / name).read_bytes())


def write_ini(game: Path, plugins, archives=()) -> None:
    lines = ["[Game Files]"]
    lines += [f"GameFile{i}={name}" for i, name in enumerate(plugins)]
    lines += ["", "[Archives]"]
    lines += [f"Archive {i}={name}" for i, name in enumerate(archives)]
    (game / "Morrowind.ini").write_text("\n".join(lines) + "\n", encoding="cp1252")
    (game / "mwdialog_load_order.txt").write_text("\n".join(plugins) + "\n", encoding="utf-8")


def read_ini_plugins(game: Path) -> list[str]:
    return deploy_read_section(game / "Morrowind.ini", "Game Files", "gamefile")


def read_ini_archives(game: Path) -> list[str]:
    return deploy_read_section(game / "Morrowind.ini", "Archives", "archive")


def deploy_read_section(path: Path, section: str, prefix: str) -> list[str]:
    text = path.read_text(encoding="cp1252", errors="replace")
    current = None
    entries = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].casefold()
            continue
        if current == section.casefold() and "=" in line:
            key, value = line.split("=", 1)
            if key.strip().casefold().startswith(prefix):
                plugin = value.strip()
                if plugin:
                    entries.append((key.strip(), plugin))
    def index(key: str) -> int:
        digits = "".join(ch for ch in key if ch.isdigit())
        return int(digits) if digits else 0
    return [name for _key, name in sorted(entries, key=lambda item: index(item[0]))]


def read_load_order(game: Path) -> list[str]:
    return [line.strip() for line in (game / "mwdialog_load_order.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith(";")]


def archive_item(path: Path, masters) -> dict:
    return {
        "archive": str(path),
        "plugins": list(masters),
        "bsas": [name for name in zipfile.ZipFile(path).namelist() if name.lower().endswith(".bsa")],
        "core_masters": list(masters),
        "sha256": ca._sha256(path),
        "install_blocked": False,
        "inspection_error": None,
        "filename_version": "1.0",
    }


def plan_for(downloads: Path, items: list[dict], using: str) -> dict:
    return {
        "downloads_dir": str(downloads),
        "archives": items,
        "master_variants": {"tamriel_data.esm": {"available": ["HD", "SD"], "using": using}},
    }


class TamrielDataSwap(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.manager = self.root / "manager"
        self.game = self.root / "game"
        self.downloads = self.root / "dl"
        self.data = self.game / "Data Files"
        self.manager.mkdir()
        self.data.mkdir(parents=True)
        self.downloads.mkdir()
        (self.manager / "mwmanager_core_addons.json").write_text(json.dumps({
            "tamriel_data.esm": ["tamriel data", "tamriel_data"],
            "tr_mainland.esm": ["tamriel rebuilt", "tr_mainland"],
            "sky_main.esm": ["sky", "sky_main"],
            "pc_main.esm": ["cyrodiil", "pc_main"],
        }), encoding="utf-8")
        for name in ("Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm"):
            put(self.data, name, tes3_esm())
        # Load order names the dependents up front so --verify does not treat the list as a later edit.
        write_ini(self.game, ["Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm", *MASTERS])
        copy_catalog(self.manager)
        Journal(self.manager / "journal").capture_baseline(self.game)

        write_zip(self.downloads / HD_ARCHIVE, {
            "Tamriel_Data.esm": tes3_esm(),
            "textures/td/marker.dds": HD_MARK,
            "textures/td/hd_only.dds": HD_ONLY,
            "textures/td/marker_n.dds": HD_NMAP,
        })
        write_zip(self.downloads / SD_ARCHIVE, {
            "Tamriel_Data.esm": tes3_esm(),
            "Tamriel_Data.bsa": tes3_bsa({"textures/td/sd_packed.dds": SD_MARK}),
            "textures/td/marker.dds": SD_MARK,
        })
        write_zip(self.downloads / TR_ARCHIVE, {
            "TR_Mainland.esm": tes3_esm("Tamriel_Data.esm"),
            "textures/tr/keep.dds": TR_KEEP,
        })
        write_zip(self.downloads / SKY_ARCHIVE, {
            "Sky_Main.esm": tes3_esm("Tamriel_Data.esm"),
            "textures/sky/keep.dds": SKY_KEEP,
        })
        write_zip(self.downloads / PC_ARCHIVE, {
            "PC_Main.esm": tes3_esm("Tamriel_Data.esm"),
            "textures/pc/keep.dds": PC_KEEP,
        })
        self.hd = self.downloads / HD_ARCHIVE
        self.sd = self.downloads / SD_ARCHIVE
        self.dependents = [
            archive_item(self.downloads / TR_ARCHIVE, ["TR_Mainland.esm"]),
            archive_item(self.downloads / SKY_ARCHIVE, ["Sky_Main.esm"]),
            archive_item(self.downloads / PC_ARCHIVE, ["PC_Main.esm"]),
        ]
        self.staging = self.manager / "staging" / "major_masters"

    def tearDown(self):
        self._tmp.cleanup()

    def _plan(self, using: str) -> dict:
        td = archive_item(self.hd if using == "HD" else self.sd, ["Tamriel_Data.esm"])
        return plan_for(self.downloads, [td, *self.dependents], using)

    def _stage_and_deploy(self, using: str, selected=None):
        selected = selected or MASTERS
        ca.choose_master_variant(self.manager, "Tamriel_Data.esm", using)
        staged = ca.stage_core_master_archives(
            self._plan(using), selected, self.manager, self.game, clear_staging=False)
        with mock.patch.object(sys, "path", list(sys.path)), mock.patch.dict(sys.modules):
            sys.modules.pop("catalog", None)
            result = deploy.deploy(self.staging, self.data, self.manager, MASTERS, selected=selected)
        write_ini(self.game,
                  ["Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm", *MASTERS],
                  [name for name in result.get("archives", [])])
        return staged, result

    def _swap(self, label: str):
        with mock.patch.object(sys, "path", list(sys.path)), mock.patch.dict(sys.modules):
            sys.modules.pop("catalog", None)
            return ca.swap_master_variant(
                self.manager, self.game, "Tamriel_Data.esm", label, plan=self._plan(label))

    def _verify(self):
        report = Journal(self.manager / "journal").verify(self.game, against="deployed", full_hash=True)
        self.assertTrue(report["ok"], report)
        return report

    def _assert_dependents(self):
        for name, rel, blob in (
            ("TR_Mainland.esm", "textures/tr/keep.dds", TR_KEEP),
            ("Sky_Main.esm", "textures/sky/keep.dds", SKY_KEEP),
            ("PC_Main.esm", "textures/pc/keep.dds", PC_KEEP),
        ):
            self.assertTrue((self.data / name).is_file(), name)
            self.assertEqual((self.data / rel).read_bytes(), blob)
        expected = ["Morrowind.esm", "Tribunal.esm", "Bloodmoon.esm", *MASTERS]
        self.assertEqual(read_ini_plugins(self.game), expected)
        self.assertEqual(read_load_order(self.game), expected)

    def _assert_hd(self):
        self.assertEqual((self.data / "textures/td/marker.dds").read_bytes(), HD_MARK)
        self.assertEqual((self.data / "textures/td/hd_only.dds").read_bytes(), HD_ONLY)
        self.assertEqual((self.data / "textures/td/marker_n.dds").read_bytes(), HD_NMAP)
        self.assertFalse((self.data / "Tamriel_Data.bsa").exists())
        self.assertNotIn("Tamriel_Data.bsa", {name.casefold() for name in read_ini_archives(self.game)})
        self.assertTrue((self.data / "Tamriel_Data.esm").is_file())
        self._assert_dependents()

    def _assert_sd(self):
        self.assertEqual((self.data / "textures/td/marker.dds").read_bytes(), SD_MARK)
        self.assertFalse((self.data / "textures/td/hd_only.dds").exists())
        self.assertFalse((self.data / "textures/td/marker_n.dds").exists())
        self.assertTrue((self.data / "Tamriel_Data.bsa").is_file())
        self.assertIn("Tamriel_Data.bsa", read_ini_archives(self.game))
        self.assertTrue((self.data / "Tamriel_Data.esm").is_file())
        self._assert_dependents()

    def test_hd_install_then_sd_swap_then_hd_restore(self):
        with redirect_stdout(io.StringIO()):
            self._stage_and_deploy("HD")
            self._assert_hd()
            self._verify()

            swapped = self._swap("SD")
            self.assertTrue(swapped.get("replaced") or swapped.get("ready") == ["Tamriel_Data.esm"])
            self._assert_sd()
            self._verify()
            self.assertEqual(ca.master_variant_choices(self.manager)["tamriel_data.esm"], "SD")
            # The previous HD package should no longer be staged once the swap unstages it.
            leftovers = [p for p in self.staging.iterdir() if p.is_dir() and "HD" in p.name]
            self.assertEqual(leftovers, [])

            restored = self._swap("HD")
            self.assertTrue(restored.get("replaced") or restored.get("ready") == ["Tamriel_Data.esm"])
            self._assert_hd()
            self._verify()
            self.assertEqual(ca.master_variant_choices(self.manager)["tamriel_data.esm"], "HD")
            self.assertFalse((self.data / "Tamriel_Data.bsa").exists())

    def test_scan_picks_the_chosen_variant_without_the_network(self):
        with redirect_stdout(io.StringIO()):
            plan = ca.scan_downloads(self.downloads, self.manager, self.root / "out", dry_run=True)
        used = Path(next(item["archive"] for item in plan["archives"]
                         if "Tamriel_Data.esm" in item.get("core_masters", item.get("plugins", [])))).name
        # SD's filename version is 26.09, so it wins until a choice is saved.
        self.assertTrue(used.startswith("Tamriel_Data 44537"), used)
        ca.choose_master_variant(self.manager, "Tamriel_Data.esm", "HD")
        with redirect_stdout(io.StringIO()):
            plan = ca.scan_downloads(self.downloads, self.manager, self.root / "out", dry_run=True)
        used = Path(next(item["archive"] for item in plan["archives"]
                         if "Tamriel_Data.esm" in item.get("core_masters", item.get("plugins", [])))).name
        self.assertTrue(used.startswith("Tamriel Data (HD)"), used)
        self.assertEqual(plan["master_variants"]["tamriel_data.esm"]["using"], "HD")

    def test_staged_masters_prefers_the_chosen_variant(self):
        with redirect_stdout(io.StringIO()):
            ca.choose_master_variant(self.manager, "Tamriel_Data.esm", "HD")
            ca.stage_core_master_archives(self._plan("HD"), ["Tamriel_Data.esm"],
                                          self.manager, self.game, clear_staging=False)
            ca.choose_master_variant(self.manager, "Tamriel_Data.esm", "SD")
            ca.stage_core_master_archives(self._plan("SD"), ["Tamriel_Data.esm"],
                                          self.manager, self.game, clear_staging=False)
        found = deploy.staged_masters(self.staging, ["Tamriel_Data.esm"])
        package = deploy._package_of(found["tamriel_data.esm"], self.staging)
        self.assertIn("44537", package.name)

    def test_bsa_files_finds_a_wrapped_archive(self):
        package = self.root / "pkg"
        put(package, "00 Data Files/Tamriel_Data.bsa", tes3_bsa({"textures/td/x.dds": b"x"}))
        found = deploy._bsa_files(package)
        self.assertEqual([p.name for p in found], ["Tamriel_Data.bsa"])

    def test_unstage_of_old_variant_is_allowed_once_the_new_one_is_live(self):
        with redirect_stdout(io.StringIO()):
            self._stage_and_deploy("HD")
            self._swap("SD")
        # After the swap the old HD folder is already unstaged; restage HD beside SD without deploying it,
        # then swap back and confirm unstage succeeds while dependents stay in Data Files.
        with redirect_stdout(io.StringIO()):
            ca.choose_master_variant(self.manager, "Tamriel_Data.esm", "HD")
            ca.stage_core_master_archives(self._plan("HD"), ["Tamriel_Data.esm"],
                                          self.manager, self.game, clear_staging=False)
            ca.swap_master_variant(self.manager, self.game, "Tamriel_Data.esm", "HD",
                                   plan=self._plan("HD"))
        self._assert_hd()
        self._assert_dependents()

    def test_sources_have_no_absolute_paths(self):
        files = [
            ROOT / "core_addons.py",
            ROOT / "major_master_deploy.py",
            ROOT / "swap_master_variant.py",
            Path(__file__),
        ]
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(?i)\b[A-Z]:\\", msg=f"absolute path in {path.name}")


if __name__ == "__main__":
    unittest.main()
