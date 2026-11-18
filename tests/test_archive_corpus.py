import tempfile
import unittest
from pathlib import Path

from tools.archive_corpus import build
from plugin_install import analyze_archive


class ArchiveCorpusTests(unittest.TestCase):
    def test_generated_corpus_exercises_install_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = {path.name: path for path in build(root / "corpus")}
            self.assertTrue(analyze_archive({"archive": str(paths["clean-plugin.zip"])}, root)["safe"])
            for name, code in (
                    ("unsafe-traversal.zip", "unsafe_member_path"),
                    ("case-collision.zip", "path_collision"),
                    ("blocked-payload.zip", "suspicious_payload"),
                    ("symlink-member.zip", "link_member"),
                    ("high-ratio.zip", "compression_ratio_anomaly")):
                report = analyze_archive({"archive": str(paths[name])}, root)
                self.assertIn(code, {finding["code"] for finding in report["findings"]}, name)


if __name__ == "__main__":
    unittest.main()
