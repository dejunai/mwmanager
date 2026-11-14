"""mwmanagermenu puts the suite mods' settings pages under one Mod Config Menu entry, without changing them."""
import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MENU = ROOT / "reference" / "mwmanager-menu" / "mods" / "mwmanagermenu" / "main.lua"
SUITE = ROOT / "reference" / "mwse-lua-guards" / "mods"
LUAJIT = ROOT / "tools" / "luajit" / ("luajit.exe" if os.name == "nt" else "luajit")


class UnifiedMenu(unittest.TestCase):
    def test_its_tab_list_is_the_suite(self):
        text = MENU.read_text("utf-8")
        block = text[text.index("local SUITE = {"):]
        block = block[:block.index("}")]
        names = sorted(p.name for p in SUITE.iterdir() if (p / "main.lua").is_file())
        self.assertEqual(sorted(re.findall(r'"([^"]+)"', block)), names)

    def test_it_ships_with_a_metadata_file_that_loads_it_before_the_suite(self):
        toml = (ROOT / "reference" / "mwmanager-menu" / "metadata" / "mwmanagermenu-metadata.toml").read_text("utf-8")
        mine = int(re.search(r"load-priority\s*=\s*(\d+)", toml).group(1))
        for other in (ROOT / "reference" / "mwse-lua-guards" / "metadata").glob("*-metadata.toml"):
            m = re.search(r"load-priority\s*=\s*(\d+)", other.read_text("utf-8"))
            if m and other.name != "mwguard-metadata.toml":          # mwguard goes first, and registers later
                self.assertGreater(mine, int(m.group(1)), other.name)

    def test_suite_pages_go_under_one_entry_and_other_mods_keep_theirs(self):
        if not LUAJIT.is_file():
            self.skipTest("needs tools/luajit")
        out = subprocess.run([str(LUAJIT), str(Path(__file__).with_name("unified_menu_driver.lua")), str(MENU)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.splitlines()
        # only the other mod and the one combined entry reach the Mod Config Menu's list
        self.assertEqual([ln for ln in lines if ln.startswith("registered")],
                         ["registered Other Mod", "registered MWManager"])
        # tabs in the suite's order (mwsaveguard is listed before mwcollide), the first one open
        self.assertIn("tabs 2 mwsaveguard Doubled Objects", lines)
        self.assertIn("open tab state 4 nil", lines)
        # opening shows the first mod's own page; switching closes it (its own save) and shows the second;
        # closing the menu closes the page that is open
        flow = [ln for ln in lines if ln.split()[0] in ("create", "close")]
        self.assertEqual(flow, ["create mwsaveguard", "close mwsaveguard", "create Doubled Objects",
                                "close Doubled Objects"])
        self.assertIn("search true false", lines)


if __name__ == "__main__":
    unittest.main()
