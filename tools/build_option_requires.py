"""Build mwmanager_option_requires.json: for every plugin in every downloaded archive, the Nexus mod that supplies it.
An option's "Requires: <plugin>, Nexus <id>" line looks its plugins' masters up here.

    python tools/build_option_requires.py [downloads folder]
The archive index (outputs/mwmanager_archive_plugins.json) is brought up to date first: a new archive is listed once."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core_addons      # noqa: E402
import needed_mods      # noqa: E402

DEFAULT_DOWNLOADS = Path(r"K:\Downloads\Vortex\morrowind")


def build(downloads: Path, manager_dir: Path = ROOT) -> dict:
    index = needed_mods.archive_index(downloads, manager_dir / "outputs" / "mwmanager_archive_plugins.json",
                                      core_addons._seven_zip(manager_dir), print)
    vortex = core_addons.vortex_downloads()
    plugins = {}
    for plugin in sorted({p for names in index.values() for p in names}, key=str.casefold):
        entry = needed_mods.resolve_requirement(plugin, index, downloads, vortex)
        if entry["nexus_id"]:
            plugins[plugin.casefold()] = entry
    return {"version": 1, "plugins": plugins}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    downloads = Path(argv[0]) if argv else DEFAULT_DOWNLOADS
    table = build(downloads)
    needed_mods.REQUIRES_FILE.write_text(json.dumps(table, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(table['plugins'])} plugins with a Nexus mod -> {needed_mods.REQUIRES_FILE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
