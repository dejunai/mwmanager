"""MWManager's answers and rules belong to one game folder: pointed at another, a fresh install starts clean and the
first game's decisions come back when it is used again."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mwmanager_gamestate as gamestate


class GameStateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.manager = Path(tmp.name)
        patcher = mock.patch.object(gamestate.tempfile, "gettempdir", return_value=str(Path(tmp.name) / "elsewhere"))
        patcher.start()                  # these games sit in a temp folder, which use_game otherwise ignores
        self.addCleanup(patcher.stop)
        self.steam, self.gog = Path(tmp.name) / "Steam" / "Morrowind", Path(tmp.name) / "GOG" / "Morrowind"
        for game in (self.steam, self.gog):
            game.mkdir(parents=True)
            (game / "Morrowind.exe").write_bytes(b"MZ")

    def write(self, rel, text):
        path = self.manager / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_another_game_starts_clean_and_the_first_comes_back(self):
        self.assertIsNone(gamestate.use_game(self.manager, self.steam))        # first use: adopted as it is
        self.write("mwmanager_conflict_answers.json", "{\"steam\": 1}")
        self.write("mwmanager_rules.txt", "[Order]\nsteam.esp\n")
        self.write("outputs/fomod_answers/nexus_1.json", "{}")
        self.write("mwmanager_core_addons.json", "{}")                        # MWManager's own: never moved

        self.assertIn("fresh start", gamestate.use_game(self.manager, self.gog))
        self.assertFalse((self.manager / "mwmanager_conflict_answers.json").exists())
        self.assertFalse((self.manager / "mwmanager_rules.txt").exists())
        self.assertFalse((self.manager / "outputs" / "fomod_answers").exists())
        self.assertTrue((self.manager / "mwmanager_core_addons.json").exists())
        self.write("mwmanager_rules.txt", "[Order]\ngog.esp\n")

        self.assertIsNone(gamestate.use_game(self.manager, self.gog))          # same game: nothing moves
        gamestate.use_game(self.manager, self.steam)
        self.assertEqual((self.manager / "mwmanager_rules.txt").read_text(encoding="utf-8"), "[Order]\nsteam.esp\n")
        self.assertTrue((self.manager / "outputs" / "fomod_answers" / "nexus_1.json").exists())
        gamestate.use_game(self.manager, self.gog)
        self.assertEqual((self.manager / "mwmanager_rules.txt").read_text(encoding="utf-8"), "[Order]\ngog.esp\n")
        self.assertFalse((self.manager / "mwmanager_conflict_answers.json").exists())


    def test_a_folder_in_temp_changes_nothing(self):
        gamestate.use_game(self.manager, self.gog)
        with mock.patch.object(gamestate.tempfile, "gettempdir", return_value=str(self.steam.parent)):
            self.assertIsNone(gamestate.use_game(self.manager, self.steam))
        self.assertIn("GOG", (self.manager / gamestate.OWNER).read_text(encoding="utf-8"))

    def test_a_folder_that_is_not_an_install_changes_nothing(self):
        gamestate.use_game(self.manager, self.gog)
        self.write("mwmanager_rules.txt", "[Order]\ngog.esp\n")
        self.assertIsNone(gamestate.use_game(self.manager, self.manager / "scratch" / "Morrowind"))
        self.assertTrue((self.manager / "mwmanager_rules.txt").exists())
        self.assertIn("GOG", (self.manager / gamestate.OWNER).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
