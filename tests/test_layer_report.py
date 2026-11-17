"""Every upstream name a MWManager layer replaces still exists upstream (a sync that renames or removes one would
leave the override silently unused). The baseline is tests/layer_overrides.json; after a deliberate change, rerun
with MWMANAGER_UPDATE_LAYER_BASELINE=1 to rewrite it."""
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mwmanager_upstream  # noqa: E402

BASELINE = ROOT / "tests" / "layer_overrides.json"


class LayerReportTests(unittest.TestCase):
    def test_every_override_still_replaces_an_upstream_name(self):
        mwmanager_upstream.compose()
        mwmanager_upstream.compose_tidy()
        now = {k: v["replaced"] for k, v in sorted(mwmanager_upstream.LAYER_REPORT.items())}
        if os.environ.get("MWMANAGER_UPDATE_LAYER_BASELINE") == "1":
            BASELINE.write_text(json.dumps(now, indent=1), encoding="utf-8")
        want = json.loads(BASELINE.read_text(encoding="utf-8"))
        problems = []
        for layer, names in want.items():
            report = mwmanager_upstream.LAYER_REPORT.get(layer, {"replaced": [], "added": []})
            for name in sorted(set(names) - set(report["replaced"])):
                where = "now added as new" if name in report["added"] else "gone"
                problems.append(f"{layer}: {name} no longer replaces an upstream name ({where}): "
                                f"renamed or removed upstream?")
            for name in sorted(set(report["replaced"]) - set(names)):
                problems.append(f"{layer}: {name} now replaces an upstream name (new override or upstream "
                                f"name clash)")
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
